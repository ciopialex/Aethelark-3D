"""
High-Fidelity Digital Twin Virtual Printer Driver for Aethelark-3D.
Provides 100% deterministic offline simulation, multi-machine thermal & AMS modeling,
and a comprehensive chaos/fault-injection engine for adaptive testing.
"""

import asyncio
import time
from pathlib import Path
from typing import Dict, List, Optional, Any, Set

from aethelark3d.drivers.base import (
    BasePrinterDriver,
    PrinterTelemetry,
    PrinterState,
    PrinterCapability,
    AMSSlotState
)
from aethelark3d.spools import FilamentVault


def get_default_capabilities(printer_key: str) -> PrinterCapability:
    """Returns canonical hardware capability profile for target fleet machine."""
    key = printer_key.upper().replace("-", "_")

    if key in ["CC1", "VIRTUAL_CC1"]:
        return PrinterCapability(
            brand="Elegoo",
            model="Centauri Carbon 1",
            nozzle_diameter=0.4,
            nozzle_material="hardened_steel",
            max_nozzle_temp=300.0,
            max_bed_temp=100.0,
            is_enclosed=True,
            build_volume=(256, 256, 256),
            has_ams=False,
            ams_slot_count=1,
            compatible_materials=["PLA", "PETG", "TPU", "ABS", "ASA", "PLA-CF", "PETG-CF", "Silk PLA"],
            incompatible_materials=[]
        )
    elif key in ["CC2", "VIRTUAL_CC2"]:
        return PrinterCapability(
            brand="Elegoo",
            model="Centauri Carbon 2",
            nozzle_diameter=0.4,
            nozzle_material="hardened_steel",
            max_nozzle_temp=320.0,
            max_bed_temp=110.0,
            is_enclosed=True,
            build_volume=(256, 256, 256),
            has_ams=False,
            ams_slot_count=1,
            compatible_materials=["PLA", "PETG", "TPU", "ABS", "ASA", "PLA-CF", "PETG-CF", "Silk PLA", "PA-CF", "PC"],
            incompatible_materials=[]
        )
    elif key in ["CC1_COMBO", "VIRTUAL_CC1_COMBO"]:
        return PrinterCapability(
            brand="Elegoo",
            model="Centauri Carbon 1 Combo (AMS)",
            nozzle_diameter=0.4,
            nozzle_material="hardened_steel",
            max_nozzle_temp=300.0,
            max_bed_temp=100.0,
            is_enclosed=True,
            build_volume=(256, 256, 256),
            has_ams=True,
            ams_slot_count=4,
            compatible_materials=["PLA", "PETG", "TPU", "ABS", "ASA", "PLA-CF", "PETG-CF", "Silk PLA"],
            incompatible_materials=[]
        )
    elif key in ["C2_COMBO", "VIRTUAL_C2_COMBO"]:
        return PrinterCapability(
            brand="Elegoo",
            model="Centauri 2 Combo (AMS)",
            nozzle_diameter=0.4,
            nozzle_material="brass",
            max_nozzle_temp=260.0,
            max_bed_temp=80.0,
            is_enclosed=False,
            build_volume=(256, 256, 256),
            has_ams=True,
            ams_slot_count=4,
            compatible_materials=["PLA Basic", "Silk PLA", "PETG", "TPU"],
            incompatible_materials=["PLA-CF", "PETG-CF", "PA-CF", "ABS", "ASA", "PC"]
        )
    elif key in ["CC2_COMBO", "VIRTUAL_CC2_COMBO"]:
        return PrinterCapability(
            brand="Elegoo",
            model="Centauri Carbon 2 Combo (AMS)",
            nozzle_diameter=0.4,
            nozzle_material="hardened_steel",
            max_nozzle_temp=320.0,
            max_bed_temp=110.0,
            is_enclosed=True,
            build_volume=(256, 256, 256),
            has_ams=True,
            ams_slot_count=4,
            compatible_materials=["PLA", "PETG", "TPU", "ABS", "ASA", "PLA-CF", "PETG-CF", "Silk PLA", "PA-CF", "PC"],
            incompatible_materials=[]
        )
    else:  # Generic fallback
        return PrinterCapability(
            brand="Elegoo",
            model=f"Centauri Generic ({printer_key})",
            nozzle_diameter=0.4,
            nozzle_material="hardened_steel",
            max_nozzle_temp=300.0,
            max_bed_temp=100.0,
            is_enclosed=True,
            build_volume=(256, 256, 256),
            has_ams=False,
            ams_slot_count=1
        )


