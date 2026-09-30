"""
Aethelark-3D Headless SDCP Event Streamer & Dynamic Island Bridge.
Transforms raw SDCP websocket frames and telemetry into high-frequency,
structured Dynamic Island JSON event payloads (for Space-Eagle web/pill.html and web/dashboard.html).
"""

import os
import sys
import time
import json
import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, Optional, Callable, Generator, AsyncGenerator
from dataclasses import dataclass, asdict

from aethelark3d.config import config
from aethelark3d.spools import FilamentVault
from aethelark3d.drivers.factory import get_driver_for_printer
from aethelark3d.drivers.base import PrinterTelemetry, PrinterState


import tempfile
# The eagle host resolves this event file as
# `Path(tempfile.gettempdir()) / f"{key}_dynamic_island.json"` (Space-Eagle
# core/module_bus/manifest.py). Hardcoding "/tmp" agreed on Linux but broke the
# handshake on macOS, where gettempdir() is not /tmp — the island would read a
# file a3d never wrote. Resolve the temp dir the same way the host does.
def island_event_dir() -> Path:
    """Where a module's island events live: this user's alone.

    /tmp is shared by every account on the machine, so any local process could
    write an event file there and put a card on the island. On Linux the
    per-user runtime directory ($XDG_RUNTIME_DIR, mode 0700) is used instead;
    elsewhere the temp directory is already per-user. The host and every
    module must compute this the same way -- the listener inherits the host's
    environment, so they do.
    """
    import os
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime and os.path.isdir(runtime):
        return Path(runtime)
    return Path(tempfile.gettempdir())


DEFAULT_DYNAMIC_ISLAND_PATH = island_event_dir() / "a3d_dynamic_island.json"


@dataclass
class DynamicIslandEvent:
    module: str
    event: str
    icon: str
    title: str
    detail: str
    badge: str
    color: str
    duration_ms: int
    printer: str
    timestamp: float
    telemetry: Dict[str, Any]
    activity: Any = None
    alert: Optional[str] = None
    card: Any = None
    #: The island stage an alert opens on: "expanded" for a moment worth
    #: seeing through the camera. Sent as `_stage`, never read by the model.
    stage: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        stage = d.pop("stage", None)
        if stage:
            d["_stage"] = stage
        act, card = d.get("activity"), d.get("card")
        if isinstance(act, dict) and isinstance(card, dict) and card.get("thumb"):
            act.setdefault("image", card["thumb"])
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict())


_JOB_KEYS = ("TaskId", "task_id", "taskId", "job_id", "print_id")


def _job_id(telemetry: PrinterTelemetry) -> str:
    def find(d, depth=0):
        if not isinstance(d, dict) or depth > 3:
            return None
        for k in _JOB_KEYS:
            if d.get(k):
                return str(d[k])
        for v in d.values():
            hit = find(v, depth + 1)
            if hit:
                return hit
        return None
    return find(telemetry.raw_telemetry) or str(telemetry.current_file or "job")


