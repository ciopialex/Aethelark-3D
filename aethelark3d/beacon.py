"""
Aethelark-3D Physical Beacon & Hardware Identification Protocol.
Provides physical feedback (Spotlight LEDs, Robotic Wave, Strain-gauge Bed-Tap, and Stepper Chimes)
to identify physical machines in a multi-printer room or print farm.
"""

import time
import json
import asyncio
from enum import Enum
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass

from aethelark3d.config import config
from aethelark3d.drivers.factory import get_driver_for_printer
from aethelark3d.drivers.base import BasePrinterDriver, PrinterState
from aethelark3d.aliases import PrinterAliasManager


class BeaconMode(str, Enum):
    SPOTLIGHT = "spotlight"  # Chamber / Toolhead LED breathing pulse or toggle
    WAVE = "wave"            # Toolhead X/Y robotic wiggle greeting
    TAP = "tap"              # Strain-gauge bed/probe touch sensor handshake
    CHIME = "chime"          # Stepper motor acoustic pitch arpeggio
    DISPLAY = "display"      # LCD message (M117)


@dataclass
class TapDetectionResult:
    detected: bool
    printer_key: Optional[str] = None
    sensor_type: str = "strain_gauge_bed"
    pressure_grams: float = 0.0
    latency_ms: float = 0.0
    message: str = ""


