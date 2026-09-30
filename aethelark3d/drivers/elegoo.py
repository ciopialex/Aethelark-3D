"""
Elegoo SDCP Printer Driver for Aethelark-3D.
Supports Centauri Carbon, CC2, Centauri 2 Combo (AMS), and Neptune series.
"""

import asyncio
import contextlib
import hashlib
import json
import math
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Any
import requests
import websockets

from aethelark3d.drivers.base import BasePrinterDriver, PrinterTelemetry, PrinterState, PrinterCapability
from aethelark3d.spools import FilamentVault


#: Elegoo ships the CC2 / ElegooLink LAN API with a fixed factory credential
#: that is in force whenever the on-screen "Access Code" toggle is OFF — the
#: out-of-the-box default state. It is the same static value for the family
#: (not derived from serial or mainboard id), used as the MQTT username /
#: password and the HTTP X-Token. Confirmed on a real Centauri Carbon 2,
#: 2026-09-11. A user who turns the toggle ON sets their own code, stored per
#: printer; until then this is what lets the eagle connect with zero setup.
#:
#: The connect path tries this first and falls back to asking for a code only
#: if the printer refuses it, so a custom code or a firmware variant still
#: works — it just costs one prompt instead of none.
DEFAULT_CC2_ACCESS_CODE = "123456"


def _remember_thumb(local, remote_name) -> None:
    try:
        from aethelark3d.job_thumbs import remember
        remember(local, str(remote_name))
    except Exception:
        pass


#: A job's state in the Centauri Carbon's PrintInfo.Status codes. The Carbon 2
#: reports the same codes through pycentauri, which translates its two-level
#: machine_status/sub_status into them (13 printing measured on both).
_JOB_STATE = {
    1: PrinterState.PREPARING, 10: PrinterState.PREPARING, 16: PrinterState.HEATING,
    15: PrinterState.LEVELING, 5: PrinterState.PAUSED, 6: PrinterState.PAUSED,
    12: PrinterState.PRINTING, 13: PrinterState.PRINTING, 7: PrinterState.PRINTING,
    8: PrinterState.STOPPED, 9: PrinterState.COMPLETED, 14: PrinterState.ERROR,
}


def job_state(code) -> PrinterState:
    """The state of a job the machine says it is running."""
    try:
        return _JOB_STATE.get(int(code), PrinterState.PRINTING)
    except (TypeError, ValueError):
        return PrinterState.PRINTING


