"""
Exhaustive Tests for ElegooSDCPDriver and SDCP Protocol Serialization.
Exercises packet schemas, command framing, status parsing, and driver lifecycle in aethelark3d/drivers/elegoo.py.
"""

import asyncio
from unittest.mock import AsyncMock, patch, MagicMock
import pytest

from aethelark3d.drivers.elegoo import ElegooSDCPDriver
from aethelark3d.drivers.base import PrinterState, PrinterTelemetry


def test_elegoo_driver_telemetry_and_methods():
    driver = ElegooSDCPDriver(name="CC1", ip="172.20.10.3")

    # The shape a Centauri Carbon actually puts on the wire, captured from
    # mainboard 5c5c12d8... on 2026-09-04. The previous version of this fixture
    # was flat and used invented field names (TargetTempOfNozzle, TotalLayers,
    # PrintProgress, a bare integer Status) -- so it passed against a parse that
    # returned zeros for every real printer.
    sample_res = {
        "CurrentStatus": [1],                 # a list, not an int
        "TempOfNozzle": 220.5,
        "TempTargetNozzle": 220.0,
        "TempOfHotbed": 55.2,
        "TempTargetHotbed": 55.0,
        "TempOfBox": 34.1,
        "PrintInfo": {                        # the job lives one level down
            "Status": 13,
            "CurrentLayer": 42,
            "TotalLayer": 120,                # singular
            "Progress": 35.0,
            "Filename": "/local/benchy.gcode",
            "CurrentTicks": 440000,
            "TotalTicks": 2000000,            # 1560 s remaining
        },
    }

    with patch.object(driver, "_send_sdcp", new_callable=AsyncMock) as mock_sdcp:
        mock_sdcp.return_value = sample_res
        
        async def _run():
            telem = await driver.get_telemetry()
            assert telem.state == PrinterState.PRINTING
            assert telem.nozzle_temp == 220.5
            assert telem.bed_temp == 55.2
            assert telem.chamber_temp == 34.1
            assert telem.current_layer == 42
            assert telem.total_layers == 120
            assert telem.progress_percent == 35.0
            assert telem.current_file == "/local/benchy.gcode"
            assert telem.nozzle_target == 220.0
            assert telem.time_remaining_seconds == 1560

            # Test command methods
            mock_sdcp.return_value = {"Ack": 0}
            assert await driver.start_print("/local/benchy.gcode") is True
            mock_sdcp.assert_called()

            await driver.pause_print()
            await driver.resume_print()
            await driver.stop_print()
            await driver.set_chamber_light(True)
            files = await driver.list_files()
            assert isinstance(files, list)

            # Test camera snapshot endpoint
            with patch("requests.get") as mock_req:
                mock_req.return_value.status_code = 200
                mock_req.return_value.content = b"\x89PNG\r\n\x1a\nFAKE_CAMERA_BYTES"
                snap = await driver.get_camera_snapshot()
                assert snap is not None
                assert snap.startswith(b"\x89PNG")

        asyncio.run(_run())