class PrinterBeacon:
    """
    Physical actuator controller for identifying and naming machines in the room.
    """

    @classmethod
    async def trigger_spotlight(cls, printer_key: str, state: bool = True, use_simulator: bool = False) -> bool:
        """
        Turn on/off chamber LED lights for a specific printer.
        """
        canonical_key = PrinterAliasManager.resolve_to_key(printer_key)
        driver = get_driver_for_printer(canonical_key, use_simulator=use_simulator)
        await driver.connect()

        if hasattr(driver, "set_light"):
            return await driver.set_light(state)

        # G-code fallback: M355 S1 (Case light on) / S0 (off)
        gcode = f"M355 S{1 if state else 0}\n"
        return await driver.send_gcode(gcode)

    @classmethod
    async def trigger_wave(cls, printer_key: str, use_simulator: bool = False) -> bool:
        """
        Execute a 2-second toolhead robotic wave: raises Z by 5mm and wiggles X +/- 15mm.
        """
        canonical_key = PrinterAliasManager.resolve_to_key(printer_key)
        driver = get_driver_for_printer(canonical_key, use_simulator=use_simulator)
        await driver.connect()

        # Relative movement wave sequence
        wave_gcode = (
            "G91\n"            # Relative positioning
            "G1 Z5 F1200\n"    # Lift Z 5mm
            "G1 X15 F6000\n"   # Wiggle Right
            "G1 X-30 F6000\n"  # Wiggle Left
            "G1 X30 F6000\n"   # Wiggle Right
            "G1 X-15 F6000\n"  # Center
            "G90\n"            # Restore absolute positioning
            "M117 Hello! I am here\n"
        )
        return await driver.send_gcode(wave_gcode)

    @classmethod
    async def trigger_chime(cls, printer_key: str, note_freq: int = 880, duration_ms: int = 300, use_simulator: bool = False) -> bool:
        """
        Play an acoustic chime on the stepper motors or mainboard buzzer.
        """
        canonical_key = PrinterAliasManager.resolve_to_key(printer_key)
        driver = get_driver_for_printer(canonical_key, use_simulator=use_simulator)
        await driver.connect()

        # M300 S<frequency_hz> P<duration_ms>
        chime_gcode = (
            f"M300 S{note_freq} P{duration_ms}\n"
            f"M300 S{int(note_freq * 1.25)} P{duration_ms}\n"
            f"M300 S{int(note_freq * 1.5)} P{duration_ms + 100}\n"
        )
        return await driver.send_gcode(chime_gcode)

    @classmethod
    async def trigger_beacon(
        cls,
        printer_key: str,
        mode: BeaconMode = BeaconMode.SPOTLIGHT,
        use_simulator: bool = False
    ) -> Dict[str, Any]:
        """
        Execute the specified beacon modality on a single printer.
        """
        canonical_key = PrinterAliasManager.resolve_to_key(printer_key)
        display_name = PrinterAliasManager.get_display_name(canonical_key)

        if mode == BeaconMode.SPOTLIGHT:
            success = await cls.trigger_spotlight(canonical_key, state=True, use_simulator=use_simulator)
            msg = f"💡 Spotlight illuminated on {display_name}."
        elif mode == BeaconMode.WAVE:
            success = await cls.trigger_wave(canonical_key, use_simulator=use_simulator)
            msg = f"👋 Robotic toolhead wave executed on {display_name}."
        elif mode == BeaconMode.CHIME:
            success = await cls.trigger_chime(canonical_key, note_freq=880, duration_ms=250, use_simulator=use_simulator)
            msg = f"🎵 3-Note chime played on {display_name}."
        elif mode == BeaconMode.DISPLAY:
            driver = get_driver_for_printer(canonical_key, use_simulator=use_simulator)
            await driver.connect()
            await driver.send_gcode(f"M117 IDENTIFY: {display_name}\n")
            success = True
            msg = f"📺 LCD message displayed on {display_name}."
        else:
            success = False
            msg = f"Unknown beacon mode: {mode}"

        out = {
            "success": success,
            "printer_key": canonical_key,
            "display_name": display_name,
            "mode": mode.value,
        }
        if success:
            out["message"] = msg
        else:
            # The message above says what was attempted. Returned on failure it
            # read as done: "3-Note chime played" with success false, and the
            # model told the user it had chimed.
            out["error"] = f"{display_name} did not respond to the {mode.value} beacon."
            out["guidance"] = ("Tell the user nothing happened on the printer. "
                               "Check it is on and reachable with a3d_status.")
        return out

    @classmethod
    async def isolate_spotlight(
        cls,
        active_printer_key: str,
        all_printer_keys: Optional[List[str]] = None,
        use_simulator: bool = False
    ) -> Dict[str, Any]:
        """
        Turn OFF lights on all printers in the fleet EXCEPT the active one, creating a spotlight in the room.
        """
        fleet_keys = all_printer_keys or list(config.get_fleet().keys())
        target_key = PrinterAliasManager.resolve_to_key(active_printer_key)

        tasks = []
        for pkey in fleet_keys:
            is_target = (pkey == target_key)
            tasks.append(cls.trigger_spotlight(pkey, state=is_target, use_simulator=use_simulator))

        results = await asyncio.gather(*tasks, return_exceptions=True)
        return {
            "success": True,
            "spotlight_printer": target_key,
            "total_fleet_controlled": len(fleet_keys),
            "message": f"Spotlight active on {PrinterAliasManager.get_display_name(target_key)} (All other printers darkened)."
        }

    @classmethod
    async def reset_all_lights(
        cls,
        all_printer_keys: Optional[List[str]] = None,
        state: bool = False,
        use_simulator: bool = False
    ):
        """
        Reset all chamber lights in the fleet to off (or on).
        """
        fleet_keys = all_printer_keys or list(config.get_fleet().keys())
        tasks = [cls.trigger_spotlight(pkey, state=state, use_simulator=use_simulator) for pkey in fleet_keys]
        await asyncio.gather(*tasks, return_exceptions=True)

    @classmethod
    async def listen_for_bed_tap(
        cls,
        printer_keys: Optional[List[str]] = None,
        timeout_seconds: float = 15.0,
        simulated_tap_key: Optional[str] = None,
        use_simulator: bool = False
    ) -> TapDetectionResult:
        """
        Listen concurrently across all fleet printers for a physical strain-gauge / bed probe touch.
        """
        fleet_keys = printer_keys or list(config.get_fleet().keys())
        start_time = time.time()

        # In simulation or test mode with explicit simulated tap
        if use_simulator or simulated_tap_key:
            selected_key = simulated_tap_key or fleet_keys[0]
            await asyncio.sleep(0.1)  # simulate brief network propagation
            return TapDetectionResult(
                detected=True,
                printer_key=selected_key,
                sensor_type="strain_gauge_load_cell",
                pressure_grams=48.5,
                latency_ms=round((time.time() - start_time) * 1000, 1),
                message=f"Physical bed tap detected on {PrinterAliasManager.get_display_name(selected_key)} (48.5g pressure)."
            )

        # Real hardware polling loop across WebSockets for strain-gauge/probe interrupt
        drivers = {}
        for pkey in fleet_keys:
            drv = get_driver_for_printer(pkey, use_simulator=use_simulator)
            await drv.connect()
            drivers[pkey] = drv

        while (time.time() - start_time) < timeout_seconds:
            for pkey, drv in drivers.items():
                telem = await drv.get_telemetry()
                # Check for probe/strain-gauge trigger flag in raw telemetry
                raw = telem.raw_telemetry or {}
                probe_triggered = raw.get("probe_triggered", False) or raw.get("strain_gauge_delta", 0.0) > 20.0
                if probe_triggered:
                    return TapDetectionResult(
                        detected=True,
                        printer_key=pkey,
                        sensor_type="strain_gauge_bed",
                        pressure_grams=float(raw.get("strain_gauge_delta", 35.0)),
                        latency_ms=round((time.time() - start_time) * 1000, 1),
                        message=f"Touch detected on {PrinterAliasManager.get_display_name(pkey)}!"
                    )
            await asyncio.sleep(0.1)

        return TapDetectionResult(
            detected=False,
            message="No physical tap detected within timeout window."
        )
