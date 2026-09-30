"""
Tests for Human-Centric Dynamic Island States and Fleet-Wide Priority Streaming.
"""

import os
import time
import json
import asyncio
import pytest
from pathlib import Path

from aethelark3d.streamer import (
    SDCPEventStreamer,
    FleetSDCPStreamer,
    DynamicIslandEvent,
    DEFAULT_DYNAMIC_ISLAND_PATH
)
from aethelark3d.drivers.base import PrinterTelemetry, PrinterState
from aethelark3d.spools import FilamentVault
from aethelark3d.agent import execute_tool, TOOL_DEFINITIONS


def test_human_centric_six_states_emission(tmp_path):
    out_file = tmp_path / "dynamic_island.json"
    streamer = SDCPEventStreamer("CC1", use_simulator=True, event_file=out_file)

    # 1. State: Active Printing (with clock completion formatting)
    telem_printing = PrinterTelemetry(
        printer_name="CC1",
        brand="Elegoo",
        ip_address="127.0.0.1",
        state=PrinterState.PRINTING,
        nozzle_temp=220.0,
        bed_temp=60.0,
        current_layer=184,
        total_layers=600,
        progress_percent=38.0,
        time_remaining_seconds=6120
    )
    ev_print = streamer.format_event_frame(telem_printing)
    assert ev_print.event == "print_progress"
    assert ev_print.color == "#00E5FF"
    assert "· done " in ev_print.detail
    assert ev_print.badge == "38%"

    # 2. State: First Layer Milestone
    telem_layer1 = PrinterTelemetry(
        printer_name="CC1",
        brand="Elegoo",
        ip_address="127.0.0.1",
        state=PrinterState.PRINTING,
        current_layer=2,
        total_layers=600,
        progress_percent=1.0,
        time_remaining_seconds=7200
    )
    ev_l1 = streamer.format_event_frame(telem_layer1)
    assert ev_l1.event == "first_layer_passed"
    assert ev_l1.activity["trailing"] == "2h 0m"
    assert ev_l1.alert.endswith(":first_layer")

    # 3. State: Spool Low Notice
    FilamentVault.load_spool("CC1", slot=1, material="PLA Basic", color="Black", grams=15.0)
    telem_idle = PrinterTelemetry(
        printer_name="CC1",
        brand="Elegoo",
        ip_address="127.0.0.1",
        state=PrinterState.IDLE
    )
    ev_spool = streamer.format_event_frame(telem_idle)
    assert ev_spool.event == "spool_low_alert"
    assert ev_spool.color == "#FF9100"
    assert "15g left" in ev_spool.detail

    # 4. State: Paused for Hardware/Magnets
    telem_paused = PrinterTelemetry(
        printer_name="CC1",
        brand="Elegoo",
        ip_address="127.0.0.1",
        state=PrinterState.PAUSED
    )
    ev_paused = streamer.format_event_frame(telem_paused)
    assert ev_paused.event == "printer_paused"
    assert ev_paused.color == "#D4AF37"
    assert ev_paused.badge == "Paused"

    # 5. State: Attention / Error / Clog
    telem_err = PrinterTelemetry(
        printer_name="CC1",
        brand="Elegoo",
        ip_address="127.0.0.1",
        state=PrinterState.ERROR
    )
    ev_err = streamer.format_event_frame(telem_err)
    assert ev_err.event == "printer_error"
    assert ev_err.color == "#FF453A"

    # 6. State: Print Complete & Cooled
    telem_done = PrinterTelemetry(
        printer_name="CC1",
        brand="Elegoo",
        ip_address="127.0.0.1",
        state=PrinterState.COMPLETED,
        bed_temp=26.0
    )
    ev_done = streamer.format_event_frame(telem_done)
    assert ev_done.event == "print_complete"
    assert ev_done.color == "#A855F7"
    assert ev_done.badge in ("Ready", "Cooling")


def test_fleet_streamer_alert_prioritization(tmp_path):
    out_file = tmp_path / "fleet_dynamic_island.json"
    # Ensure fresh spools
    FilamentVault.load_spool("CC1", slot=1, material="PLA", color="Black", grams=800.0)
    FilamentVault.load_spool("CC2", slot=1, material="PLA", color="White", grams=800.0)
    FilamentVault.load_spool("C2_COMBO", slot=1, material="Silk", color="Red", grams=800.0)

    fleet = FleetSDCPStreamer(
        printer_keys=["CC1", "CC2", "C2_COMBO"],
        use_simulator=True,
        event_file=out_file
    )

    # Poll single fleet frame
    ev = asyncio.run(fleet.poll_fleet_frame())
    assert ev.module == "3d"
    assert out_file.exists()


def test_agent_get_fleet_dynamic_island_tool():
    # Verify tool schema definition
    tool_names = [t["name"] for t in TOOL_DEFINITIONS]
    assert "get_fleet_dynamic_island_event" in tool_names

    # Execute tool
    res = execute_tool("get_fleet_dynamic_island_event", {"use_simulator": True})
    assert res["success"] is True
    assert "event" in res
    assert res["event"]["module"] == "3d"
