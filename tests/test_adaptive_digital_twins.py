"""
Adaptive Stress & Fault-Injection Test Suite for Aethelark-3D Fleet Digital Twins.
Tests CC1, CC2, and C2_COMBO under simulated thermal overshoots, filament runouts,
network disconnections, AMS multi-material transitions, and high-concurrency fleet workloads.
"""

import asyncio
import pytest
from pathlib import Path

from aethelark3d.drivers.factory import get_driver_for_printer
from aethelark3d.drivers.base import PrinterState, PrinterCapability
from aethelark3d.drivers.simulator import VirtualPrinterDriver


def test_fleet_thermal_boundaries_and_material_constraints():
    """Verify hardware thermal limits across CC1, CC2, and C2_COMBO."""
    async def _test():
        # 1. CC1: Enclosed, Hardened Steel (300°C Max Nozzle / 100°C Max Bed)
        cc1 = get_driver_for_printer("VIRTUAL_CC1", use_simulator=True, physics_speed=50.0)
        await cc1.connect()
        assert cc1.capabilities.max_nozzle_temp == 300.0
        assert cc1.capabilities.max_bed_temp == 100.0
        assert cc1.capabilities.is_enclosed is True
        assert cc1.capabilities.nozzle_material == "hardened_steel"

        # Start valid print
        started_cc1 = await cc1.start_print("/local/standard_pla.gcode", nozzle_target=220.0, bed_target=55.0)
        assert started_cc1 is True
        await cc1.stop_print()

        # Attempt over-temperature violation on CC1
        with pytest.raises(ValueError, match="exceeds hardware maximum 300.0°C"):
            await cc1.start_print("/local/extreme.gcode", nozzle_target=320.0, bed_target=55.0)

        # 2. CC2: Enclosed, High-Temp Hardened Steel (320°C Max Nozzle / 110°C Max Bed)
        cc2 = get_driver_for_printer("VIRTUAL_CC2", use_simulator=True, physics_speed=50.0)
        await cc2.connect()
        assert cc2.capabilities.max_nozzle_temp == 320.0
        assert cc2.capabilities.max_bed_temp == 110.0
        # High-temp engineering material (PA-CF / PC) target succeeds on CC2
        started_cc2 = await cc2.start_print("/local/pacf_bracket.gcode", nozzle_target=310.0, bed_target=105.0)
        assert started_cc2 is True
        await cc2.stop_print()

        # 3. C2_COMBO: Open Frame, Brass (260°C Max Nozzle / 80°C Max Bed)
        c2 = get_driver_for_printer("VIRTUAL_C2_COMBO", use_simulator=True, physics_speed=50.0)
        await c2.connect()
        assert c2.capabilities.max_nozzle_temp == 260.0
        assert c2.capabilities.max_bed_temp == 80.0
        assert c2.capabilities.is_enclosed is False
        assert c2.capabilities.nozzle_material == "brass"

        # Attempting high-temp or abrasive profile on C2 brass nozzle fails
        with pytest.raises(ValueError, match="exceeds hardware maximum 260.0°C"):
            await c2.start_print("/local/pacf_bracket.gcode", nozzle_target=290.0, bed_target=60.0)

    asyncio.run(_test())


def test_ams_4slot_lifecycle_and_midprint_runout():
    """Verify 4-slot AMS behavior, slot switching, and runout pause handling on C2_COMBO."""
    async def _test():
        c2_ams: VirtualPrinterDriver = get_driver_for_printer("VIRTUAL_C2_COMBO", use_simulator=True, physics_speed=50.0)
        await c2_ams.connect()
        assert c2_ams.capabilities.has_ams is True
        assert c2_ams.capabilities.ams_slot_count == 4

        # Slot switching
        assert c2_ams.select_ams_slot(2) is True
        telemetry = await c2_ams.get_telemetry()
        assert telemetry.raw_telemetry["ams"]["active_slot"] == 2
        assert telemetry.raw_telemetry["ams"]["slots"][2]["has_runout"] is False

        # Start print on Slot 2
        started = await c2_ams.start_print("/local/multicolor_art.gcode", total_layers=200)
        assert started is True

        # Allow print to begin heating/leveling
        await asyncio.sleep(0.04)

        # Incur mid-print filament runout on Slot 2
        c2_ams.inject_fault("filament_runout", slot=2)
        await asyncio.sleep(0.03)

        telemetry_fault = await c2_ams.get_telemetry()
        assert telemetry_fault.state == PrinterState.PAUSED
        assert telemetry_fault.substate_code == 101  # Filament runout substate
        assert telemetry_fault.raw_telemetry["ams"]["slots"][2]["has_runout"] is True

        # Resume must be blocked while runout is active
        resume_attempt = await c2_ams.resume_print()
        assert resume_attempt is False

        # Simulate spool swap / fault clear
        c2_ams.clear_fault("filament_runout")
        resumed = await c2_ams.resume_print()
        assert resumed is True

        await c2_ams.stop_print()

    asyncio.run(_test())