def _eta(seconds: Optional[int]) -> str:
    if not seconds or seconds <= 0:
        return ""
    h, m = divmod(int(seconds) // 60, 60)
    return f"{h}h {m}m" if h else f"{max(m, 1)}m"


def _activity(pname: str, trailing: str, progress: Optional[float] = None,
              state: str = "active", relevance: int = 50) -> Dict[str, Any]:
    return {"id": pname, "state": state, "leading": pname, "trailing": trailing,
            "progress": progress, "relevance": relevance, "stale_after_s": 60}


_SPEEDS = ((50, "silent"), (100, "balanced"), (130, "sport"), (160, "ludicrous"))


def _find(d, names, depth=0):
    if not isinstance(d, dict) or depth > 3:
        return None
    for k, v in d.items():
        if k in names and v not in (None, ""):
            return v
    for v in d.values():
        hit = _find(v, names, depth + 1)
        if hit is not None:
            return hit
    return None


def _extras(t: PrinterTelemetry) -> Dict[str, Any]:
    raw = t.raw_telemetry or {}
    coord = _find(raw, {"CurrenCoord", "CurrentCoord", "coordinate", "position"})
    xyz = ""
    if isinstance(coord, str) and coord.count(",") == 2:
        try:
            x, y, z = (float(v) for v in coord.split(","))
            xyz = f"X {x:.1f}  Y {y:.1f}  Z {z:.2f}"
        except ValueError:
            xyz = ""
    light = _find(raw, {"SecondLight", "light_status", "chamber_light"})
    pct = _find(raw, {"PrintSpeedPct", "print_speed_pct", "speed_pct"})
    speed = ""
    try:
        if pct is not None:
            speed = min(_SPEEDS, key=lambda s: abs(s[0] - float(pct)))[1]
    except (TypeError, ValueError):
        speed = ""
    fans = _find(raw, {"CurrentFanSpeed", "fan_speed"})
    fans = fans if isinstance(fans, dict) else {}

    def fan(*names):
        v = next((fans[n] for n in names if isinstance(fans.get(n), (int, float))), None)
        return f"{v:.0f}%" if v is not None else "—"

    return {"xyz": xyz, "light": None if light is None else bool(light), "speed": speed,
            "fan_part": fan("ModelFan"), "fan_aux": fan("AuxiliaryFan"), "fan_box": fan("BoxFan")}


def _deg(v: float) -> str:
    return f"{v:.0f}°C" if v and v > 0 else "—"


def _bar(v: float, target: float) -> int:
    return max(0, min(100, round(v / target * 100))) if v and target and target > 0 else 0


def job_card(key: str, pname: str, t: PrinterTelemetry, state_label: str,
             pct: Optional[float]) -> Dict[str, Any]:
    from aethelark3d import job_thumbs
    ex = _extras(t)
    thumb = job_thumbs.lookup(t.current_file)
    rem = t.time_remaining_seconds or 0
    finish = ""
    if rem > 0:
        finish = "done " + (datetime.now() + timedelta(seconds=rem)).strftime("%I:%M %p").lstrip("0")
    card = {
        "variant": "job",
        "printer": pname, "printer_key": key, "state_label": state_label,
        "pct_label": f"{pct:.0f}%" if pct is not None else "—",
        "progress_pct": round(pct) if pct is not None else 0,
        "eta_left": f"{_eta(rem)} left" if rem > 0 else "",
        "finish_at": finish,
        "layer_label": f"{t.current_layer} / {t.total_layers}" if t.total_layers else "—",
        "xyz": ex["xyz"] or "—",
        "thumb": thumb.as_uri() if thumb else "",
        "nozzle": _deg(t.nozzle_temp), "nozzle_target": _deg(t.nozzle_target),
        "bed": _deg(t.bed_temp), "bed_target": _deg(t.bed_target),
        "chamber": _deg(t.chamber_temp),
        "nozzle_pct": _bar(t.nozzle_temp, t.nozzle_target),
        "bed_pct": _bar(t.bed_temp, t.bed_target),
        "file_tail": str(t.current_file or ""),
        "light_on": "true" if ex["light"] else "false",
        "light_next": "off" if ex["light"] else "on",
        "pause_action": "resume" if state_label == "Paused" else "pause",
        "pause_label": "Resume print" if state_label == "Paused" else "Pause print",
        "fan_part": ex["fan_part"], "fan_aux": ex["fan_aux"], "fan_box": ex["fan_box"],
        "fans": "·".join(v.rstrip("%") for v in (ex["fan_part"], ex["fan_aux"], ex["fan_box"]))
                + ("%" if ex["fan_part"] != "—" else ""),
        "cam_note": " · ".join(x for x in (f"{pct:.0f}%" if pct is not None else "",
                                         f"Layer {t.current_layer}/{t.total_layers}" if t.total_layers else "")
                               if x) or state_label,
        "title": pname,
        "detail": " · ".join(x for x in (f"{_eta(rem)} left" if rem > 0 else "",
                                         f"Layer {t.current_layer}/{t.total_layers}" if t.total_layers else "")
                             if x) or state_label,
        "badge": f"{pct:.0f}%" if pct is not None else state_label,
    }
    for _, mode in _SPEEDS:
        card[f"speed_{mode}"] = "true" if ex["speed"] == mode else "false"
    return card


class SDCPEventStreamer:
    """
    Headless SDCP Event Streamer & HUD telemetry bridge.
    """

    def __init__(
        self,
        printer_key: str = "",
        use_simulator: bool = False,
        event_file: Optional[Path] = None
    ):
        if not printer_key:
            from .config import config as _config
            printer_key = _config.default_printer
        self.printer_key = printer_key.upper().replace("-", "_")
        self.use_simulator = use_simulator
        self.event_file = event_file or DEFAULT_DYNAMIC_ISLAND_PATH
        self.driver = get_driver_for_printer(self.printer_key, use_simulator=self.use_simulator)

    def format_event_frame(self, telemetry: PrinterTelemetry) -> DynamicIslandEvent:
        """
        Convert telemetry into normalized Dynamic Island visual event schema.
        """
        from .config import config as _config
        pname = ((_config.get_printer(self.printer_key) or {}).get("name")
                 or telemetry.printer_name or self.printer_key)
        now = time.time()

        # Check FilamentVault for spool warnings
        slots = FilamentVault.get_printer_slots(self.printer_key)
        critically_low_spool = None
        if slots:
            for s_name, s_data in slots.items():
                if s_data.get("remaining_grams", 1000.0) < 20.0:
                    critically_low_spool = (s_name, s_data)
                    break

        job = f"{pname}:{_job_id(telemetry)}"

        # 1. Finished & Bed Cooled State
        if telemetry.state == PrinterState.COMPLETED:
            bed_t = telemetry.bed_temp
            is_cool = bed_t <= 35.0
            sub = "Bed is cool · ready to lift off" if is_cool else f"Bed cooling · {bed_t:.0f}°C"
            badge = "Ready" if is_cool else "Cooling"
            return DynamicIslandEvent(
                module="3d",
                event="print_complete",
                icon="complete",
                printer=pname,
                title=f"{pname} · Finished",
                detail=sub,
                badge=badge,
                color="#A855F7",
                duration_ms=10000,
                timestamp=now,
                telemetry=asdict(telemetry),
                activity=_activity(pname, "Done", 1.0, state="ended") if is_cool
                else _activity(pname, f"Cooling {bed_t:.0f}°", 1.0),
                alert=f"{job}:cooled" if is_cool else f"{job}:complete",
                card=job_card(self.printer_key, pname, telemetry, "Finished", 100.0),
            )

        # 2. Hardware Pause / Intervention State
        if telemetry.state == PrinterState.PAUSED:
            return DynamicIslandEvent(
                module="3d",
                event="printer_paused",
                icon="paused",
                printer=pname,
                title=f"{pname} · Paused",
                detail="Check the filament, then say resume",
                badge="Paused",
                color="#D4AF37",
                duration_ms=10000,
                timestamp=now,
                telemetry=asdict(telemetry),
                activity=_activity(pname, "Paused", (telemetry.progress_percent or 0) / 100,
                                   relevance=90),
                alert=f"{job}:paused:{telemetry.current_layer}",
                card=job_card(self.printer_key, pname, telemetry, "Paused",
                              telemetry.progress_percent),
                stage="expanded",
            )

        # 3. Printing State Progress Ring (Primary Telemetry Priority)
        if telemetry.state in [PrinterState.PRINTING, PrinterState.PREPARING, PrinterState.HEATING, PrinterState.LEVELING]:
            cur_layer = telemetry.current_layer
            tot_layer = max(1, telemetry.total_layers)
            pct = round(telemetry.progress_percent if telemetry.progress_percent > 0 else (cur_layer / tot_layer * 100.0), 1)
            
            time_badge = f"{pct:.0f}%"
            time_detail = f"Layer {cur_layer}/{tot_layer}"
            if telemetry.time_remaining_seconds and telemetry.time_remaining_seconds > 0:
                finish_dt = datetime.now() + timedelta(seconds=telemetry.time_remaining_seconds)
                finish_str = finish_dt.strftime("%I:%M %p").lstrip("0")
                time_detail = f"Layer {cur_layer}/{tot_layer} · done {finish_str}"

            warming = {PrinterState.HEATING: "Heating", PrinterState.LEVELING: "Leveling",
                       PrinterState.PREPARING: "Preparing"}.get(telemetry.state)
            live = _activity(pname, warming or _eta(telemetry.time_remaining_seconds)
                             or f"{pct:.0f}%", None if warming else min(1.0, pct / 100))

            if cur_layer == 2 and not warming:
                return DynamicIslandEvent(
                    module="3d",
                    event="first_layer_passed",
                    icon="first_layer",
                    printer=pname,
                    title=f"{pname} · First layer done",
                    detail=f"Layer 1 of {tot_layer} finished · worth a look",
                    badge=time_badge,
                    color="#00FFA3",
                    duration_ms=6000,
                    timestamp=now,
                    telemetry=asdict(telemetry),
                    activity=live,
                    alert=f"{job}:first_layer",
                    card=job_card(self.printer_key, pname, telemetry, "Printing", pct),
                    stage="expanded",
                )

            return DynamicIslandEvent(
                module="3d",
                event="print_progress",
                icon="printing",
                printer=pname,
                title=f"{pname} · {warming or 'Printing'}",
                detail=time_detail,
                badge=time_badge,
                color="#00E5FF",
                duration_ms=4000,
                timestamp=now,
                telemetry=asdict(telemetry),
                activity=live,
                card=job_card(self.printer_key, pname, telemetry, warming or "Printing", pct),
            )

        # 4. A fault the printer itself reported. The alarm is what it is for.
        if telemetry.state == PrinterState.ERROR:
            return DynamicIslandEvent(
                module="3d",
                event="printer_error",
                icon="attention",
                printer=pname,
                title=f"{pname} · Stopped",
                detail="The printer reported an error",
                badge="Check it",
                color="#FF453A",
                duration_ms=10000,
                timestamp=now,
                telemetry=asdict(telemetry),
                activity=_activity(pname, "Stopped", state="ended")
                if telemetry.current_file else None,
                alert=f"{job}:error" if telemetry.current_file else None,
                card=job_card(self.printer_key, pname, telemetry, "Stopped",
                              telemetry.progress_percent) if telemetry.current_file else None,
                stage="expanded" if telemetry.current_file else None,
            )

        # 4b. We could not reach it. That is a fact about the network, not about
        # the hardware -- nothing stopped, no feed stalled, and the hotend's
        # state is unknown because no socket was ever opened. Reported at
        # standby priority so a machine that is merely switched off cannot
        # displace a print genuinely running on another one.
        if telemetry.state == PrinterState.DISCONNECTED:
            where = telemetry.ip_address or "its last known address"
            lan_only = "LAN Only" in str((telemetry.raw_telemetry or {}).get("error") or "")
            return DynamicIslandEvent(
                module="3d",
                event="printer_standby",
                icon="standby",
                printer=pname,
                title=f"{pname} · Offline",
                detail=("Turn on LAN Only in its network settings" if lan_only
                        else f"No answer from {where} · check power and Wi-Fi"),
                badge="Offline",
                color="#7A7A7A",
                duration_ms=4000,
                timestamp=now,
                telemetry=asdict(telemetry)
            )

        # 5. Spool Low Priority Alert (When Idle / Standby)
        if critically_low_spool:
            s_name, s_data = critically_low_spool
            mat = s_data.get("material", "PLA")
            col = s_data.get("color", "Filament")
            rem = s_data.get("remaining_grams", 0.0)
            return DynamicIslandEvent(
                module="3d",
                event="spool_low_alert",
                icon="spool_low",
                printer=pname,
                title=f"{pname} · Spool low",
                detail=f"{col} {mat} in slot {s_name} · {rem:.0f}g left",
                badge=f"{rem:.0f}g",
                color="#FF9100",
                duration_ms=8000,
                timestamp=now,
                telemetry=asdict(telemetry)
            )

        # 6. Idle / Standby State
        return DynamicIslandEvent(
            module="3d",
            event="printer_standby",
            icon="standby",
            printer=pname,
            title=f"{pname} · Idle",
            detail=f"Nozzle {telemetry.nozzle_temp:.0f}°C · Bed {telemetry.bed_temp:.0f}°C",
            badge="Idle",
            color="#7A7A7A",
            duration_ms=3000,
            timestamp=now,
            telemetry=asdict(telemetry)
        )

    def write_event_atomically(self, event: DynamicIslandEvent, target_path: Optional[Path] = None) -> Path:
        """
        Write event JSON atomically via tmp file swap so consumers never read corrupt bytes.
        """
        out_path = target_path or self.event_file
        out_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = out_path.with_suffix(f"{out_path.suffix}.tmp_{os.getpid()}_{int(time.time()*1000)}")

        with open(temp_path, "w", encoding="utf-8") as f:
            f.write(event.to_json() + "\n")
            f.flush()
            os.fsync(f.fileno())

        os.replace(temp_path, out_path)
        return out_path

    async def poll_single_frame(self, write: bool = True) -> DynamicIslandEvent:
        """
        Fetch one telemetry frame from driver and format into Dynamic Island event.
        """
        await self.driver.connect()
        telemetry = await self.driver.get_telemetry()
        event = self.format_event_frame(telemetry)
        if write:
            self.write_event_atomically(event)
        return event

    async def stream_generator(
        self,
        interval: float = 2.0,
        max_iterations: Optional[int] = None
    ) -> AsyncGenerator[DynamicIslandEvent, None]:
        """
        Asynchronous generator yielding continuous live events.
        """
        count = 0
        while True:
            if max_iterations is not None and count >= max_iterations:
                break
            try:
                event = await self.poll_single_frame()
                yield event
            except Exception as e:
                err_event = DynamicIslandEvent(
                    module="3d",
                    event="printer_error",
                    icon="⚠️",
                    printer=self.printer_key,
                    title=f"{self.printer_key.upper()} OFFLINE",
                    detail=f"Connection error: {str(e)[:60]}",
                    badge="Check Link",
                    color="#FF453A",
                    duration_ms=5000,
                    timestamp=time.time(),
                    telemetry={"error": str(e)}
                )
                self.write_event_atomically(err_event)
                yield err_event

            count += 1
            await asyncio.sleep(interval)


class FleetSDCPStreamer:
    """
    Fleet-wide SDCP Event Streamer.
    Monitors all configured fleet printers (CC1, CC2, C2_COMBO) concurrently,
    aggregating and prioritizing high-signal human-centric events for Space-Eagle.
    """

    EVENT_PRIORITY = {
        "printer_error": 100,      # Urgent: Clog, air printing, thermal fault
        "printer_paused": 90,      # Urgent: Paused for magnets/hardware/swap
        "spool_low_alert": 80,     # Warning: Spool low advance notice
        "first_layer_passed": 70,  # Milestone: Layer 1 adhered successfully
        "print_complete": 60,      # Celebration: Finished & cooled
        "print_progress": 50,      # Ambient: Active printing progress
        "purgex_savings_achieved": 40,
        "printer_standby": 10,     # Low priority: Idle standby
    }

    def __init__(
        self,
        printer_keys: Optional[list[str]] = None,
        use_simulator: bool = False,
        event_file: Optional[Path] = None
    ):
        # Not three names in the source. See Config.addressable_printer_keys.
        from aethelark3d.config import config as _config
        #: Follow the fleet as it changes, unless the caller named printers.
        self._follows_config = not printer_keys
        self.use_simulator = use_simulator
        self.event_file = event_file or DEFAULT_DYNAMIC_ISLAND_PATH
        self.printer_keys: list[str] = []
        self.streamers: dict = {}
        self._set_keys(printer_keys or _config.addressable_printer_keys())

    def _set_keys(self, keys) -> None:
        keys = list(keys or [])
        if keys == self.printer_keys and self.streamers:
            return
        self.printer_keys = keys
        self.streamers = {
            pk: SDCPEventStreamer(pk, use_simulator=self.use_simulator, event_file=self.event_file)
            for pk in keys
        }

    def refresh_fleet(self) -> None:
        """Pick up printers added or removed since this listener started.

        The eagle starts one listener for the whole session. A printer found by
        `a3d discover` halfway through would otherwise stay unwatched until the
        next launch, and a fresh install -- no printers yet -- would watch
        nothing forever.
        """
        if not self._follows_config:
            return
        from aethelark3d.config import config as _config
        try:
            _config.load()
        except Exception:
            return
        self._set_keys(_config.addressable_printer_keys())

    async def poll_fleet_frame(self) -> DynamicIslandEvent:
        """
        Poll all fleet printers in parallel and return the highest priority event.
        """
        tasks = [s.poll_single_frame(write=False) for s in self.streamers.values()]
        events: list[DynamicIslandEvent] = await asyncio.gather(*tasks, return_exceptions=False)

        # Sort by priority desc, then by progress percent if both printing
        def sort_key(ev: DynamicIslandEvent):
            prio = self.EVENT_PRIORITY.get(ev.event, 0)
            pct = 0.0
            if ev.telemetry and "progress_percent" in ev.telemetry:
                pct = ev.telemetry.get("progress_percent") or 0.0
            return (prio, pct)

        events.sort(key=sort_key, reverse=True)
        top_event = events[0]
        blocks = [ev.activity for ev in events if isinstance(ev.activity, dict)]
        if blocks:
            top_event.activity = blocks
        self.streamers[self.printer_keys[0]].write_event_atomically(top_event, self.event_file)
        return top_event

    async def stream_fleet_generator(
        self,
        interval: float = 2.0,
        max_iterations: Optional[int] = None
    ) -> AsyncGenerator[DynamicIslandEvent, None]:
        """
        Continuously stream the highest priority fleet event.
        """
        count = 0
        while True:
            if max_iterations is not None and count >= max_iterations:
                break
            if count % 15 == 0:
                self.refresh_fleet()
            if not self.streamers:
                # Nothing to watch is not an error. Say nothing, look again.
                count += 1
                await asyncio.sleep(interval)
                continue
            try:
                top_event = await self.poll_fleet_frame()
                yield top_event
            except Exception as e:
                err_event = DynamicIslandEvent(
                    module="3d",
                    event="printer_error",
                    icon="⚠️",
                    printer="FLEET",
                    title="FLEET TELEMETRY ERROR",
                    detail=str(e)[:60],
                    badge="Check Fleet",
                    color="#FF453A",
                    duration_ms=5000,
                    timestamp=time.time(),
                    telemetry={"error": str(e)}
                )
                yield err_event

            count += 1
            await asyncio.sleep(interval)
