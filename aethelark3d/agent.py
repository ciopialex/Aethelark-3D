"""
Aethelark-3D Universal Agent & MCP Tool Interface.
Vendor-neutral function declarations and deterministic tool dispatcher for AI agents
(Space Eagle, Claude, OpenAI, Gemini, local LLMs, and MCP Servers).
"""

from typing import List, Dict, Any, Optional
import json
import time
import asyncio

from .config import config
from .api import search_models, download_model, find_and_prepare_print
from .filaments.catalog import resolve_filament_profile
from .spools import FilamentVault
from .drivers.factory import get_driver_for_printer


# -----------------------------------------------------------------------------
# Universal Tool Declarations (Standard JSON Schema & MCP Compatible)
# -----------------------------------------------------------------------------

TOOL_DEFINITIONS: List[Dict[str, Any]] = [
    {
        "name": "search_3d_models",
        "description": "Search 3D model repositories for printable designs with sorting filters.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search term (e.g. 'watch stand', 'spacex starship', 'cable clip')"
                },
                "sort_by": {
                    "type": "string",
                    "enum": ["default", "downloads", "likes", "least_material", "fastest"],
                    "description": "Ranking criteria based on user preference"
                },
                "limit": {
                    "type": "integer",
                    "description": "Number of results to return (default 5)"
                }
            },
            "required": ["query"]
        }
    },
    {
        "name": "print_3d_model",
        "description": "Autonomous zero-touch 3D printing trigger. Downloads model, matches filament physics, slices headlessly, uploads to printer, and triggers heating & Auto Bed Leveling.",
        "parameters": {
            "type": "object",
            "properties": {
                "query_or_id": {
                    "type": "string",
                    "description": "Keywords or specific model ID to download and print"
                },
                "filament": {
                    "type": "string",
                    "description": "Loaded filament brand/type (e.g. 'Bambu Silk PLA Red', 'Elegoo Rapid PLA+ Black', 'eSUN PETG')"
                },
                "printer": {
                    "type": "string",
                    "enum": ["CC1", "CC2", "C2", "C2_COMBO", "CC2_COMBO"],
                    "description": "Target printer key in fleet (default: CC1)"
                },
                "auto_start": {
                    "type": "boolean",
                    "description": "Set true to immediately trigger physical heating, leveling, and printing"
                }
            },
            "required": ["query_or_id"]
        }
    },
    {
        "name": "get_printer_status",
        "description": "Query real-time telemetry from a printer in the fleet (temperatures, print progress, active file, online/standby state).",
        "parameters": {
            "type": "object",
            "properties": {
                "printer": {
                    "type": "string",
                    "enum": ["CC1", "CC2", "C2", "C2_COMBO", "CC2_COMBO"],
                    "description": "Printer to inspect (default: CC1)"
                }
            }
        }
    },
    {
        "name": "control_printer_light",
        "description": "Turn the chamber LED light on or off for a printer.",
        "parameters": {
            "type": "object",
            "properties": {
                "turn_on": {
                    "type": "boolean",
                    "description": "True to illuminate chamber white LED, False to turn off"
                },
                "printer": {
                    "type": "string",
                    "enum": ["CC1", "CC2", "C2", "C2_COMBO", "CC2_COMBO"],
                    "description": "Target printer in fleet"
                }
            },
            "required": ["turn_on"]
        }
    },
    {
        "name": "list_fleet_printers",
        "description": "List all configured 3D printers in the Centauri Series fleet with their connection status and AMS configuration.",
        "parameters": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "set_printer_ip",
        "description": "Assign or update the network IP address for a printer in the fleet.",
        "parameters": {
            "type": "object",
            "properties": {
                "printer": {
                    "type": "string",
                    "enum": ["CC1", "CC2", "C2", "C2_COMBO", "CC2_COMBO"],
                    "description": "Target printer key"
                },
                "ip_address": {
                    "type": "string",
                    "description": "LAN IP address (e.g. '192.168.1.50' or '172.20.10.3')"
                }
            },
            "required": ["printer", "ip_address"]
        }
    },
    {
        "name": "load_spool",
        "description": "Load or restore a filament spool into a printer slot (AMS 1-4 or single). Can restore a used spool from shelf inventory automatically.",
        "parameters": {
            "type": "object",
            "properties": {
                "printer": {
                    "type": "string",
                    "enum": ["CC1", "CC2", "C2", "C2_COMBO", "CC2_COMBO"],
                    "description": "Target printer key"
                },
                "slot": {
                    "type": "integer",
                    "description": "AMS slot index (1-4). Defaults to 1."
                },
                "material": {
                    "type": "string",
                    "description": "Filament material & brand (e.g. 'Bambu Silk PLA', 'Elegoo Rapid PLA+')"
                },
                "color": {
                    "type": "string",
                    "description": "Filament color (e.g. 'Red', 'Black', 'Red/Black')"
                },
                "grams": {
                    "type": "number",
                    "description": "Total spool mass in grams. Leave empty if loading an existing used spool from shelf."
                },
                "is_used": {
                    "type": "boolean",
                    "description": "True if loading an already partially used spool from storage"
                }
            },
            "required": ["material"]
        }
    },
    {
        "name": "unload_spool",
        "description": "Unload a spool from a printer/AMS slot and store it into offline shelf inventory with its remaining mass.",
        "parameters": {
            "type": "object",
            "properties": {
                "printer": {
                    "type": "string",
                    "enum": ["CC1", "CC2", "C2", "C2_COMBO", "CC2_COMBO"],
                    "description": "Target printer key"
                },
                "slot": {
                    "type": "integer",
                    "description": "AMS slot index (1-4). Defaults to 1."
                }
            }
        }
    },
    {
        "name": "get_spool_status",
        "description": "Check the remaining filament mass and active spools on a printer's AMS slots.",
        "parameters": {
            "type": "object",
            "properties": {
                "printer": {
                    "type": "string",
                    "enum": ["CC1", "CC2", "C2", "C2_COMBO", "CC2_COMBO"],
                    "description": "Target printer key"
                },
                "slot": {
                    "type": "integer",
                    "description": "Specific AMS slot index (1-4) or leave empty for all slots"
                }
            }
        }
    },
    {
        "name": "get_shelf_inventory",
        "description": "List all partially used or backup filament spools currently stored on the offline storage shelf.",
        "parameters": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "dispatch_print_job",
        "description": "Evaluate fleet hardware constraints (nozzle metallurgy, enclosure, AMS slots, and mounted vault spools) to route a print job to the optimal machine with commercial quoting.",
        "parameters": {
            "type": "object",
            "properties": {
                "model_name": {
                    "type": "string",
                    "description": "Name of the 3D model"
                },
                "material": {
                    "type": "string",
                    "description": "Required filament material (e.g. 'PLA-CF', 'ABS Pro', 'Silk PLA', 'PETG')"
                },
                "color": {
                    "type": "string",
                    "description": "Required filament color (e.g. 'Black', 'Red', 'Emerald Green')"
                },
                "mass_grams": {
                    "type": "number",
                    "description": "Estimated model mass in grams"
                },
                "is_multicolor": {
                    "type": "boolean",
                    "description": "Set true if model requires multi-material / multi-color AMS"
                }
            },
            "required": ["material"]
        }
    },
    {
        "name": "doctor_hardware_probe",
        "description": "Execute sub-200ms preflight diagnostic probe on a printer to verify network latency, SDCP protocol, thermal sensors (nozzle/bed/chamber), and RTSP camera feed.",
        "parameters": {
            "type": "object",
            "properties": {
                "printer": {
                    "type": "string",
                    "enum": ["CC1", "CC2", "C2", "C2_COMBO", "CC2_COMBO"],
                    "description": "Target printer to diagnose (default: CC1)"
                },
                "use_simulator": {
                    "type": "boolean",
                    "description": "Set true to probe simulated Digital Twin"
                }
            }
        }
    },
    {
        "name": "get_dynamic_island_event",
        "description": "Fetch normalized Dynamic Island HUD telemetry payload (print progress, spool low alert, standby state) for Space-Eagle web/pill.html.",
        "parameters": {
            "type": "object",
            "properties": {
                "printer": {
                    "type": "string",
                    "enum": ["CC1", "CC2", "C2", "C2_COMBO", "CC2_COMBO"],
                    "description": "Target printer"
                },
                "use_simulator": {
                    "type": "boolean",
                    "description": "Set true to stream from simulated Digital Twin"
                }
            }
        }
    },
    {
        "name": "get_fleet_dynamic_island_event",
        "description": "Fetch highest-priority Dynamic Island HUD event across the entire fleet (CC1, CC2, C2_COMBO), prioritizing active alerts, pauses, and printing over standby.",
        "parameters": {
            "type": "object",
            "properties": {
                "use_simulator": {
                    "type": "boolean",
                    "description": "Set true to stream from simulated Digital Twin fleet"
                }
            }
        }
    },
    {
        "name": "beacon_printer",
        "description": "Trigger physical hardware identification beacon (chamber LED spotlight, toolhead robotic wave, bed-tap sensor, or stepper chime) to locate a machine in the room.",
        "parameters": {
            "type": "object",
            "properties": {
                "printer": {
                    "type": "string",
                    "description": "Target printer key or alias (e.g. 'CC1', 'CC2', 'Zeus')"
                },
                "mode": {
                    "type": "string",
                    "enum": ["spotlight", "wave", "tap", "chime", "display"],
                    "description": "Identification mode (default: 'spotlight')"
                },
                "use_simulator": {
                    "type": "boolean",
                    "description": "Run against simulator"
                }
            }
        }
    },
    {
        "name": "start_naming_session",
        "description": "Initialize a multi-turn conversational workflow to identify and assign human callsigns to unnamed fleet printers.",
        "parameters": {
            "type": "object",
            "properties": {
                "use_simulator": {
                    "type": "boolean",
                    "description": "Set true to run in simulation mode"
                }
            }
        }
    },
    {
        "name": "advance_naming_session",
        "description": "Advance the conversational naming graph with user input (selecting identification modality, assigning a name, or switching modes mid-flight).",
        "parameters": {
            "type": "object",
            "properties": {
                "user_speech": {
                    "type": "string",
                    "description": "What the user said (e.g. 'Use lights', 'Call that one Zeus', 'For my open-frame printer let's use the robotic wave')"
                },
                "use_simulator": {
                    "type": "boolean",
                    "description": "Run in simulation mode"
                }
            },
            "required": ["user_speech"]
        }
    },
    {
        "name": "set_printer_alias",
        "description": "Directly assign a custom human callsign/name (e.g. 'Zeus', 'Titan', 'Hera') to a physical printer.",
        "parameters": {
            "type": "object",
            "properties": {
                "alias": {
                    "type": "string",
                    "description": "Friendly name for the printer (e.g. 'Zeus')"
                },
                "printer": {
                    "type": "string",
                    "description": "Target printer key (e.g. 'CC1', 'CC2', 'C2_COMBO')"
                }
            },
            "required": ["alias", "printer"]
        }
    },
    {
        "name": "list_printer_aliases",
        "description": "List all registered human callsigns and their mapped printer keys and IP addresses.",
        "parameters": {
            "type": "object",
            "properties": {}
        }
    }
]


