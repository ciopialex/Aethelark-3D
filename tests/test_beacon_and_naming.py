"""
Tests for Physical Beacon Protocol, Conversational Naming Decision Graph,
Callsign Aliases, and Stepper Motor Fleet Orchestra.
"""

import pytest
import asyncio
from aethelark3d.aliases import PrinterAliasManager
from aethelark3d.beacon import PrinterBeacon, BeaconMode, TapDetectionResult
from aethelark3d.naming_graph import NamingSessionGraph, NamingStep
from aethelark3d.agent import execute_tool, TOOL_DEFINITIONS


def test_alias_manager_lifecycle():
    # Set alias
    aliases = PrinterAliasManager.set_alias("ZEUS", "CC1")
    assert "ZEUS" in aliases
    assert aliases["ZEUS"] == "CC1"

    # Bidirectional resolution
    assert PrinterAliasManager.resolve_to_key("zeus") == "CC1"
    assert PrinterAliasManager.resolve_to_key("ZEUS") == "CC1"
    assert PrinterAliasManager.resolve_to_key("CC1") == "CC1"
    assert "ZEUS" in PrinterAliasManager.get_display_name("CC1")

    # Set another
    PrinterAliasManager.set_alias("HERA", "CC2")
    assert PrinterAliasManager.resolve_to_key("hera") == "CC2"

    # Remove
    assert PrinterAliasManager.remove_alias("HERA") is True
    assert "HERA" not in PrinterAliasManager.get_aliases()


def test_beacon_modalities_simulation():
    # 1. Spotlight beacon
    res_spot = asyncio.run(PrinterBeacon.trigger_beacon("CC1", mode=BeaconMode.SPOTLIGHT, use_simulator=True))
    assert res_spot["success"] is True
    assert "Spotlight" in res_spot["message"]

    # 2. Robotic wave beacon
    res_wave = asyncio.run(PrinterBeacon.trigger_beacon("CC1", mode=BeaconMode.WAVE, use_simulator=True))
    assert res_wave["success"] is True
    assert "wave" in res_wave["message"]

    # 3. Stepper chime beacon
    res_chime = asyncio.run(PrinterBeacon.trigger_beacon("CC1", mode=BeaconMode.CHIME, use_simulator=True))
    assert res_chime["success"] is True
    assert "chime" in res_chime["message"]

    # 4. Isolate spotlight across fleet
    res_iso = asyncio.run(PrinterBeacon.isolate_spotlight("CC1", use_simulator=True))
    assert res_iso["success"] is True
    assert res_iso["spotlight_printer"] == "CC1"

    # 5. Bed tap detection
    res_tap = asyncio.run(PrinterBeacon.listen_for_bed_tap(simulated_tap_key="C2_COMBO", use_simulator=True))
    assert res_tap.detected is True
    assert res_tap.printer_key == "C2_COMBO"
    assert res_tap.pressure_grams > 0


def test_conversational_naming_graph_flow():
    graph = NamingSessionGraph()

    # Step 1: Start session
    init_res = graph.start_session()
    assert init_res["step"] == NamingStep.INIT.value
    assert len(init_res["options"]) == 4

    # Step 2: Select Spotlight mode
    mode_res = asyncio.run(graph.select_mode("lights", use_simulator=True))
    assert mode_res["step"] == NamingStep.BEACONING_PRINTER.value
    first_printer = mode_res["active_printer"]

    # Step 3: Name first printer 'Zeus'
    advance_res = asyncio.run(graph.assign_name_and_advance("Zeus", use_simulator=True))
    assert PrinterAliasManager.resolve_to_key("Zeus") == first_printer

    # Step 4: Mid-flight switch to 'wave' mode
    switch_res = asyncio.run(graph.switch_mode("robotic wave", use_simulator=True))
    assert graph.state.active_mode == BeaconMode.WAVE


def test_agent_tools_beacon_and_aliases():
    # 1. beacon_printer tool
    res_b = execute_tool("beacon_printer", {"printer": "CC1", "mode": "spotlight", "use_simulator": True})
    assert res_b["success"] is True

    # 2. set_printer_alias tool
    res_a = execute_tool("set_printer_alias", {"alias": "TITAN", "printer": "CC1"})
    assert res_a["success"] is True
    assert res_a["alias"] == "TITAN"

    # 3. list_printer_aliases tool
    res_l = execute_tool("list_printer_aliases", {})
    assert res_l["success"] is True
    assert "TITAN" in res_l["aliases"]

    # 4. start_naming_session tool
    res_s = execute_tool("start_naming_session", {"use_simulator": True})
    assert res_s["success"] is True
    assert "session" in res_s