class ElegooSDCPDriver(BasePrinterDriver):
    """Production SDCP / CC2 Protocol Driver for Elegoo 3D Printers."""

    _cc2_cache_by_ip: Dict[str, Dict[str, Any]] = {}

    def __init__(
        self,
        name: str,
        ip: str,
        port: int = 3030,
        capabilities: Optional[PrinterCapability] = None,
        access_code: Optional[str] = None
    ):
        caps = capabilities or PrinterCapability(
            brand="Elegoo",
            model="Centauri Carbon",
            nozzle_diameter=0.4,
            build_volume=(256, 256, 256),
            has_ams=False,
            ams_slot_count=1,
            supported_file_extensions=[".gcode"]
        )
        super().__init__(name, ip, port, caps)
        self.access_code = access_code
        self.ws_uri = f"ws://{self.ip}:{self.port}/websocket"
        self.http_upload_url = f"http://{self.ip}/uploadFile/upload"
        self._ws = None

    @property
    def is_cc2_family(self) -> bool:
        """True if printer uses the CC2/ElegooLink MQTT protocol.

        Port 80 is the discriminator that always holds: resolve_transport maps
        the ElegooLink family to port 80 and SDCP (CC1) to 3030, so a printer
        found by discovery is routed correctly even before it has a stored
        code and even when its model name is only the MAC-derived placeholder.
        The access-code and model checks stay as belt-and-suspenders for
        entries created another way.
        """
        return (
            self.port == 80
            or bool(self.access_code)
            or self.capabilities.model in [
                "Elegoo Centauri Carbon 2",
                "Elegoo Centauri 2",
                "Centauri Carbon 2",
                "Centauri 2",
            ]
        )

    @property
    def _effective_code(self) -> str:
        """The code to authenticate with: the user's if they set one, else the
        Elegoo factory default. Trying the default first is what makes an
        out-of-the-box printer connect with no setup; a printer with a custom
        code simply carries it here instead."""
        return self.access_code or DEFAULT_CC2_ACCESS_CODE

    @contextlib.asynccontextmanager
    async def _cc2(self, enable_control: bool = False):
        """One ElegooLink (MQTT) session, closed however the block ends.

        Every CC2 call used to be connect -> request -> close with no
        `finally`, so any timeout or error skipped the close and stranded the
        connection on the printer's broker. Measured 2026-09-24: an orphaned
        `a3d listen --fleet`, polling every 2 s for 56 minutes, held 340
        ESTABLISHED connections to 192.168.8.105:1883 and burned 37% of a
        core. A clean cycle leaks nothing -- the failures were the leak.
        """
        from pycentauri import CC2Printer
        p = await CC2Printer.connect(self.ip, access_code=self._effective_code,
                                     enable_control=enable_control)
        try:
            yield p
        finally:
            try:
                await p.close()
            except Exception:
                pass

    async def connect(self) -> bool:
        if self.is_cc2_family:
            try:
                res = requests.get(
                    f"http://{self.ip}/system/info",
                    params={"X-Token": self._effective_code},
                    timeout=1.0
                )
                if res.status_code == 200:
                    return True
            except Exception:
                pass
            try:
                async with self._cc2(enable_control=False):
                    return True
            except Exception:
                return False
        # Reuse a socket that is still open. Without this, every call opened a
        # NEW websocket and overwrote self._ws, orphaning the previous one
        # without closing it — and the streamer calls connect() once per poll,
        # every 2 seconds, forever.
        #
        # Measured 2026-09-07 against CC1 while it was genuinely printing:
        #   listener running   10 sockets held   `a3d status` 3.43s -> DISCONNECTED, 0/0, 0.0C
        #   listener killed     0 sockets        `a3d status` 0.53s -> PRINTING, layer 9/66, 260.1C
        #
        # A printer's SDCP endpoint accepts a small number of concurrent
        # clients. One leaking listener exhausts it, so every OTHER client —
        # including the `a3d status` the eagle runs to answer "how is CC1" —
        # times out and reports the printer offline while it is printing. That
        # is both the 6.5x latency and the wrong answer.
        if self._ws is not None and not self._ws_is_dead():
            return True
        await self.disconnect()          # never stack a second one on a corpse
        try:
            self._ws = await websockets.connect(self.ws_uri, open_timeout=4.0)
            return True
        except Exception:
            self._ws = None
            return False

    def _ws_is_dead(self) -> bool:
        """True when the handle we hold can no longer carry a frame.

        websockets 15 dropped `.closed` from the asyncio client and exposes
        `.state` / `.close_code` instead, while the legacy client still has
        `.closed`. Both are checked rather than one assumed, because guessing
        wrong here fails in the direction that reopens the leak.
        """
        ws = self._ws
        if ws is None:
            return True
        closed = getattr(ws, "closed", None)
        if closed is not None:
            return bool(closed)
        if getattr(ws, "close_code", None) is not None:
            return True
        state = getattr(ws, "state", None)
        return state is not None and getattr(state, "name", "") != "OPEN"

    async def disconnect(self) -> None:
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

    def _mainboard_id(self) -> str:
        cached = getattr(self, "_board", None)
        if cached is not None:
            return cached
        board = ""
        try:
            from aethelark3d.config import config
            for slot in (config.printers or {}).values():
                if isinstance(slot, dict) and str(slot.get("host")) == str(self.ip):
                    board = str(slot.get("mainboard_id") or "")
                    break
        except Exception:
            board = ""
        if board.startswith("elegoolink:"):
            board = ""
        self._board = board
        return board

    async def _send_sdcp(self, cmd: int, data: Dict[str, Any], from_type: int = 2,
                         want_status: bool = False) -> Dict[str, Any]:
        """Transmit raw SDCP packet and return response payload.

        One request produces two frames. `sdcp/response/<id>` carries only an
        acknowledgement that the request landed -- its payload is `{"Ack": 0}`.
        The machine's actual state arrives separately on `sdcp/status/<id>`.

        `want_status` asks for the second. Matching on `Data.Cmd` alone returns
        the ack, and since every telemetry field then falls to its default, the
        printer reads as idle at zero degrees no matter what it is doing -- an
        answer indistinguishable from a genuinely idle machine.
        """
        req_id = f"a3d_{cmd}_{int(time.time() * 1000)}"
        board = self._mainboard_id()
        msg = {
            "Id": "",
            "Topic": f"sdcp/request/{board}" if board else "",
            "Data": {
                "Cmd": cmd,
                "Data": data,
                "MainboardID": board,
                "RequestID": req_id,
                "TimeStamp": int(time.time()),
                "From": from_type
            }
        }

        ws = self._ws
        must_close = False
        try:
            if not ws:
                try:
                    ws = await websockets.connect(self.ws_uri, open_timeout=2.0)
                except (asyncio.TimeoutError, OSError):
                    await asyncio.sleep(0.4)
                    ws = await websockets.connect(self.ws_uri, open_timeout=3.0)
                must_close = True

            await ws.send(json.dumps(msg))
            deadline = time.monotonic() + 8.0
            while time.monotonic() < deadline:
                raw = await asyncio.wait_for(ws.recv(), max(0.1, deadline - time.monotonic()))
                resp = json.loads(raw)
                if want_status:
                    status = resp.get("Status")
                    if isinstance(status, dict):
                        return status
                    continue
                body = resp.get("Data", {}) if isinstance(resp.get("Data"), dict) else {}
                if body.get("RequestID") == req_id or (body.get("Cmd") == cmd and "Status" not in resp):
                    return body.get("Data", {}) or {}
            return {}
        except Exception:
            return {}
        finally:
            if must_close and ws:
                try:
                    await ws.close()
                except Exception:
                    pass

    def _sniff_cc2_telemetry(self, timeout: float = 1.5) -> Optional[PrinterTelemetry]:
        """Passive MQTT telemetry collector for CC2.

        When another client (such as Elegoo Slicer GUI) is open, the CC2 LAN API
        rejects active client registration with 'too many clients', causing active
        RPC method 1002 (p.status()) to time out.
        However, the CC2 broker continuously broadcasts 1Hz telemetry deltas on
        `elegoo/{sn}/api_status` (method 6000 pushes) to any connected MQTT subscriber
        without needing client registration.
        This method sniffs those broadcast deltas, updates the cache, and builds
        accurate real-time PrinterTelemetry without connection contention.
        """
        import paho.mqtt.client as mqtt

        ip = self.ip
        code = self._effective_code
        cache = self._cc2_cache_by_ip.setdefault(ip, {})
        loaded = FilamentVault.get_active_spools(self.name)

        http_ok = False
        sys_info = {}
        try:
            res = requests.get(
                f"http://{ip}/system/info",
                params={"X-Token": code},
                timeout=0.8
            )
            if res.status_code == 200:
                http_ok = True
                sys_info = res.json().get("system_info", {})
        except Exception:
            pass

        got_message = [False]

        def on_connect(client, userdata, flags, rc, props=None):
            client.subscribe("elegoo/#")

        def deep_merge(target, src):
            for k, v in src.items():
                if isinstance(v, dict) and isinstance(target.get(k), dict):
                    deep_merge(target[k], v)
                else:
                    target[k] = v

        def on_message(client, userdata, msg):
            try:
                data = json.loads(msg.payload)
                res = data.get("result", {})
                if isinstance(res, dict):
                    deep_merge(cache, res)
                    if "extruder" in res or "print_status" in res:
                        got_message[0] = True
            except Exception:
                pass

        client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
        client.username_pw_set("elegoo", code)
        client.on_connect = on_connect
        client.on_message = on_message

        try:
            client.connect(ip, 1883, keepalive=10)
            client.loop_start()
            start = time.time()
            while time.time() - start < timeout:
                if got_message[0] and (time.time() - start >= 0.3 or ("extruder" in cache and "print_status" in cache)):
                    break
                time.sleep(0.05)
            client.loop_stop()
            client.disconnect()
        except Exception:
            if not http_ok:
                return None

        if not http_ok and not got_message[0] and not cache:
            return None

        ext = cache.get("extruder", {})
        bed = cache.get("heater_bed", {})
        ps = cache.get("print_status", {})
        ms = cache.get("machine_status", {})
        z_sens = cache.get("ztemperature_sensor", {})

        nozzle_temp = float(ext.get("temperature", 0.0) or 0.0)
        nozzle_target = float(ext.get("target", 0.0) or 0.0)
        bed_temp = float(bed.get("temperature", 0.0) or 0.0)
        bed_target = float(bed.get("target", 0.0) or 0.0)
        chamber_temp = float(z_sens.get("temperature", 0.0) or 0.0)

        current_layer = int(ps.get("current_layer", 0) or 0)
        total_layers = int(ps.get("total_layer", 0) or 0)
        remaining_sec = int(ps.get("remaining_time_sec", 0) or 0)
        print_duration = int(ps.get("print_duration", 0) or 0)
        total_duration = int(ps.get("total_duration", 0) or 0)
        filename = ps.get("filename")

        if ms.get("progress") is not None and float(ms.get("progress")) > 0:
            progress = float(ms["progress"])
        elif total_duration > 0 and print_duration > 0:
            progress = min(100.0, round((print_duration / total_duration) * 100.0, 1))
        elif total_layers > 0 and current_layer > 0:
            progress = min(100.0, round((current_layer / total_layers) * 100.0, 1))
        else:
            progress = 0.0

        status_code = ms.get("status", 0)
        if status_code == 2 or remaining_sec > 0 or (nozzle_temp > 150.0 and print_duration > 0):
            state = PrinterState.PRINTING
        elif status_code in (2502, 2505):
            state = PrinterState.PAUSED
        elif status_code == 1 or (nozzle_temp < 60.0 and print_duration == 0):
            state = PrinterState.IDLE
        elif http_ok or got_message[0]:
            state = PrinterState.PRINTING if nozzle_temp > 150.0 else PrinterState.IDLE
        else:
            state = PrinterState.DISCONNECTED

        return PrinterTelemetry(
            printer_name=self.name,
            brand=self.capabilities.brand,
            ip_address=self.ip,
            state=state,
            substate_code=None,
            nozzle_temp=nozzle_temp,
            nozzle_target=nozzle_target,
            bed_temp=bed_temp,
            bed_target=bed_target,
            chamber_temp=chamber_temp,
            current_layer=current_layer,
            total_layers=total_layers,
            progress_percent=progress,
            current_file=filename,
            time_remaining_seconds=remaining_sec if remaining_sec > 0 else None,
            loaded_spools=loaded,
            raw_telemetry={"_cc2": cache, "system_info": sys_info}
        )

    async def get_telemetry(self) -> PrinterTelemetry:
        loaded = FilamentVault.get_active_spools(self.name)

        if self.is_cc2_family:
            try:
                async with self._cc2() as p:
                    stat = await p.status(timeout=0.8)

                # machine_status 2 is a job running, 5 levelling, 6 an error;
                # 1 idle, 3/4 loading or unloading filament. Inside a job the
                # translated PrintInfo.Status says which part of it.
                st_code = stat.print_info.status if stat.print_info else 0
                machine_st = stat.raw.get("_cc2", {}).get("machine_status", 0)
                if machine_st == 2:
                    state = job_state(st_code)
                elif machine_st == 5:
                    state = PrinterState.LEVELING
                elif machine_st == 6:
                    state = PrinterState.ERROR
                else:
                    state = PrinterState.IDLE

                return PrinterTelemetry(
                    printer_name=self.name,
                    brand=self.capabilities.brand,
                    ip_address=self.ip,
                    state=state,
                    substate_code=None,
                    nozzle_temp=float(stat.temp_nozzle or 0.0),
                    nozzle_target=float(stat.temp_nozzle_target or 0.0),
                    bed_temp=float(stat.temp_bed or 0.0),
                    bed_target=float(stat.temp_bed_target or 0.0),
                    chamber_temp=float(stat.temp_chamber or 0.0) if stat.temp_chamber is not None else 0.0,
                    current_layer=int(stat.print_info.current_layer or 0) if stat.print_info else 0,
                    total_layers=int(stat.print_info.total_layer or 0) if stat.print_info and stat.print_info.total_layer else 0,
                    progress_percent=float(stat.print_info.progress or 0.0) if stat.print_info else 0.0,
                    current_file=stat.print_info.filename if stat.print_info else None,
                    time_remaining_seconds=int(stat.raw.get("_cc2", {}).get("remaining_time_sec", 0)),
                    loaded_spools=loaded,
                    raw_telemetry=stat.raw
                )
            except Exception as e:
                sniffed = await asyncio.to_thread(self._sniff_cc2_telemetry, timeout=1.5)
                if sniffed:
                    return sniffed
                return PrinterTelemetry(
                    printer_name=self.name,
                    brand=self.capabilities.brand,
                    ip_address=self.ip,
                    state=PrinterState.DISCONNECTED,
                    loaded_spools=loaded,
                    raw_telemetry={"error": str(e)}
                )

        try:
            res = await self._send_sdcp(cmd=0, data={}, from_type=2, want_status=True)
            if not res:
                return PrinterTelemetry(
                    printer_name=self.name,
                    brand=self.capabilities.brand,
                    ip_address=self.ip,
                    state=PrinterState.DISCONNECTED,
                    loaded_spools=loaded
                )

            # CurrentStatus is a list: the board reports concurrent machine
            # states, and the first is the one a card shows. Older firmware
            # sends a bare int, so both are accepted.
            current = res.get("CurrentStatus")
            if isinstance(current, list):
                status_code = current[0] if current else 0
            else:
                status_code = current or 0

            # Everything about the job itself is nested one level down.
            print_info = res.get("PrintInfo") or {}

            # CurrentStatus is the machine: 0 idle, 1 running a job, 2 taking a
            # file. It never means paused; reading 2 as PAUSED made every
            # upload look like a paused print. What the job is doing is its
            # own Status (job_state).
            job_status = print_info.get("Status")
            if status_code == 1:
                state = job_state(job_status)
            elif status_code == 2:
                state = PrinterState.UPLOADING
            else:
                state = PrinterState.IDLE

            # Elapsed and total, never the remainder. Seconds on firmware
            # V1.4.49 (TotalTicks 6953 for a 1h56m slice); a total longer than
            # ten days can only be milliseconds. Absent either, the time left
            # is unknown, which is not the same as zero.
            done, total = print_info.get("CurrentTicks"), print_info.get("TotalTicks")
            remaining = None
            if isinstance(done, (int, float)) and isinstance(total, (int, float)):
                if total > done:
                    scale = 1000 if total > 10 * 24 * 3600 else 1
                    remaining = int((total - done) / scale)

            return PrinterTelemetry(
                printer_name=self.name,
                brand=self.capabilities.brand,
                ip_address=self.ip,
                state=state,
                substate_code=print_info.get("Status"),
                nozzle_temp=float(res.get("TempOfNozzle") or 0.0),
                nozzle_target=float(res.get("TempTargetNozzle") or 0.0),
                bed_temp=float(res.get("TempOfHotbed") or 0.0),
                bed_target=float(res.get("TempTargetHotbed") or 0.0),
                chamber_temp=float(res.get("TempOfBox") or 0.0),
                current_layer=int(print_info.get("CurrentLayer") or 0),
                total_layers=int(print_info.get("TotalLayer") or 0),
                progress_percent=float(print_info.get("Progress") or 0.0),
                current_file=print_info.get("Filename") or None,
                time_remaining_seconds=remaining,
                loaded_spools=loaded,
                raw_telemetry=res
            )
        except Exception:
            return PrinterTelemetry(
                printer_name=self.name,
                brand=self.capabilities.brand,
                ip_address=self.ip,
                state=PrinterState.DISCONNECTED,
                loaded_spools=loaded
            )

    async def send_gcode(self, gcode: str) -> bool:
        """Sends raw G-code command to printer."""
        if self.is_cc2_family:
            try:
                async with self._cc2(enable_control=True) as p:
                    res = await p._cc2_request(1010, {"gcode": gcode})
                return res.get("error_code") == 0
            except Exception:
                return False
        
        try:
            res = await self._send_sdcp(cmd=1010, data={"Gcode": gcode, "gcode": gcode}, from_type=2)
            return res.get("Ack", 0) == 0
        except Exception:
            return False

    async def upload_job(self, gcode_path: Path) -> str:
        """
        Transmits chunked .gcode ZIP container via POST /uploadFile/upload or CC2 upload.
        Returns the remote path on printer (e.g. /local/<filename>).
        """
        if not gcode_path.exists():
            raise FileNotFoundError(f"Job file not found: {gcode_path}")

        if self.is_cc2_family:
            async with self._cc2(enable_control=True) as p:
                remote_name = await p.upload_file(gcode_path)
            _remember_thumb(gcode_path, remote_name)
            return f"/local/{remote_name}"

        file_bytes = gcode_path.read_bytes()
        total_size = len(file_bytes)
        chunk_size = 1048576  # 1 MB
        file_md5 = hashlib.md5(file_bytes).hexdigest()
        session_uuid = uuid.uuid4().hex
        num_chunks = math.ceil(total_size / chunk_size)
        filename = gcode_path.name
        url = f"http://{self.ip}:{self.port}/uploadFile/upload"

        def do_upload():
            for i in range(num_chunks):
                offset = i * chunk_size
                chunk = file_bytes[offset:offset + chunk_size]
                fields = {"S-File-MD5": file_md5, "Check": "1", "Offset": str(offset),
                          "Uuid": session_uuid, "TotalSize": str(total_size)}
                resp = requests.post(url, data=fields,
                                     files={"File": (filename, chunk, "application/octet-stream")},
                                     timeout=30)
                ok = resp.status_code == 200
                try:
                    ok = ok and bool(resp.json().get("success", True))
                except ValueError:
                    pass
                if not ok:
                    raise RuntimeError(f"the printer refused part {i + 1} of {num_chunks} "
                                       f"of the upload (HTTP {resp.status_code}: {resp.text[:120]})")

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, do_upload)

        remote = f"/local/{filename}"
        names: set = set()
        for _ in range(12):
            listing = await self._send_sdcp(cmd=258, data={"Url": "/local"})
            names = {f.get("name") for f in (listing.get("FileList") or []) if isinstance(f, dict)}
            if not names or remote in names:
                break
            await asyncio.sleep(1.5)
        if names and remote not in names:
            raise RuntimeError("the upload finished but the file is not on the printer")
        _remember_thumb(gcode_path, filename)
        return remote

    @staticmethod
    async def _await_print_commit(p, timeout_s: float = 480.0) -> bool:
        """Hold the (self-pinging) CC2 connection open until the print is
        self-sustaining, then it is safe to close.

        The genuine slicer never disconnects after 1020; the firmware needs the
        client present to run its check-and-level-and-heat sequence. We keep the
        connection alive by staying inside pycentauri's session (its ping loop
        pings while `p` is open, exactly as the slicer's 10s pings do) and poll
        until the print is ACTUALLY PRINTING — current_layer >= 1 — which proves
        the levelling sweep finished and the job committed. Closing before that
        (e.g. the instant the M104 S140 pre-heat raises the nozzle target, which
        happens BEFORE the sweep) risks the same abort as closing immediately,
        so the pre-heat is a progress sign, not the release. Returns True once
        printing, False on timeout. Never raises: a dropped poll during the
        sweep (the firmware stops answering 1002 mid-mesh) is not a failure — the
        ping loop keeps the connection alive regardless.

        The timeout must clear the FULL-PLATE mesh, which is slow: measured
        2026-09-14, a genuine ElegooSlicer print on this CC2 (its firmware has no
        adaptive/partial mesh yet) took 331s from 1020 to its second layer. A
        first attempt with a 300s window closed the connection ~30s early, mid-
        sweep, and re-stalled the print for 8+ minutes. 480s gives a comfortable
        margin over the observed 331s; the a3d print manifest's tool timeout is
        raised to match so the bus does not kill the subprocess mid-hold.
        """
        import asyncio as _asyncio
        deadline = _asyncio.get_event_loop().time() + timeout_s
        while _asyncio.get_event_loop().time() < deadline:
            try:
                st = await p.status()
                raw = getattr(st, "raw", {}) or {}
                pi = raw.get("PrintInfo", {}) or {}
                layer = pi.get("CurrentLayer") or 0
                if layer and layer >= 1:
                    return True          # first layer down -> self-sustaining
            except Exception:
                pass                     # a dropped poll mid-sweep is expected
            await _asyncio.sleep(4)
        return False

    async def start_print(self, remote_path: str, auto_level: bool = True) -> bool:
        """Sends Cmd 128 (SDCP) or Method 1020 (CC2) to initiate print execution."""
        clean_filename = remote_path.replace("/local/", "")
        if self.is_cc2_family:
            try:
                async with self._cc2(enable_control=True) as p:
                    # Method 1020 needs the full `config` block the ElegooSlicer
                    # send-dialog ships. Captured off the wire 2026-09-12 (Auto-
                    # leveling ON): pycentauri sends only {filename, storage_media}
                    # and drops this block, so the printer skips its pre-print
                    # check-and-level sequence and drives the nozzle into an
                    # un-mapped bed — it gouged a plate before this was known.
                    # `printer_check` is what runs that sequence (the dialog's
                    # "Auto-leveling" toggle); it is not optional for a remote print
                    # nobody is standing over. These are the exact keys and values
                    # the real slicer sent.
                    params = {
                        "config": {
                            "bedlevel_force": False,
                            "delay_video": False,
                            "print_layout": "A",
                            "printer_check": bool(auto_level),
                            "slot_map": [],
                        },
                        "filename": clean_filename,
                        "storage_media": "local",
                    }
                    res = await p._cc2_request(1020, params, timeout=90.0)
                    ec = 0
                    if isinstance(res, dict):
                        ec = res.get("error_code", res.get("Data", {}).get("error_code", 0)) or 0
                    if ec != 0:
                        return False
                    # Do NOT hang up now. Captured off the wire 2026-09-14 against a
                    # genuine ElegooSlicer print that completed: after 1020 the
                    # slicer HOLDS the MQTT connection open and pings every ~10s all
                    # the way through levelling -> heat -> printing. Our old
                    # `await p.close()` here dropped the connection the instant the
                    # command was ack'd, and the firmware read that disconnect as an
                    # abort: the nozzle target stayed 0, nothing heated, and it hung
                    # in AUTO_LEVELING for minutes (two power-cycles). The 1020 body
                    # was byte-identical to the slicer's the whole time — the bug was
                    # hanging up, not the command. So keep the (self-pinging)
                    # connection alive until the print has COMMITTED — the nozzle
                    # target rises off 0 and it starts heating — then it is
                    # self-sustaining and we can close.
                    committed = await self._await_print_commit(p)
                return committed
            except Exception:
                return False

        payload = {
            "Filename": remote_path,
            "StartLayer": 0,
            "Calibration_switch": 1 if auto_level else 0,
            "PrintPlatformType": 1,  # Textured PEI
            "Tlp_Switch": 0
        }
        res = await self._send_sdcp(cmd=128, data=payload, from_type=2)
        ack = (res or {}).get("Ack")
        if ack == 0:
            return True
        reasons = {1: "it is busy with another job", 2: "it could not find the uploaded file",
                   3: "the uploaded file failed its checksum", 4: "it could not read the uploaded file",
                   5: "the file does not match this printer's resolution",
                   6: "it does not recognise the file format",
                   7: "the file was prepared for a different machine model"}
        if ack is None:
            raise RuntimeError(f"it did not answer the start command ({res!r})"[:200])
        raise RuntimeError(f"it refused to start: {reasons.get(ack, f'code {ack}')}")

    async def pause_print(self) -> bool:
        if self.is_cc2_family:
            try:
                async with self._cc2(enable_control=True) as p:
                    await p.pause()
                return True
            except Exception:
                return False
        res = await self._send_sdcp(cmd=129, data={}, from_type=2)
        return res.get("Ack") == 0

    async def resume_print(self) -> bool:
        if self.is_cc2_family:
            try:
                async with self._cc2(enable_control=True) as p:
                    await p.resume()
                return True
            except Exception:
                return False
        res = await self._send_sdcp(cmd=131, data={}, from_type=2)
        return res.get("Ack") == 0

    async def stop_print(self) -> bool:
        if self.is_cc2_family:
            try:
                async with self._cc2(enable_control=True) as p:
                    await p.stop()
                return True
            except Exception:
                return False
        res = await self._send_sdcp(cmd=130, data={}, from_type=2)
        return res.get("Ack") == 0

    async def set_light(self, state: bool) -> bool:
        return await self.set_chamber_light(state)

    async def set_chamber_light(self, state: bool, brightness: int = 100) -> bool:
        if self.is_cc2_family:
            try:
                async with self._cc2(enable_control=True) as p:
                    await p.set_light(state)
                return True
            except Exception:
                return False

        val = 1 if state else 0
        payload = {
            "LightStatus": {
                "FirstLight": val,
                "SecondLight": val,
                "RgbLight": [255, 255, 255] if state else [0, 0, 0]
            }
        }
        res = await self._send_sdcp(cmd=403, data=payload, from_type=2)
        return res.get("Ack") == 0

    #: The CC2's live print-speed modes, name -> firmware value. Changing this
    #: is honoured MID-PRINT, exactly like the printer's own screen.
    SPEED_MODES = {"silent": 0, "balanced": 1, "sport": 2, "ludicrous": 3}

    async def set_speed_mode(self, mode) -> bool:
        """Set the live print-speed mode. Accepts a name (silent/balanced/sport/
        ludicrous) or its number (0-3). CC2 uses ElegooLink method 1031 via
        pycentauri, which the firmware applies to the print already running.

        Returns False for a printer whose protocol has no verified speed command
        (the SDCP CC1 today) — the caller turns that into a clear message rather
        than guessing a command onto real hardware.
        """
        if self.is_cc2_family:
            try:
                async with self._cc2(enable_control=True) as p:
                    await p.set_print_speed(mode)   # name or int; pycentauri validates
                return True
            except Exception:
                return False
        # SDCP: command 403 with PrintSpeedPct, the Centauri Carbon's own modes
        # (silent 50, balanced 100, sport 130, ludicrous 160). Measured on a
        # live print, 2026-09-29: acknowledged and read back as set.
        names = ["silent", "balanced", "sport", "ludicrous"]
        pcts = {"silent": 50, "balanced": 100, "sport": 130, "ludicrous": 160}
        raw = str(mode).strip().lower()
        name = raw if raw in pcts else (names[int(raw)] if raw in ("0", "1", "2", "3") else None)
        if name is None:
            return False
        reply = await self._send_sdcp(cmd=403, data={"PrintSpeedPct": pcts[name]}, from_type=2)
        if reply.get("Ack") != 0:
            return False
        for _ in range(4):
            await asyncio.sleep(0.6)
            status = await self._send_sdcp(cmd=0, data={}, want_status=True)
            now = (status.get("PrintInfo") or {}).get("PrintSpeedPct")
            if now == pcts[name]:
                return True
        return True

    async def list_files(self) -> List[Dict[str, Any]]:
        res = await self._send_sdcp(cmd=258, data={"Url": "/local"}, from_type=1)
        return res.get("FileList", [])

    async def set_camera(self, on: bool) -> Optional[str]:
        """Switch the chamber camera stream; the URL it streams at, or None.

        SDCP command 386 answers with `VideoUrl` (host:3031/video, MJPEG). The
        stream sends nothing until it has been switched on. The Centauri
        Carbon 2 is not reached this way.
        """
        if self.is_cc2_family:
            # The Carbon 2 serves MJPEG on 8080 (pycentauri's CAMERA_PORT_CC2)
            # with no command to switch it; the stream is used only if it answers.
            if not on:
                return None
            url = f"http://{self.ip}:8080/video"

            def answers() -> bool:
                try:
                    with requests.get(url, stream=True, timeout=3.0) as resp:
                        return resp.status_code == 200 and bool(next(resp.iter_content(512), b""))
                except Exception:
                    return False

            return url if await asyncio.get_event_loop().run_in_executor(None, answers) else None
        reply = await self._send_sdcp(cmd=386, data={"Enable": 1 if on else 0})
        if reply.get("Ack") not in (0, None) or not on:
            return None
        url = str(reply.get("VideoUrl") or f"{self.ip}:3031/video")
        return url if url.startswith("http") else f"http://{url}"

    async def get_camera_snapshot(self) -> Optional[bytes]:
        # Centauri Carbon / Elegoo IP camera snapshot endpoint
        cam_url = f"http://{self.ip}:8080/?action=snapshot"
        try:
            loop = asyncio.get_event_loop()
            resp = await loop.run_in_executor(None, lambda: requests.get(cam_url, timeout=3.0))
            if resp.status_code == 200:
                return resp.content
        except Exception:
            pass
        return None