def get_agent_tools(schema_format: str = "standard") -> List[Dict[str, Any]]:
    """
    Returns tool definitions formatted for the requested agent framework:
    'standard' / 'mcp' (JSON Schema), 'anthropic' (input_schema), 'openai' (function format), or 'gemini'.
    """
    if schema_format.lower() in ["anthropic", "claude"]:
        return [
            {
                "name": t["name"],
                "description": t["description"],
                "input_schema": t["parameters"]
            }
            for t in TOOL_DEFINITIONS
        ]
    elif schema_format.lower() in ["openai"]:
        return [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["parameters"]
                }
            }
            for t in TOOL_DEFINITIONS
        ]
    elif schema_format.lower() in ["gemini"]:
        # Gemini expects uppercase types (OBJECT, STRING, INTEGER, etc.)
        def _to_gemini(schema):
            if isinstance(schema, dict):
                res = {}
                for k, v in schema.items():
                    if k == "type" and isinstance(v, str):
                        res[k] = v.upper()
                    else:
                        res[k] = _to_gemini(v)
                return res
            elif isinstance(schema, list):
                return [_to_gemini(x) for x in schema]
            return schema
        return _to_gemini(TOOL_DEFINITIONS)
    
    # Default standard JSON Schema / MCP
    return TOOL_DEFINITIONS


