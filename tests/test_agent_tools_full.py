"""
Exhaustive Agent & MCP Tool Execution Tests.
Exercises all 11 tool dispatcher routes, parameter validations, and error handlers in aethelark3d/agent.py.
"""

from aethelark3d.agent import execute_tool, get_agent_tools, get_gemini_tools


def test_agent_tool_full_coverage():
    # 1. search_3d_models
    res_search = execute_tool("search_3d_models", {"query": "benchy", "limit": 2})
    assert res_search["success"] is True

    # 2. list_fleet_printers
    res_fleet = execute_tool("list_fleet_printers", {})
    assert res_fleet["success"] is True
    assert res_fleet["fleet_count"] >= 3

    # 3. set_printer_ip
    res_ip = execute_tool("set_printer_ip", {"printer": "CC2", "ip_address": "192.168.1.99"})
    assert res_ip["success"] is True
    assert res_ip["assigned_ip"] == "192.168.1.99"

    # Missing IP validation
    res_bad_ip = execute_tool("set_printer_ip", {"printer": "", "ip_address": ""})
    assert res_bad_ip["success"] is False

    # 4. Spool operations
    res_load = execute_tool("load_spool", {
        "printer": "VIRTUAL_CC1",
        "slot": 1,
        "material": "eSUN PLA+",
        "color": "Grey",
        "remaining_grams": 900.0
    })
    assert res_load["success"] is True

    res_spool_stat = execute_tool("get_spool_status", {"printer": "VIRTUAL_CC1", "slot": 1})
    assert res_spool_stat["success"] is True
    assert res_spool_stat["color"] == "Grey"

    res_unload = execute_tool("unload_spool", {"printer": "VIRTUAL_CC1", "slot": 1})
    assert res_unload["success"] is True

    res_shelf = execute_tool("get_shelf_inventory", {})
    assert res_shelf["success"] is True

    # 5. Printer Telemetry & Job Control on Digital Twin
    res_status = execute_tool("get_printer_status", {"printer": "VIRTUAL_CC1"})
    assert res_status["success"] is True
    assert "status" in res_status

    res_light = execute_tool("control_printer_light", {"printer": "VIRTUAL_CC1", "turn_on": True})
    assert res_light["success"] is True

    # Start a print on Digital Twin so it can be paused and resumed
    from aethelark3d.drivers.factory import get_driver_for_printer
    import asyncio
    sim_driver = get_driver_for_printer("VIRTUAL_CC1", use_simulator=True)
    asyncio.run(sim_driver.start_print("/local/test_part.gcode"))

    res_pause = execute_tool("pause_print_job", {"printer": "VIRTUAL_CC1"})
    assert res_pause["success"] is True

    res_resume = execute_tool("resume_print_job", {"printer": "VIRTUAL_CC1"})
    assert res_resume["success"] is True

    res_stop = execute_tool("stop_print_job", {"printer": "VIRTUAL_CC1"})
    assert res_stop["success"] is True

    from aethelark3d.drivers.factory import reset_simulators
    reset_simulators()

    # 6. dispatch_print_job
    res_dispatch = execute_tool("dispatch_print_job", {
        "model_name": "propeller.stl",
        "material": "PA-CF",
        "color": "Black",
        "mass_grams": 50.0
    })
    assert res_dispatch["success"] is True
    assert res_dispatch["selected_printer"] in ["CC1", "CC2"]

    # 7. Unknown tool handler
    res_unknown = execute_tool("non_existent_tool", {})
    assert res_unknown["success"] is False
    assert "Unknown tool" in res_unknown["error"]