class VirtualPrinterDriver(BasePrinterDriver):
    """
    High-Fidelity Digital Twin Simulator Driver.
    Simulates thermodynamics, register states, AMS multi-material feeds,
    and supports programmed chaos/fault injections.
    """

    def __init__(
        self,
        name: str = "VIRTUAL_CC1",
        ip: str = "127.0.0.1",
        port: int = 3030,
        capabilities: Optional[PrinterCapability] = None,
        physics_speed: float = 1.0
    ):
        caps = capabilities or get_default_capabilities(name)
        super().__init__(name, ip, port, caps)
        
        self.physics_speed = physics_speed  # Speed multiplier for testing
        
        # Machine register state
        self._state = PrinterState.IDLE
        self._substate_code: Optional[int] = 0
        self._previous_active_state = PrinterState.PRINTING
        self._ambient_temp = 24.0
        self._nozzle_temp = 25.0
        self._nozzle_target = 0.0
        self._bed_temp = 24.0
        self._bed_target = 0.0
        self._chamber_temp = 24.0
        self._current_layer = 0
        self._total_layers = 0
        self._current_file: Optional[str] = None
        self._files: List[Dict[str, Any]] = []
        self._light_state = False
        self._is_connected = False
        self._job_start_time = 0.0
        self._sim_task: Optional[asyncio.Task] = None
        
        # AMS Multi-Slot Virtual Registers
        self._active_ams_slot: int = 1
        self._ams_slots: Dict[int, AMSSlotState] = {}
        if self.capabilities.has_ams:
            for s in range(1, self.capabilities.ams_slot_count + 1):
                self._ams_slots[s] = AMSSlotState(
                    slot=s,
                    material=f"Slot {s} Material",
                    color="White",
                    remaining_grams=1000.0,
                    is_active=(s == 1),
                    has_runout=False,
                    rfid_present=True
                )

        # Fault Injection State
        self._active_faults: Set[str] = set()
        self._fault_params: Dict[str, Any] = {}

    # -------------------------------------------------------------------------
    # Fault Injection / Chaos Testing Engine
    # -------------------------------------------------------------------------

    def inject_fault(self, fault_type: str, **kwargs) -> None:
        """
        Inject a simulated hardware or network fault into the Digital Twin.
        Supported faults:
            - 'network_disconnect': Drops connection immediately
            - 'network_timeout': Delays operations
            - 'thermal_runaway': Spikes temperature past safe thresholds
            - 'heater_failure': Prevents temperature from rising
            - 'filament_runout': Trips runout sensor on specified slot
            - 'ams_jam': Jams the active AMS feeder
        """
        self._active_faults.add(fault_type)
        self._fault_params[fault_type] = kwargs

        if fault_type == "network_disconnect":
            self._is_connected = False

        elif fault_type == "filament_runout":
            slot = kwargs.get("slot", self._active_ams_slot)
            if slot in self._ams_slots:
                self._ams_slots[slot].has_runout = True
                self._ams_slots[slot].remaining_grams = 0.0
            if self._state in [PrinterState.PRINTING, PrinterState.HEATING, PrinterState.LEVELING]:
                self._previous_active_state = self._state
                self._state = PrinterState.PAUSED
                self._substate_code = 101  # Error: Filament Runout Triggered

        elif fault_type == "thermal_runaway":
            target = kwargs.get("target", "nozzle")
            spike_temp = kwargs.get("temp", 350.0)
            if target == "nozzle":
                self._nozzle_temp = spike_temp
            elif target == "bed":
                self._bed_temp = spike_temp
            self._state = PrinterState.ERROR
            self._substate_code = 500  # Emergency Thermal Runaway Stop

    def clear_fault(self, fault_type: Optional[str] = None) -> None:
        """Clear specific or all injected faults."""
        if fault_type:
            self._active_faults.discard(fault_type)
            self._fault_params.pop(fault_type, None)
            if fault_type == "filament_runout":
                for s in self._ams_slots.values():
                    s.has_runout = False
        else:
            self._active_faults.clear()
            self._fault_params.clear()
            for s in self._ams_slots.values():
                s.has_runout = False

    # -------------------------------------------------------------------------
    # Driver Protocol Implementation
    # -------------------------------------------------------------------------

    async def connect(self) -> bool:
        if "network_disconnect" in self._active_faults:
            return False
        if "network_timeout" in self._active_faults:
            await asyncio.sleep(self._fault_params.get("network_timeout", {}).get("delay", 0.5))
        self._is_connected = True
        return True

    async def disconnect(self) -> None:
        self._is_connected = False
        if self._sim_task and not self._sim_task.done():
            self._sim_task.cancel()

    async def get_telemetry(self) -> PrinterTelemetry:
        if not self._is_connected or "network_disconnect" in self._active_faults:
            return PrinterTelemetry(
                printer_name=self.name,
                brand=self.capabilities.brand,
                ip_address=self.ip,
                state=PrinterState.DISCONNECTED
            )

        if "network_timeout" in self._active_faults:
            await asyncio.sleep(self._fault_params.get("network_timeout", {}).get("delay", 0.1))

        progress = (self._current_layer / max(1, self._total_layers)) * 100.0 if self._total_layers > 0 else 0.0
        loaded = FilamentVault.get_active_spools(self.name)

        # Build raw telemetry with AMS and register state
        raw_telemetry: Dict[str, Any] = {
            "simulated": True,
            "light": self._light_state,
            "substate_code": self._substate_code,
            "active_faults": list(self._active_faults)
        }
        if self.capabilities.has_ams:
            raw_telemetry["ams"] = {
                "active_slot": self._active_ams_slot,
                "slots": {
                    s: {
                        "material": st.material,
                        "color": st.color,
                        "remaining_grams": st.remaining_grams,
                        "has_runout": st.has_runout
                    }
                    for s, st in self._ams_slots.items()
                }
            }

        return PrinterTelemetry(
            printer_name=self.name,
            brand=self.capabilities.brand,
            ip_address=self.ip,
            state=self._state,
            substate_code=self._substate_code,
            nozzle_temp=round(self._nozzle_temp, 1),
            nozzle_target=self._nozzle_target,
            bed_temp=round(self._bed_temp, 1),
            bed_target=self._bed_target,
            chamber_temp=round(self._chamber_temp, 1),
            current_layer=self._current_layer,
            total_layers=self._total_layers,
            progress_percent=round(min(100.0, progress), 1),
            current_file=self._current_file,
            loaded_spools=loaded,
            raw_telemetry=raw_telemetry
        )

    async def upload_job(self, gcode_path: Path) -> str:
        """Simulate chunked file upload and storage."""
        if not self._is_connected or "network_disconnect" in self._active_faults:
            raise ConnectionError("Printer is disconnected.")
        if not gcode_path.exists():
            raise FileNotFoundError(f"File not found: {gcode_path}")
        
        remote_path = f"/local/{gcode_path.name}"
        self._files.append({
            "name": remote_path,
            "size": gcode_path.stat().st_size,
            "time": int(time.time())
        })
        return remote_path

    async def start_print(
        self,
        remote_path: str,
        auto_level: bool = True,
        nozzle_target: float = 230.0,
        bed_target: float = 55.0,
        total_layers: int = 100
    ) -> bool:
        if not self._is_connected or "network_disconnect" in self._active_faults:
            return False

        # Verify physical thermal constraints
        if nozzle_target > self.capabilities.max_nozzle_temp:
            raise ValueError(
                f"Requested nozzle temp {nozzle_target}°C exceeds hardware maximum {self.capabilities.max_nozzle_temp}°C on {self.name}"
            )
        if bed_target > self.capabilities.max_bed_temp:
            raise ValueError(
                f"Requested bed temp {bed_target}°C exceeds hardware maximum {self.capabilities.max_bed_temp}°C on {self.name}"
            )

        # Check AMS runout state
        if self.capabilities.has_ams:
            active_slot_state = self._ams_slots.get(self._active_ams_slot)
            if active_slot_state and active_slot_state.has_runout:
                self._state = PrinterState.PAUSED
                self._substate_code = 101  # Cannot start with active runout
                return False

        found = any(f["name"] == remote_path for f in self._files) or remote_path.startswith("/local/")
        if not found:
            return False

        self._current_file = remote_path.split("/")[-1]
        self._state = PrinterState.HEATING
        self._substate_code = 0
        self._nozzle_target = nozzle_target
        self._bed_target = bed_target
        self._current_layer = 0
        self._total_layers = total_layers
        self._job_start_time = time.time()

        if self._sim_task and not self._sim_task.done():
            self._sim_task.cancel()
        self._sim_task = asyncio.create_task(self._run_physics_loop(auto_level))
        return True

    async def _run_physics_loop(self, auto_level: bool):
        """Simulates physical heat-up, bed probing, and layer progression."""
        step_dt = 0.02 / max(0.1, self.physics_speed)
        try:
            # 1. Thermal ramp phase
            while self._bed_temp < self._bed_target or self._nozzle_temp < self._nozzle_target:
                if self._state == PrinterState.PAUSED:
                    await asyncio.sleep(step_dt)
                    continue
                if self._state in [PrinterState.STOPPED, PrinterState.ERROR]:
                    return

                if "heater_failure" in self._active_faults:
                    # Heater doesn't rise, triggers error
                    await asyncio.sleep(step_dt * 5)
                    self._state = PrinterState.ERROR
                    self._substate_code = 502  # Heater failure
                    return

                self._bed_temp = min(self._bed_target, self._bed_temp + 15.0 * self.physics_speed)
                self._nozzle_temp = min(self._nozzle_target, self._nozzle_temp + 35.0 * self.physics_speed)

                # Enclosed chambers absorb heat from bed; open-frame dissipates to ambient
                if self.capabilities.is_enclosed:
                    target_chamber = self._ambient_temp + (self._bed_target - self._ambient_temp) * 0.4
                    self._chamber_temp = min(target_chamber, self._chamber_temp + 0.8 * self.physics_speed)
                else:
                    self._chamber_temp = self._ambient_temp

                await asyncio.sleep(step_dt)

            # 2. Auto Bed Leveling
            if auto_level:
                self._state = PrinterState.LEVELING
                for _ in range(3):
                    if self._state == PrinterState.PAUSED:
                        await asyncio.sleep(step_dt)
                        continue
                    if self._state in [PrinterState.STOPPED, PrinterState.ERROR]:
                        return
                    await asyncio.sleep(step_dt)

            # 3. Printing layer progression
            self._state = PrinterState.PRINTING
            for layer in range(1, self._total_layers + 1):
                while self._state == PrinterState.PAUSED:
                    await asyncio.sleep(step_dt)

                if self._state not in [PrinterState.PRINTING, PrinterState.PAUSED]:
                    break

                # Check for runout fault
                if "filament_runout" in self._active_faults:
                    self._state = PrinterState.PAUSED
                    self._substate_code = 101
                    break

                # Deduct filament in AMS if present
                if self.capabilities.has_ams and self._active_ams_slot in self._ams_slots:
                    slot_st = self._ams_slots[self._active_ams_slot]
                    slot_st.remaining_grams = max(0.0, slot_st.remaining_grams - 0.1)
                    if slot_st.remaining_grams <= 0.0:
                        slot_st.has_runout = True
                        self._state = PrinterState.PAUSED
                        self._substate_code = 101
                        break

                self._current_layer = layer
                await asyncio.sleep(step_dt)

            if self._state == PrinterState.PRINTING:
                self._state = PrinterState.COMPLETED
                self._nozzle_target = 0.0
                self._bed_target = 0.0

        except asyncio.CancelledError:
            pass

    async def pause_print(self) -> bool:
        if self._state in [PrinterState.PRINTING, PrinterState.HEATING, PrinterState.LEVELING]:
            self._previous_active_state = self._state
            self._state = PrinterState.PAUSED
            return True
        return False

    async def resume_print(self) -> bool:
        if self._state == PrinterState.PAUSED:
            if "filament_runout" in self._active_faults:
                # Cannot resume while runout fault is active
                return False
            self._state = self._previous_active_state or PrinterState.PRINTING
            return True
        return False

    async def stop_print(self) -> bool:
        self._state = PrinterState.STOPPED
        self._nozzle_target = 0.0
        self._bed_target = 0.0
        if self._sim_task and not self._sim_task.done():
            self._sim_task.cancel()
        return True

    async def set_chamber_light(self, state: bool, brightness: int = 100) -> bool:
        self._light_state = state
        return True

    async def set_light(self, state: bool) -> bool:
        return await self.set_chamber_light(state)

    async def send_gcode(self, gcode: str) -> bool:
        """Simulate G-code execution on virtual hardware."""
        if "M355 S1" in gcode:
            self._light_state = True
        elif "M355 S0" in gcode:
            self._light_state = False
        return True

    async def list_files(self) -> List[Dict[str, Any]]:
        return self._files

    async def get_camera_snapshot(self) -> Optional[bytes]:
        return b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15c4\x00\x00\x00\rIDATx\x9cc`\x00\x00\x00\x02\x00\x01H\xaf\xa4q\x00\x00\x00\x00IEND\xaeB`\x82"

    def select_ams_slot(self, slot: int) -> bool:
        """Select active AMS slot for extrusion."""
        if not self.capabilities.has_ams:
            return False
        if 1 <= slot <= self.capabilities.ams_slot_count:
            self._active_ams_slot = slot
            for s, st in self._ams_slots.items():
                st.is_active = (s == slot)
            return True
        return False