# Backward-compatible alias
get_gemini_tools = lambda: get_agent_tools(schema_format="gemini")


# -----------------------------------------------------------------------------
# Universal Deterministic Tool Dispatcher
# -----------------------------------------------------------------------------

def execute_tool(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """
    Direct deterministic dispatcher for Agent tool calls.
    Returns structured JSON for any LLM agent or MCP client.
    """
    printer_key = (args.get("printer") or config.default_printer).upper().replace("-", "_")

    if name == "load_spool":
        slot = int(args.get("slot", 1))
        mat = args.get("material", "Generic PLA")
        col = args.get("color", "Black")
        grams = float(args["grams"]) if "grams" in args and args["grams"] is not None else None
        is_used = bool(args.get("is_used", False))

        spool = FilamentVault.load_spool(printer_key, slot=slot, material=mat, color=col, grams=grams, is_used=is_used)
        if spool.get("matched_from_shelf"):
            msg = f"Found and restored used spool of {mat} ({col}) from shelf with {spool['remaining_grams']:.1f}g into {printer_key} Slot {slot}!"
        else:
            msg = f"Loaded {spool['remaining_grams']:.1f}g of {mat} ({col}) into {printer_key} Slot {slot}."

        return {
            "success": True,
            "printer": printer_key,
            "slot": slot,
            "spool": spool,
            "message": msg
        }

    elif name == "unload_spool":
        slot = int(args.get("slot", 1))
        spool = FilamentVault.unload_spool(printer_key, slot=slot)
        if spool:
            return {
                "success": True,
                "printer": printer_key,
                "slot": slot,
                "spool": spool,
                "message": f"Unloaded {spool['material']} ({spool['color']}) with {spool['remaining_grams']:.1f}g from {printer_key} Slot {slot} to shelf."
            }
        return {
            "success": False,
            "error": f"No spool found in {printer_key} Slot {slot} to unload."
        }

    elif name == "get_spool_status":
        slot_arg = args.get("slot")
        if slot_arg is not None:
            slot = int(slot_arg)
            spool = FilamentVault.get_mounted_spool(printer_key, slot=slot)
            if not spool:
                return {"success": True, "printer": printer_key, "slot": slot, "message": f"Slot {slot} on {printer_key} is empty."}
            rem = spool.get("remaining_grams", 1000.0)
            init = spool.get("initial_grams", 1000.0)
            pct = (rem / init) * 100.0 if init > 0 else 0
            return {
                "success": True,
                "printer": printer_key,
                "slot": slot,
                "material": spool.get("material"),
                "color": spool.get("color"),
                "remaining_grams": rem,
                "percentage_remaining": round(pct, 1),
                "message": f"{printer_key} Slot {slot} has {rem:.1f}g ({pct:.1f}%) of {spool.get('material')} ({spool.get('color')})."
            }
        else:
            slots = FilamentVault.get_printer_slots(printer_key)
            return {
                "success": True,
                "printer": printer_key,
                "slots": slots,
                "message": f"{printer_key} has {len(slots)} active spools mounted."
            }

    elif name == "get_shelf_inventory":
        shelf_items = FilamentVault.get_shelf_inventory()
        return {
            "success": True,
            "shelf_count": len(shelf_items),
            "inventory": shelf_items,
            "message": f"Storage shelf currently has {len(shelf_items)} spools stored."
        }

    elif name == "search_3d_models":
        query = args.get("query", "")
        sort_by = args.get("sort_by", "default")
        limit = int(args.get("limit", 5))
        results = search_models(query=query, limit=limit, sort_by=sort_by)
        return {
            "success": True,
            "count": len(results),
            "results": results[:limit]
        }

    elif name == "print_3d_model":
        query_or_id = args.get("query_or_id", "")
        filament = args.get("filament", "Generic PLA")
        auto_start = args.get("auto_start", True)

        try:
            res = find_and_prepare_print(
                query=query_or_id,
                filament=filament,
                printer_key=printer_key,
                open_slicer=False,
                auto_start=auto_start
            )
            if not res.get("success"):
                return {"success": False, "error": res.get("error")}
            return {
                "success": True,
                "model_title": res.get("model_title"),
                "filament_used": res.get("filament"),
                "gcode_file": res.get("gcode_file"),
                "uploaded": res.get("uploaded"),
                "print_started": res.get("print_command_sent"),
                "message": f"Successfully sliced and triggered {res.get('model_title')} on {printer_key} with {res.get('filament')}!"
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    elif name == "get_printer_status":
        known = list(config.printers.keys())
        from .aliases import PrinterAliasManager
        target_key = PrinterAliasManager.resolve_to_key(printer_key)
        is_sim = target_key.startswith("VIRTUAL_") or target_key in ["SIM", "VIRTUAL"]
        if not is_sim and target_key not in config.printers:
            return {
                "success": False,
                "error": f"There is no printer called {printer_key!r}.",
                "guidance": f"The printers set up here are: {', '.join(known)}." if known else "There are no printers set up here.",
                "printers": known
            }
        driver = get_driver_for_printer(target_key)

        async def fetch_status():
            await driver.connect()
            return await driver.get_telemetry()

        try:
            telem = asyncio.run(fetch_status())
            return {
                "success": True,
                "printer": printer_key,
                "status": str(telem.state.value),
                "nozzle_temp_c": telem.nozzle_temp,
                "nozzle_target_c": telem.nozzle_target,
                "bed_temp_c": telem.bed_temp,
                "bed_target_c": telem.bed_target,
                "chamber_temp_c": telem.chamber_temp,
                "current_layer": telem.current_layer,
                "total_layers": telem.total_layers,
                "print_percentage": telem.progress_percent,
                "current_file": telem.current_file,
                "loaded_spools": telem.loaded_spools
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to reach {printer_key}: {e}"}

    elif name == "control_printer_light":
        turn_on = bool(args.get("turn_on", True))
        driver = get_driver_for_printer(printer_key)

        async def toggle_light():
            await driver.connect()
            return await driver.set_chamber_light(turn_on)

        try:
            success = asyncio.run(toggle_light())
            return {
                "success": success,
                "printer": printer_key,
                "light_state": "ON" if turn_on else "OFF",
                "message": f"Chamber light on {printer_key} turned {'ON' if turn_on else 'OFF'}."
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to toggle light on {printer_key}: {e}"}

    elif name == "list_fleet_printers":
        printers = []
        for k, p in config.printers.items():
            printers.append({
                "key": k,
                "name": p.get("name"),
                "model": p.get("model"),
                "host": p.get("host") or "Unassigned",
                "enclosed": p.get("enclosed", False),
                "has_ams": p.get("has_ams", False),
                "ams_slots": p.get("ams_slots", [])
            })
        return {"success": True, "fleet_count": len(printers), "printers": printers}

    elif name == "pause_print_job":
        driver = get_driver_for_printer(printer_key)
        try:
            success = asyncio.run(driver.pause_print())
            return {"success": success, "printer": printer_key, "action": "paused"}
        except Exception as e:
            return {"success": False, "error": f"Failed to pause {printer_key}: {e}"}

    elif name == "resume_print_job":
        driver = get_driver_for_printer(printer_key)
        try:
            success = asyncio.run(driver.resume_print())
            return {"success": success, "printer": printer_key, "action": "resumed"}
        except Exception as e:
            return {"success": False, "error": f"Failed to resume {printer_key}: {e}"}

    elif name == "stop_print_job":
        driver = get_driver_for_printer(printer_key)
        try:
            success = asyncio.run(driver.stop_print())
            return {"success": success, "printer": printer_key, "action": "stopped"}
        except Exception as e:
            return {"success": False, "error": f"Failed to stop {printer_key}: {e}"}

    elif name == "set_printer_ip":
        target = args.get("printer", "").upper().replace("-", "_")
        ip = args.get("ip_address", "").strip()
        if not target or not ip:
            return {"success": False, "error": "Missing printer key or ip_address."}

        config.set_printer_ip(target, ip)
        return {
            "success": True,
            "printer": target,
            "assigned_ip": ip,
            "message": f"Successfully updated IP address for {target} to {ip}."
        }

    elif name == "dispatch_print_job":
        from .dispatcher import FleetDispatcher, PrintJob
        dispatcher = FleetDispatcher()
        job = PrintJob(
            id=f"job_{int(time.time())}",
            model_name=args.get("model_name", "model.stl"),
            material=args.get("material", "PLA Basic"),
            color=args.get("color", "Black"),
            mass_grams=float(args.get("mass_grams", 30.0)),
            is_multicolor=bool(args.get("is_multicolor", False)),
            colors_required=[args.get("color", "Black")] if not args.get("is_multicolor") else ["Red", "Black"]
        )
        dec = dispatcher.dispatch_job(job)
        return {
            "success": dec.is_feasible,
            "selected_printer": dec.selected_printer,
            "slot_index": dec.slot_index,
            "spool_swap_required": dec.spool_swap_required,
            "confidence_score": dec.confidence_score,
            "rationale": dec.rationale,
            "rejections": dec.rejection_reasons,
            "quote": dec.quote
        }

    elif name == "doctor_hardware_probe":
        from .doctor import HardwareDoctor
        use_sim = bool(args.get("use_simulator", False))
        report = HardwareDoctor.probe_printer(printer_key=printer_key, use_simulator=use_sim)
        return {
            "success": True,
            "report": report.to_dict()
        }

    elif name == "get_dynamic_island_event":
        from .streamer import SDCPEventStreamer
        use_sim = bool(args.get("use_simulator", False))
        streamer = SDCPEventStreamer(printer_key=printer_key, use_simulator=use_sim)
        event = asyncio.run(streamer.poll_single_frame())
        return {
            "success": True,
            "event": event.to_dict()
        }

    elif name == "get_fleet_dynamic_island_event":
        from .streamer import FleetSDCPStreamer
        use_sim = bool(args.get("use_simulator", False))
        f_streamer = FleetSDCPStreamer(printer_keys=config.addressable_printer_keys(),
                                       use_simulator=use_sim)
        event = asyncio.run(f_streamer.poll_fleet_frame())
        return {
            "success": True,
            "event": event.to_dict()
        }

    elif name == "beacon_printer":
        from .beacon import PrinterBeacon, BeaconMode
        mode_str = args.get("mode", "spotlight").lower()
        b_mode = BeaconMode.SPOTLIGHT
        if "wave" in mode_str:
            b_mode = BeaconMode.WAVE
        elif "chime" in mode_str:
            b_mode = BeaconMode.CHIME
        elif "tap" in mode_str:
            b_mode = BeaconMode.TAP
        elif "display" in mode_str:
            b_mode = BeaconMode.DISPLAY

        use_sim = bool(args.get("use_simulator", False))
        if b_mode == BeaconMode.TAP:
            tap_res = asyncio.run(PrinterBeacon.listen_for_bed_tap(use_simulator=use_sim))
            return {
                "success": tap_res.detected,
                "tapped_printer": tap_res.printer_key,
                "sensor": tap_res.sensor_type,
                "pressure_grams": tap_res.pressure_grams,
                "message": tap_res.message
            }
        else:
            res = asyncio.run(PrinterBeacon.trigger_beacon(printer_key, mode=b_mode, use_simulator=use_sim))
            return res

    elif name == "start_naming_session":
        from .naming_graph import NamingSessionGraph
        use_sim = bool(args.get("use_simulator", False))
        graph = NamingSessionGraph()
        return {
            "success": True,
            "session": graph.start_session()
        }

    elif name == "advance_naming_session":
        from .naming_graph import NamingSessionGraph
        speech = args.get("user_speech", "")
        use_sim = bool(args.get("use_simulator", False))
        graph = NamingSessionGraph()
        graph.start_session()

        # Check if user picked modality or provided a name
        speech_lower = speech.lower()
        if any(m in speech_lower for m in ["light", "spot", "tap", "touch", "wave", "wiggle", "chime", "sound"]):
            res = asyncio.run(graph.select_mode(speech, use_simulator=use_sim))
        else:
            res = asyncio.run(graph.assign_name_and_advance(speech, use_simulator=use_sim))

        return {
            "success": True,
            "result": res
        }

    elif name == "set_printer_alias":
        from .aliases import PrinterAliasManager
        alias = args.get("alias", "")
        target = args.get("printer", printer_key)
        aliases = PrinterAliasManager.set_alias(alias, target)
        return {
            "success": True,
            "alias": alias.upper(),
            "printer": target.upper(),
            "all_aliases": aliases,
            "message": f"Successfully named printer {target.upper()} as '{alias.upper()}'."
        }

    elif name == "list_printer_aliases":
        from .aliases import PrinterAliasManager
        aliases = PrinterAliasManager.get_aliases()
        return {
            "success": True,
            "aliases": aliases,
            "total_count": len(aliases)
        }

    return {"success": False, "error": f"Unknown tool: {name}"}


# Backward-compatible alias
execute_gemini_tool = execute_tool