def test_network_chaos_and_reconnection():
    """Verify system stability when network drops or times out mid-session."""
    async def _test():
        driver: VirtualPrinterDriver = get_driver_for_printer("VIRTUAL_CC1", use_simulator=True, physics_speed=50.0)
        await driver.connect()
        assert (await driver.get_telemetry()).state == PrinterState.IDLE

        # Inject abrupt network disconnect
        driver.inject_fault("network_disconnect")
        telemetry_dc = await driver.get_telemetry()
        assert telemetry_dc.state == PrinterState.DISCONNECTED

        # Disconnected printer rejects new commands
        started = await driver.start_print("/local/test.gcode")
        assert started is False

        # Network recovers
        driver.clear_fault("network_disconnect")
        connected = await driver.connect()
        assert connected is True
        telemetry_rec = await driver.get_telemetry()
        assert telemetry_rec.state == PrinterState.IDLE

    asyncio.run(_test())


def test_thermal_runaway_emergency_shutdown():
    """Verify safety shutdown when an unexpected thermal runaway occurs."""
    async def _test():
        driver: VirtualPrinterDriver = get_driver_for_printer("VIRTUAL_CC1", use_simulator=True, physics_speed=50.0)
        await driver.connect()
        await driver.start_print("/local/part.gcode", nozzle_target=220.0, bed_target=55.0)

        # Trigger catastrophic thermal runaway on nozzle
        driver.inject_fault("thermal_runaway", target="nozzle", temp=370.0)
        telemetry = await driver.get_telemetry()
        assert telemetry.state == PrinterState.ERROR
        assert telemetry.substate_code == 500
        assert telemetry.nozzle_temp == 370.0

        await driver.stop_print()

    asyncio.run(_test())


def test_concurrent_fleet_simulation():
    """Verify concurrent execution of CC1, CC2, and C2_COMBO simultaneously."""
    async def _test():
        cc1 = get_driver_for_printer("VIRTUAL_CC1", use_simulator=True, physics_speed=50.0)
        cc2 = get_driver_for_printer("VIRTUAL_CC2", use_simulator=True, physics_speed=50.0)
        c2 = get_driver_for_printer("VIRTUAL_C2_COMBO", use_simulator=True, physics_speed=50.0)

        await asyncio.gather(cc1.connect(), cc2.connect(), c2.connect())

        # Start distinct jobs on all 3 machines concurrently
        r1, r2, r3 = await asyncio.gather(
            cc1.start_print("/local/job_cc1.gcode", nozzle_target=220.0, bed_target=55.0),
            cc2.start_print("/local/job_cc2.gcode", nozzle_target=250.0, bed_target=80.0),
            c2.start_print("/local/job_c2.gcode", nozzle_target=210.0, bed_target=50.0)
        )
        assert r1 is True
        assert r2 is True
        assert r3 is True

        await asyncio.sleep(0.08)

        t1, t2, t3 = await asyncio.gather(cc1.get_telemetry(), cc2.get_telemetry(), c2.get_telemetry())
        assert t1.printer_name == "VIRTUAL_CC1"
        assert t2.printer_name == "VIRTUAL_CC2"
        assert t3.printer_name == "VIRTUAL_C2_COMBO"
        assert t1.state in [PrinterState.HEATING, PrinterState.LEVELING, PrinterState.PRINTING]
        assert t2.state in [PrinterState.HEATING, PrinterState.LEVELING, PrinterState.PRINTING]
        assert t3.state in [PrinterState.HEATING, PrinterState.LEVELING, PrinterState.PRINTING]

        # Stop all concurrent jobs
        await asyncio.gather(cc1.stop_print(), cc2.stop_print(), c2.stop_print())

    asyncio.run(_test())
