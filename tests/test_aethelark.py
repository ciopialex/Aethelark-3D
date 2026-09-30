"""
Comprehensive unit and integration tests for Aethelark-3D core architecture.
Tests drivers, digital twin simulator, slicer abstraction, rasterizer, and spool vault.
"""

import asyncio
from pathlib import Path
from aethelark3d.filaments.catalog import resolve_filament_profile
from aethelark3d.agent import get_agent_tools, execute_tool, get_gemini_tools, execute_gemini_tool
from aethelark3d.config import config
from aethelark3d.drivers.factory import get_driver_for_printer
from aethelark3d.drivers.base import PrinterState
from aethelark3d.slicers.rasterizer import render_stl_to_image
from aethelark3d.spools import FilamentVault


def test_filament_resolver_rapid_pla():
    name, path, physics = resolve_filament_profile("eSUN PLA HS Black")
    assert "PLA" in name or "RAPID" in name
    assert path.exists()
    assert physics["max_volumetric_speed"] >= 20.0


def test_filament_resolver_silk():
    name, path, physics = resolve_filament_profile("Red Silk PLA")
    assert "Silk" in name
    assert path.exists()
    assert physics["outer_wall_speed"] == 60  # Silk slow outer wall rule


def test_agent_tools_schema_multi_format():
    # 1. Standard / MCP format
    mcp_tools = get_agent_tools(schema_format="standard")
    assert len(mcp_tools) >= 11
    tool_names = [t["name"] for t in mcp_tools]
    assert "search_3d_models" in tool_names
    assert "print_3d_model" in tool_names
    assert "get_printer_status" in tool_names
    assert "dispatch_print_job" in tool_names

    # 2. Anthropic / Claude format
    claude_tools = get_agent_tools(schema_format="anthropic")
    assert len(claude_tools) >= 11
    assert "input_schema" in claude_tools[0]

    # 3. OpenAI format
    openai_tools = get_agent_tools(schema_format="openai")
    assert len(openai_tools) >= 11
    assert openai_tools[0]["type"] == "function"

    # 4. Gemini format
    gemini_tools = get_gemini_tools()
    assert len(gemini_tools) >= 11


def test_agent_tool_execution_on_digital_twin():
    # 1. Inspect fleet
    fleet_res = execute_tool("list_fleet_printers", {})
    assert fleet_res["success"] is True
    assert fleet_res["fleet_count"] >= 3

    # 2. Control light on Virtual CC1
    light_res = execute_tool("control_printer_light", {"printer": "VIRTUAL_CC1", "turn_on": True})
    assert light_res["success"] is True
    assert light_res["light_state"] == "ON"

    # 3. Status on Virtual CC1
    status_res = execute_tool("get_printer_status", {"printer": "VIRTUAL_CC1"})
    assert status_res["success"] is True
    assert status_res["status"] == "IDLE"
    assert status_res["nozzle_temp_c"] == 25.0

    # 4. Agent tool execution of dispatch_print_job
    dispatch_res = execute_tool("dispatch_print_job", {
        "model_name": "bracket.stl",
        "material": "PLA-CF",
        "color": "Black",
        "mass_grams": 40.0
    })
    assert dispatch_res["success"] is True
    assert dispatch_res["selected_printer"] in ["CC1", "CC2"]
    assert dispatch_res["quote"] is not None


def test_printer_fleet_config():
    assert "CC1" in config.printers
    assert "CC1_COMBO" in config.printers
    assert "CC2" in config.printers
    assert "C2" in config.printers
    assert "C2_COMBO" in config.printers
    assert "CC2_COMBO" in config.printers
    # The shipped fleet carries a `host` slot for CC1 but leaves it BLANK on
    # purpose: a default address is a claim about the buyer's network that
    # cannot be true (see Config.__init__). `a3d discover` fills it in when the
    # printer answers. So the invariant is the slot's existence and type, not a
    # routable value -- asserting a real IP here only passed by reading the
    # developer's own discovered config, and failed on any fresh install.
    assert isinstance(config.printers["CC1"].get("host"), str)
    assert config.printers["CC1_COMBO"]["has_ams"] is True
    assert config.printers["CC2_COMBO"]["has_ams"] is True


def test_filament_vault_lifecycle():
    # 1. Load brand-new 1000g spool into C2_COMBO Slot 2
    spool = FilamentVault.load_spool("C2_COMBO", slot=2, material="Bambu Silk PLA", color="Red", grams=1000.0)
    assert spool["remaining_grams"] == 1000.0
    assert spool["mounted_to"]["printer"] == "C2_COMBO"
    assert spool["mounted_to"]["slot"] == 2

    # 2. Simulate 650g used over prints
    rem = FilamentVault.deduct_usage("C2_COMBO", slot=2, grams_used=650.0)
    assert rem == 350.0

    # 3. Unload spool from C2_COMBO Slot 2 to Shelf
    unloaded = FilamentVault.unload_spool("C2_COMBO", slot=2)
    assert unloaded is not None
    assert unloaded["remaining_grams"] == 350.0
    assert len(FilamentVault.get_shelf_inventory()) >= 1

    # 4. Mount the used spool from shelf into CC1 Slot 1
    restored = FilamentVault.load_spool("CC1", slot=1, material="Bambu Silk PLA", color="Red", is_used=True)
    assert restored["matched_from_shelf"] is True
    assert restored["remaining_grams"] == 350.0
    assert restored["mounted_to"]["printer"] == "CC1"
    assert restored["mounted_to"]["slot"] == 1


def test_virtual_digital_twin_driver():
    """Verify high-fidelity digital twin simulator driver."""
    async def _async_test():
        driver = get_driver_for_printer("VIRTUAL_CC1", use_simulator=True)
        connected = await driver.connect()
        assert connected is True

        # 1. Check initial standby telemetry
        telemetry = await driver.get_telemetry()
        assert telemetry.state == PrinterState.IDLE
        assert telemetry.nozzle_temp == 25.0
        assert telemetry.bed_temp == 24.0

        # 2. Control light
        light_ack = await driver.set_chamber_light(True)
        assert light_ack is True

        # 3. Trigger virtual print
        started = await driver.start_print("/local/test_seal.gcode", auto_level=True)
        assert started is True

        # Wait for physics simulation loop to advance
        await asyncio.sleep(0.3)
        telemetry_printing = await driver.get_telemetry()
        assert telemetry_printing.state in [PrinterState.HEATING, PrinterState.LEVELING, PrinterState.PRINTING]
        assert telemetry_printing.nozzle_temp > 25.0
        assert telemetry_printing.bed_temp > 24.0

        # 4. Pause and Resume
        paused = await driver.pause_print()
        assert paused is True
        resumed = await driver.resume_print()
        assert resumed is True

        # 5. Stop
        stopped = await driver.stop_print()
        assert stopped is True
        await driver.disconnect()

    asyncio.run(_async_test())


def test_3d_thumbnail_rasterizer(tmp_path):
    """Verify pure Python 3D vector thumbnail rasterization on real binary STL geometry."""
    from tests.test_real_artifacts import create_synthetic_binary_stl
    stl_file = tmp_path / "cube.stl"
    create_synthetic_binary_stl(stl_file, size_mm=20.0)

    img = render_stl_to_image(stl_file, size=(512, 512), color_rgb=(230, 45, 55), is_rgba=True)
    assert img.size == (512, 512)
    assert img.mode == "RGBA"
    
    # Save and verify
    out_png = tmp_path / "thumb.png"
    img.save(out_png)
    assert out_png.stat().st_size > 1000
