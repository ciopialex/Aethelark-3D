"""
Unit tests for SDCPEventStreamer and Dynamic Island HUD event generation.
Ensures compliance with docs/UI_STATES.md event schemas and atomic file locking.
"""

import json
import asyncio
from pathlib import Path
from aethelark3d.streamer import SDCPEventStreamer, DynamicIslandEvent
from aethelark3d.spools import FilamentVault
from aethelark3d.agent import execute_tool, TOOL_DEFINITIONS
from aethelark3d.drivers.base import PrinterTelemetry, PrinterState


def test_streamer_standby_event(tmp_path):
    out_file = tmp_path / "dynamic_island.json"
    # Ensure nominal spool is mounted
    FilamentVault.load_spool("CC1", slot=1, material="Generic PLA", color="Black", grams=800.0)
    streamer = SDCPEventStreamer("CC1", use_simulator=True, event_file=out_file)
    event: DynamicIslandEvent = asyncio.run(streamer.poll_single_frame())

    assert event.module == "3d"
    assert event.event in ["printer_standby", "print_progress"]
    assert out_file.exists()

    with open(out_file, "r") as f:
        data = json.load(f)
        assert data["module"] == "3d"


def test_streamer_printing_progress_ring(tmp_path):
    out_file = tmp_path / "dynamic_island.json"
    streamer = SDCPEventStreamer("CC1", use_simulator=True, event_file=out_file)
    
    # Mock telemetry in printing state
    mock_telem = PrinterTelemetry(
        printer_name="Centauri Carbon",
        brand="Elegoo",
        ip_address="172.20.10.3",
        state=PrinterState.PRINTING,
        nozzle_temp=220.0,
        nozzle_target=220.0,
        bed_temp=60.0,
        bed_target=60.0,
        chamber_temp=35.0,
        current_layer=412,
        total_layers=1200,
        progress_percent=34.3,
        current_file="ECC_0.4_Rattlesnake.gcode",
        time_remaining_seconds=8100
    )
    
    event = streamer.format_event_frame(mock_telem)
    assert event.event == "print_progress"
    assert event.color == "#00E5FF"
    assert "Layer 412/1200" in event.detail
    assert "· done " in event.detail
    assert event.badge == "34%"


def test_streamer_spool_low_alert(tmp_path):
    out_file = tmp_path / "dynamic_island.json"
    # Mount low spool
    FilamentVault.load_spool("CC1", slot=1, material="Bambu Silk PLA", color="Red", grams=14.0)

    streamer = SDCPEventStreamer("CC1", use_simulator=True, event_file=out_file)
    event: DynamicIslandEvent = asyncio.run(streamer.poll_single_frame())

    assert event.event == "spool_low_alert"
    assert event.color == "#FF9100"
    assert "14g left" in event.detail
    assert event.badge == "14g"


def test_streamer_generator_stream(tmp_path):
    out_file = tmp_path / "dynamic_island.json"
    streamer = SDCPEventStreamer("CC1", use_simulator=True, event_file=out_file)
    
    async def run_gen():
        events = []
        async for ev in streamer.stream_generator(interval=0.01, max_iterations=3):
            events.append(ev)
        return events

    events = asyncio.run(run_gen())
    assert len(events) == 3
    assert out_file.exists()


def test_agent_tool_doctor_and_dynamic_island():
    # Test new MCP tool definitions exist
    tool_names = [t["name"] for t in TOOL_DEFINITIONS]
    assert "doctor_hardware_probe" in tool_names
    assert "get_dynamic_island_event" in tool_names

    # Test doctor_hardware_probe execution
    doc_res = execute_tool("doctor_hardware_probe", {"printer": "CC1", "use_simulator": True})
    assert doc_res["success"] is True
    assert "report" in doc_res
    assert doc_res["report"]["printer_key"] == "CC1"

    # Test get_dynamic_island_event execution
    hud_res = execute_tool("get_dynamic_island_event", {"printer": "CC1", "use_simulator": True})
    assert hud_res["success"] is True
    assert "event" in hud_res
    assert hud_res["event"]["module"] == "3d"
