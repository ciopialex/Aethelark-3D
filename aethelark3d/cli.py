"""
Aethelark-3D Unified CLI.
High-speed CLI and Machine-Contract Interface for 3D Printer Automation.
"""

import re
import sys
import os
import time
import json
import asyncio
from pathlib import Path
from typing import Optional, List, Dict, Any
import typer
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich import print as rprint

from .config import config
from .filaments.catalog import resolve_filament_profile
from .drivers.base import PrinterState, PrinterTelemetry
# NOTE: `.api` (pulls curl_cffi via the providers, numpy via the rasterizer) and
# `.drivers.factory` (pulls numpy via the Elegoo driver) are imported INSIDE the
# few commands that use them, not here. The eagle spawns a fresh `a3d` process
# per voice turn, so a top-level heavy import is paid on every command — even
# `register`, `status` and `fleet` that never need those libraries. Measured
# ~0.44s -> ~0.15s cold start for the light commands by keeping this list lean.
from .spools import FilamentVault
from .doctor import HardwareDoctor, DoctorReport
from .streamer import SDCPEventStreamer, DEFAULT_DYNAMIC_ISLAND_PATH
from .slice_info import read_slice_info, humanise
from . import costing

#: `--grams` not given. Distinguishable from a real 0 so the file's own
#: figure is used rather than silently overridden by a default.
_GRAMS_UNSET = -1.0

app = typer.Typer(
    name="aethelark-3d",
    help="⚡ Aethelark-3D: Universal 3D Model Automation & Slicer Handoff Engine",
    add_completion=False,
    rich_markup_mode="rich",
    # Failures leave as sentences (see main); the eagle reads stderr aloud,
    # and a framed traceback is not something to say to a person.
    pretty_exceptions_enable=False,
)
console = Console()


#: The orderings every --sort flag advertises. `default` is accepted too: it is
#: what the flag defaults to and means "however the source ranked them".
VALID_ORDERINGS = ("default", "downloads", "likes", "least_material", "fastest")


def _check_ordering(sort: str, as_json: bool) -> None:
    """Refuse an ordering nobody offers, naming the ones that work."""
    if sort in VALID_ORDERINGS:
        return
    offered = ", ".join(o for o in VALID_ORDERINGS if o != "default")
    message = (f"{sort!r} is not an ordering this module offers. "
               f"Use one of: {offered}.")
    if as_json:
        print(json.dumps({"error": message, "valid_orderings": list(VALID_ORDERINGS)},
                         indent=2))
    else:
        console.print(f"[bold red]{message}[/bold red]")
    raise typer.Exit(1)


@app.command("search")
def cli_search(
    query: str = typer.Argument(..., help="Search query (e.g. 'watch stand', 'starship')"),
    limit: int = typer.Option(10, "--limit", "-n", help="Number of results"),
    sort: str = typer.Option("default", "--sort", "-s", help="Sort by: downloads, likes, least_material, fastest"),
    as_json: bool = typer.Option(False, "--json", "-j", help="Output machine-readable JSON")
):
    """
    🔍 Search 3D models with smart metric filters.
    """
    _check_ordering(sort, as_json)

    if not as_json:
        console.print(f"[cyan]Searching Aethelark-3D for:[/cyan] [bold white]'{query}'[/bold white] (Sort: {sort})...")

    from .api import search_models
    results = search_models(query=query, limit=limit, sort_by=sort)
    
    if as_json:
        print(json.dumps(results, indent=2))
        return

    if not results:
        console.print("[yellow]No models found.[/yellow]")
        return

    table = Table(title=f"Aethelark-3D Search: '{query}'", header_style="bold magenta", show_lines=True)
    table.add_column("#", justify="right", style="dim", width=4)
    table.add_column("ID", justify="right", style="cyan", width=9)
    table.add_column("Title", style="bold white")
    table.add_column("Creator", style="green", width=16)
    table.add_column("Downloads", justify="right", style="yellow", width=11)
    table.add_column("Time", justify="right", style="blue", width=9)
    table.add_column("Weight", justify="right", style="dim", width=8)

    for idx, r in enumerate(results, 1):
        table.add_row(
            str(idx),
            str(r["id"]),
            r["title"][:40],
            r["creator"][:15],
            f"{r['downloads']:,}",
            r["print_time"],
            r["weight"]
        )
    console.print(table)


@app.command("download")
def cli_download(
    query_or_id: str = typer.Argument(..., help="Keywords or Model ID"),
    sort: str = typer.Option("downloads", "--sort", "-s", help="downloads, likes, least_material, fastest"),
    open_slicer: bool = typer.Option(False, "--open", help="Launch in Elegoo Slicer"),
    stl: bool = typer.Option(False, "--stl", help="Download raw STL instead of 3MF"),
    printer: Optional[str] = typer.Option(None, "--printer", "-p", help="Printer this model is destined for: CC1, CC2, C2_COMBO"),
    as_json: bool = typer.Option(False, "--json", "-j", help="Output machine-readable JSON")
):
    """
    🚀 Download model and organize locally.
    """
    if not as_json:
        console.print(f"[cyan]Downloading model:[/cyan] {query_or_id}...")

    from .api import download_model
    res = download_model(query_or_id=query_or_id, sort_by=sort, raw_stl=stl,
                         open_slicer=open_slicer, printer=printer)
    
    if as_json:
        print(json.dumps(res, indent=2))
        # A failure that exits 0 reads as success to the eagle, whose `ok`
        # comes from the exit code -- and the model is told to trust `ok`.
        if not res.get("success"):
            raise typer.Exit(1)
        return

    if res.get("success"):
        console.print(f"[bold green]✨ Success![/bold green] File saved to: [underline]{res['file_path']}[/underline]")
    else:
        console.print(f"[bold red]❌ Failed: {res.get('error')}[/bold red]")


@app.command("browse")
def cli_browse(
    query: str = typer.Argument(..., help="What to look for, e.g. 'watch stand'"),
    limit: int = typer.Option(5, "--limit", "-n", help="How many candidates to fetch"),
    printer: Optional[str] = typer.Option(None, "--printer", "-p", help="Printer these are destined for"),
    sort: str = typer.Option("downloads", "--sort", "-s", help="downloads, likes, least_material, fastest"),
    as_json: bool = typer.Option(False, "--json", "-j", help="Output machine-readable JSON")
):
    """
    🎠 Download the top N matches and return them with geometry, for browsing.
    """
    from .api import browse_models
    deck = browse_models(query, limit=limit, printer=printer, sort_by=sort)
    failed = deck.get("failed") or []

    if as_json:
        # "Nothing matched" and "everything broke" are different answers and
        # must not both be a successful empty deck. Search finding nothing is
        # exit 0 with no candidates; search finding models that then all failed
        # to download is a failure, and carries the reason the provider gave.
        if not deck["candidates"] and failed:
            deck["error"] = (
                f"all {len(failed)} matches for '{query}' failed to download: "
                + "; ".join(f"{f.get('title') or f.get('id')}: {f.get('error')}"
                            for f in failed[:3]))
            print(json.dumps(deck, indent=2))
            raise typer.Exit(1)
        print(json.dumps(deck, indent=2))
        return

    if not deck["candidates"]:
        if failed:
            console.print(f"[bold red]All {len(failed)} matches for '{query}' "
                          f"failed to download.[/bold red]")
            for f in failed[:3]:
                console.print(f"  [dim]{f.get('title') or f.get('id')}: "
                              f"{f.get('error')}[/dim]")
        else:
            console.print(f"[bold red]No models found for '{query}'.[/bold red]")
        raise typer.Exit(1)
    for i, c in enumerate(deck["candidates"], 1):
        d = c.get("dimensions") or {}
        console.print(f"[cyan]{i}.[/cyan] {c['title']} "
                      f"[dim]({d.get('x')}×{d.get('y')}×{d.get('z')}mm)[/dim]")


@app.command("discover")
def cli_discover(
    seconds: float = typer.Option(2.0, "--seconds", "-t", help="How long to listen"),
    adopt_found: bool = typer.Option(True, "--adopt/--no-adopt", help="Write what is found into the fleet"),
    as_json: bool = typer.Option(False, "--json", "-j", help="Output machine-readable JSON")
):
    """
    📡 Find 3D printers on this network and update the fleet with what answers.
    """
    from .discovery import adopt as adopt_devices, discover
    devices = discover(timeout=seconds)
    result = (adopt_devices(devices) if adopt_found else
              {"found": len(devices), "moved": {}, "identified": {}, "added": {},
               "silent": {}, "printers": [
                   {"key": "?", "ip": d.ip, "name": d.label,
                    "model": d.machine, "mainboard_id": d.mainboard_id}
                   for d in devices]})
    if as_json:
        if devices and adopt_found:
            from .readiness import check, guidance
            checks = [check(p["key"]) for p in result["printers"]]
            for p, c in zip(result["printers"], checks):
                p.update(ready=c["ready"], needs_access_code=c["needs_access_code"])
                if c["why"]:
                    p["why"] = c["why"]
            result["guidance"] = guidance(checks)
        if not devices:
            # The eagle reads `error` and `guidance`; a bare result dict
            # reached the model as raw JSON with nothing to say next.
            silent = result.get("silent") or {}
            result["error"] = "No 3D printers answered on this network."
            result["guidance"] = (
                ("The printers already set up did not answer either, so they are "
                 "probably off. " if silent else "") +
                "Ask the user to switch the printer on and make sure it is on the "
                "same Wi-Fi as this computer, then look again.")
        print(json.dumps(result, indent=2))
        if not devices:
            raise typer.Exit(1)
        return
    if not devices:
        console.print("[bold red]No 3D printers answered on this network.[/bold red]")
        console.print("[dim]They must be powered on and on the same network as this computer.[/dim]")
        raise typer.Exit(1)
    for p in result["printers"]:
        console.print(f"[green]●[/green] [bold]{p['key']}[/bold] {p['name']} "
                      f"[dim]{p['model']}[/dim] at [cyan]{p['ip']}[/cyan]")
    for key, ip in result["moved"].items():
        console.print(f"[yellow]↻[/yellow] {key} moved to {ip}")
    for key in result["added"]:
        console.print(f"[cyan]+[/cyan] {key} added to your fleet")
    # Saying only what answered leaves someone with three printers reading
    # "1 found" and unable to tell it from owning one.
    for key, host in result.get("silent", {}).items():
        where = f" (last seen at {host})" if host else ""
        console.print(f"[dim]○ {key} did not answer{where}[/dim]")


@app.command("local")
def cli_local(
    name: str = typer.Argument(..., help="Part of the file name, e.g. 'keychain'"),
    directory: Optional[str] = typer.Option(None, "--dir", "-d", help="Only look in this folder"),
    printer: Optional[str] = typer.Option(None, "--printer", "-p", help="Printer these are destined for"),
    limit: int = typer.Option(8, "--limit", "-n", help="How many matches to return"),
    as_json: bool = typer.Option(False, "--json", "-j", help="Output machine-readable JSON")
):
    """
    📁 Find model files already on this machine, and measure them.
    """
    from .api import local_models
    deck = local_models(name, directory=directory, printer=printer, limit=limit)
    if as_json:
        print(json.dumps(deck, indent=2))
        return
    if not deck["candidates"]:
        where = directory or "the usual model folders"
        console.print(f"[dim]No model file matching '{name}' in {where}.[/dim]")
        return
    for i, c in enumerate(deck["candidates"], 1):
        d = c.get("dimensions") or {}
        console.print(f"[cyan]{i}.[/cyan] {c['title']} "
                      f"[dim]({d.get('x')}×{d.get('y')}×{d.get('z')}mm)[/dim] "
                      f"[dim]{c['file_path']}[/dim]")


@app.command("print")
def cli_print(
    query_or_id: str = typer.Argument(..., help="Model to print (e.g. 'watch stand')"),
    printer: Optional[str] = typer.Option(None, "--printer", "-p", help="Printer name or key (default: your default printer)"),
    filament: Optional[str] = typer.Option(None, "--filament", "-f", help="Filament: e.g. 'Bambu PLA Silk'"),
    sort: str = typer.Option("downloads", "--sort", "-s", help="downloads, least_material, fastest"),
    auto_start: bool = typer.Option(True, "--start/--no-start", help="Auto start print on printer"),
    auto_level: bool = typer.Option(True, "--level/--no-level", help="Run bed levelling before the print"),
    leveling: Optional[str] = typer.Option(None, "--leveling", help="true or false; an alternative to --level/--no-level for callers that pass values rather than flags"),
    want_purgex: Optional[str] = typer.Option(None, "--want-purgex", help="true to opt into PurgeX multi-colour purge optimisation; omit for standard purge (the default)"),
    purgex: bool = typer.Option(False, "--purgex/--default-purge", help="Opt in to PURGEX intelligent purge & anti-topple tower. Off by default; the basic pipeline uses standard purge."),
    nozzle_temp: Optional[int] = typer.Option(None, "--nozzle-temp", help="Nozzle temperature for this print"),
    bed_temp: Optional[int] = typer.Option(None, "--bed-temp", help="Bed temperature for this print"),
    as_json: bool = typer.Option(False, "--json", "-j", help="Output machine-readable JSON")
):
    """
    🖨️ 100% Autonomous Headless Print: Download -> Map Filament -> Headless Slice -> Upload -> Auto-Start.
    """
    printer = printer or config.default_printer
    purge_mode_str = "PURGEX (Optimized)" if purgex else "Standard Default"
    if not as_json:
        console.print(f"[bold cyan]🚀 Executing 100% Headless Aethelark-3D Pipeline for:[/bold cyan] {query_or_id}")
        console.print(f"[dim]Target: {printer} | Filament: {filament or 'Default'} | Purge Mode: {purge_mode_str} | Criteria: {sort}[/dim]")

    from .api import find_and_prepare_print
    res = find_and_prepare_print(
        query=query_or_id,
        sort_by=sort,
        printer_key=printer,
        filament=filament,
        auto_start=auto_start,
        auto_level=(auto_level if leveling is None
                    else str(leveling).strip().lower() in ("true", "1", "yes", "on")),
        enable_purgex=(purgex if want_purgex is None
                       else str(want_purgex).strip().lower() in ("true", "1", "yes", "on")),
        nozzle_temp=nozzle_temp,
        bed_temp=bed_temp,
    )

    if as_json:
        print(json.dumps(res, indent=2))
        if not res.get("success"):
            raise typer.Exit(1)
        return

    if res.get("success"):
        upload_txt = "[bold green]✅ Uploaded to Printer[/bold green]" if res.get("uploaded") else "[yellow]⚠️ Local Only[/yellow]"
        start_txt = "[bold green]🚀 Print Command Sent (ABL Enabled)![/bold green]" if res.get("print_command_sent") else "[dim]Ready on Printer[/dim]"

        console.print(Panel(
            f"[bold green]✨ Headless Pipeline Complete (Zero UI Interaction)![/bold green]\n\n"
            f"📦 [bold]Model:[/bold] {res.get('model_title')}\n"
            f"🧵 [bold]Filament Profile:[/bold] {res.get('filament')}\n"
            f"⚡ [bold]Sliced G-Code:[/bold] [underline]{res.get('gcode_file')}[/underline]\n"
            f"🌐 [bold]Network Delivery:[/bold] {upload_txt}\n"
            f"🖨️ [bold]Printer Status:[/bold] {start_txt}",
            title="🎉 Print Job Dispatched",
            border_style="green"
        ))
    else:
        console.print(f"[bold red]❌ Failed: {res.get('error')}[/bold red]")


@app.command("print-batch")
def cli_print_batch(
    jobs: str = typer.Argument(..., help='JSON array of {"model_id","printer"}'),
    as_json: bool = typer.Option(False, "--json", "-j", help="Output machine-readable JSON")
):
    """
    🖨️  Start prints for several picked models, each on its named printer.
    """
    from .api import print_batch
    try:
        parsed = json.loads(jobs)
    except ValueError as e:
        console.print(f"[bold red]jobs must be a JSON array ({e}).[/bold red]")
        raise typer.Exit(2)
    out = print_batch(parsed)
    if as_json:
        if out["failed"] and not out["started"]:
            # Nothing started: a failure, not an empty success.
            out["error"] = "; ".join(
                f"{j.get('model_id')} on {j.get('printer')}: {j.get('error')}"
                for j in out["failed"][:3])
            out["guidance"] = "Tell the user none of the prints started, and why."
            print(json.dumps(out, indent=2))
            raise typer.Exit(1)
        print(json.dumps(out, indent=2))
        return
    for j in out["started"]:
        console.print(f"[green]▶ started[/green] {j['model_id']} on {j['printer']}")
    for j in out["failed"]:
        console.print(f"[red]✗ {j['model_id']} on {j.get('printer')}: {j.get('error')}[/red]")


@app.command("filament")
def cli_filament(
    query: str = typer.Argument(..., help="Filament name (e.g. 'AzureFilm HS Black Matte PLA', 'eSUN PLA+ Black')"),
    printer: Optional[str] = typer.Option(None, "--printer", "-p", help="Printer name or key (default: your default printer)"),
    as_json: bool = typer.Option(False, "--json", "-j", help="Output machine-readable JSON")
):
    """
    🧵 Test filament name resolution to ElegooSlicer JSON presets & physical print rules.
    """
    printer = printer or config.default_printer
    from .filaments.resolver import UnknownMaterial
    try:
        name, path, physics = resolve_filament_profile(query, printer=printer)
    except UnknownMaterial as e:
        if as_json:
            print(json.dumps({"query": query, "error": str(e),
                              "guidance": "Ask which material it is."}, indent=2))
        else:
            console.print(f"[yellow]{e}[/yellow]")
        raise typer.Exit(code=1)

    if as_json:
        print(json.dumps({
            "query": query,
            "printer": printer,
            "matched_preset": name,
            "preset_path": str(path),
            "physics": physics
        }, indent=2))
        return

    console.print(Panel(
        f"[bold cyan]Input Query:[/bold cyan] [bold white]'{query}'[/bold white]\n"
        f"[bold cyan]Target Machine:[/bold cyan] [bold yellow]{printer.upper()}[/bold yellow]\n"
        f"[bold green]Matched Preset:[/bold green] [yellow]{name}[/yellow]\n"
        f"[dim]JSON Path:[/dim] {path}\n\n"
        f"⚙️ [bold]Calibrated Print Parameters:[/bold]\n"
        f"  • Nozzle Temperatures: [bold green]{physics.get('nozzle_first_layer', 220)}°C (1st layer) / {physics.get('nozzle_other_layers', 215)}°C (other layers)[/bold green]\n"
        f"  • Bed Temperature: [bold yellow]{physics.get('bed_temp', 60)}°C[/bold yellow]\n"
        f"  • Outer Wall Speed: [bold yellow]{physics.get('outer_wall_speed', 150)} mm/s[/bold yellow]\n"
        f"  • Max Volumetric Flow: [bold cyan]{physics.get('max_volumetric_speed', 16.0)} mm³/s[/bold cyan]\n"
        f"  • Flow Ratio: [bold magenta]{physics.get('flow_ratio', 1.0)}[/bold magenta]\n"
        f"  • Part Cooling Fan: [bold white]{physics.get('fan_min_speed', 80)}% - {physics.get('fan_max_speed', 100)}%[/bold white]\n"
        f"  • Strategy: [dim italic]{physics.get('notes', 'Standard')}[/dim italic]",
        title="🧵 Filament Profile Resolution",
        border_style="cyan"
    ))


@app.command("printers")
def cli_printers(
    action_or_key: Optional[str] = typer.Argument(None, help="Action ('remove' or 'list') or printer key to remove"),
    printer_to_remove: Optional[str] = typer.Argument(None, help="Printer key when action is 'remove'"),
    remove: Optional[str] = typer.Option(None, "--remove", "-r", help="Remove a printer from the fleet by key"),
    as_json: bool = typer.Option(False, "--json", "-j", help="Output machine-readable JSON")
):
    """
    🖨️ View configured printer fleet (CC1, CC2, C2_COMBO).
    """
    target_rm = remove
    if action_or_key and action_or_key.lower().strip() == "remove":
        target_rm = printer_to_remove or target_rm

    if target_rm:
        from .aliases import PrinterAliasManager
        canonical_key = PrinterAliasManager.resolve_to_key(target_rm)
        success = config.remove_printer(canonical_key)
        if success:
            if as_json:
                print(json.dumps({"success": True, "removed": canonical_key, "fleet_count": len(config.printers)}, indent=2))
            else:
                console.print(f"[bold green]✓ Removed printer from fleet:[/bold green] [bold cyan]{canonical_key}[/bold cyan] (remaining: {len(config.printers)})")
            return
        else:
            known = list(config.printers.keys())
            err_msg = f"There is no printer called {target_rm!r}."
            guidance = f"The printers set up here are: {', '.join(known)}." if known else "There are no printers set up yet. Offer to look for them on this network with a3d_discover."
            if as_json:
                print(json.dumps({"success": False, "error": err_msg, "guidance": guidance, "printers": known}, indent=2))
            else:
                console.print(f"[bold red]❌ {err_msg}[/bold red] {guidance}")
            raise typer.Exit(code=1)

    if as_json:
        print(json.dumps(config.printers, indent=2))
        return

    table = Table(title="Aethelark-3D Printer Fleet", header_style="bold cyan")
    table.add_column("Key", style="bold yellow", width=10)
    table.add_column("Printer Name", style="bold white")
    table.add_column("IP / Host", style="green", width=15)
    table.add_column("AMS Multi-Color", style="magenta", width=15)

    for k, p in config.printers.items():
        ams_str = "Yes (4 slots)" if p.get("has_ams") else "No"
        host_str = p.get("host") or "[dim]Not Set[/dim]"
        table.add_row(k, p.get("name"), host_str, ams_str)

    console.print(table)


@app.command("printer")
def cli_printer(
    action: str = typer.Argument("list", help="'list' or 'remove'"),
    printer_key: Optional[str] = typer.Argument(None, help="Target printer key to remove (e.g. CC1, CC2)"),
    remove: Optional[str] = typer.Option(None, "--remove", "-r", help="Target printer key to remove"),
    as_json: bool = typer.Option(False, "--json", "-j", help="Output machine-readable JSON")
):
    """
    🖨️ View or remove printers from your fleet.
    """
    target_rm = remove or (printer_key if action.lower().strip() == "remove" else None)
    if target_rm:
        from .aliases import PrinterAliasManager
        canonical_key = PrinterAliasManager.resolve_to_key(target_rm)
        success = config.remove_printer(canonical_key)
        if success:
            if as_json:
                print(json.dumps({"success": True, "removed": canonical_key, "fleet_count": len(config.printers)}, indent=2))
            else:
                console.print(f"[bold green]✓ Removed printer from fleet:[/bold green] [bold cyan]{canonical_key}[/bold cyan] (remaining: {len(config.printers)})")
            return
        else:
            known = list(config.printers.keys())
            err_msg = f"There is no printer called {target_rm!r}."
            guidance = f"The printers set up here are: {', '.join(known)}." if known else "There are no printers set up yet. Offer to look for them on this network with a3d_discover."
            if as_json:
                print(json.dumps({"success": False, "error": err_msg, "guidance": guidance, "printers": known}, indent=2))
            else:
                console.print(f"[bold red]❌ {err_msg}[/bold red] {guidance}")
            raise typer.Exit(code=1)

    return cli_printers(as_json=as_json)


def _is_printer_online(host: Optional[str], port: int = 3030, timeout: float = 0.25) -> bool:
    if not host or str(host).strip() in ["", "0.0.0.0", "127.0.0.1", "null", "None"]:
        return False
    import socket
    try:
        with socket.create_connection((str(host).strip(), int(port or 3030)), timeout=timeout):
            return True
    except (socket.timeout, ConnectionRefusedError, OSError):
        return False


@app.command("fleet")
def cli_fleet(
    action_or_key: Optional[str] = typer.Argument(None, help="Action ('remove' or 'list') or printer key to remove"),
    printer_to_remove: Optional[str] = typer.Argument(None, help="Printer key when action is 'remove'"),
    set_ip: Optional[str] = typer.Option(None, "--set-ip", help="Set IP for printer (e.g. --set-ip CC2 --ip 192.168.1.50)"),
    ip: Optional[str] = typer.Option(None, "--ip", help="IP address to assign with --set-ip"),
    remove: Optional[str] = typer.Option(None, "--remove", "-r", help="Remove a printer from the fleet by key (e.g. --remove CC2)"),
    as_json: bool = typer.Option(False, "--json", "-j", help="Output machine-readable JSON fleet list")
):
    """
    🏭 Manage and inspect your Centauri Series 3D printer fleet.
    """
    from .drivers.factory import get_driver_for_printer
    target_rm = remove
    if action_or_key and action_or_key.lower().strip() == "remove":
        target_rm = printer_to_remove or target_rm

    if target_rm:
        from .aliases import PrinterAliasManager
        canonical_key = PrinterAliasManager.resolve_to_key(target_rm)
        success = config.remove_printer(canonical_key)
        if success:
            if as_json:
                print(json.dumps({"success": True, "removed": canonical_key, "fleet_count": len(config.printers)}, indent=2))
            else:
                console.print(f"[bold green]✓ Removed printer from fleet:[/bold green] [bold cyan]{canonical_key}[/bold cyan] (remaining: {len(config.printers)})")
            return
        else:
            known = list(config.printers.keys())
            err_msg = f"There is no printer called {target_rm!r}."
            guidance = f"The printers set up here are: {', '.join(known)}." if known else "There are no printers set up yet. Offer to look for them on this network with a3d_discover."
            if as_json:
                print(json.dumps({"success": False, "error": err_msg, "guidance": guidance, "printers": known}, indent=2))
            else:
                console.print(f"[bold red]❌ {err_msg}[/bold red] {guidance}")
            raise typer.Exit(code=1)

    if set_ip and ip:
        key = set_ip.upper().replace("-", "_")
        config.set_printer_ip(key, ip)
        if as_json:
            print(json.dumps({"success": True, "printer": key, "host": ip}))
        else:
            console.print(f"[green]Assigned IP [bold]{ip}[/bold] to printer [bold]{key}[/bold]![/green]")
        return

    fleet_data = []
    for k, p in config.printers.items():
        driver = get_driver_for_printer(k)
        caps = driver.capabilities
        slots = FilamentVault.get_printer_slots(k)
        host_val = p.get("host")
        is_online = _is_printer_online(host_val, p.get("port", 3030))

        fleet_data.append({
            "key": k,
            "name": p.get("name", k),
            "brand": p.get("brand", "Elegoo"),
            "model": p.get("model", "Centauri Carbon"),
            "host": host_val,
            "port": p.get("port", 3030),
            "type": "4-Slot AMS" if p.get("has_ams") else "Single Spool",
            "has_ams": bool(p.get("has_ams")),
            "online": is_online,
            "loaded_spools": slots,
            "capabilities": {
                "nozzle_material": caps.nozzle_material,
                "nozzle_diameter": caps.nozzle_diameter,
                "max_nozzle_temp": caps.max_nozzle_temp,
                "max_bed_temp": caps.max_bed_temp,
                "is_enclosed": caps.is_enclosed,
                "has_ams": caps.has_ams,
                "ams_slot_count": caps.ams_slot_count
            }
        })

    if as_json:
        print(json.dumps(fleet_data, indent=2))
        return

    table = Table(title="🏭 Aethelark-3D Fleet: Centauri Series", header_style="bold magenta", show_lines=True)
    table.add_column("Key", style="bold cyan", width=10)
    table.add_column("Printer Name", style="bold white", width=28)
    table.add_column("Type", style="green", width=12)
    table.add_column("Loaded Spools", style="yellow", width=24)
    table.add_column("IP Address", justify="right", style="blue", width=14)
    table.add_column("Status", justify="center", width=10)

    for item in fleet_data:
        k = item["key"]
        pname = item["name"]
        type_str = item["type"]
        ip_str = item["host"] or "[dim]Unassigned[/dim]"
        status_str = "[bold green]ONLINE[/bold green]" if item["online"] else "[dim]STANDBY[/dim]"
        
        slots = item["loaded_spools"]
        if slots:
            slot_items = [f"{s.get('material', '')} ({s.get('color', '')})" for s in slots.values()]
            spool_summary = ", ".join(slot_items)
        else:
            spool_summary = "[dim]Empty[/dim]"

        table.add_row(k, pname, type_str, spool_summary, ip_str, status_str)

    console.print(table)


def _job_view(key: str, name: str, telemetry) -> dict:
    """The job card for the island when something is on the bed, opened on the
    controls: the running print already is the compact bar."""
    labels = {PrinterState.PRINTING: "Printing", PrinterState.PAUSED: "Paused",
              PrinterState.HEATING: "Heating", PrinterState.LEVELING: "Leveling",
              PrinterState.PREPARING: "Preparing"}
    label = labels.get(telemetry.state)
    from .streamer import job_card
    if not label:
        return {"_card": _printer_card(key, name, telemetry)}
    pct = telemetry.progress_percent or None
    return {"_card": job_card(key, name, telemetry, label, pct)}


def _printer_card(key: str, name: str, telemetry) -> dict:
    """A printer with nothing on the bed: its state, temperatures, the spool
    it holds and its light. Nothing about a job, because there is none."""
    from .spools import FilamentVault
    from .streamer import _deg, job_card
    offline = telemetry.state == PrinterState.DISCONNECTED
    label = "Offline" if offline else "Idle"
    card = job_card(key, name, telemetry, label, None)
    spool = FilamentVault.get_mounted_spool(key, slot=1) or {}
    loaded = " ".join(x for x in (spool.get("color"), spool.get("material")) if x)
    card.update({
        "variant": "printer",
        "badge": label,
        "cam_note": label,
        "spool": loaded or "No filament recorded",
        "detail": ("Not answering" if offline else
                   f"Ready · nozzle {_deg(telemetry.nozzle_temp)} · bed {_deg(telemetry.bed_temp)}"),
    })
    return card


@app.command("status")
def cli_status(
    printer: Optional[str] = typer.Option(None, "--printer", "-p", help="Printer to query: CC1, CC2, C2_COMBO (defaults to active printing machine)"),
    use_simulator: bool = typer.Option(False, "--simulator", "--sim", help="Query simulated Digital Twin"),
    as_json: bool = typer.Option(False, "--json", "-j", help="Output machine-readable JSON telemetry")
):
    """
    📊 Query real-time telemetry (temperatures, print progress, status) from printer.
    """
    from .drivers.factory import get_driver_for_printer
    known = list(config.printers.keys())

    # 0ms cache tap: If the dynamic island event file was updated <10s ago by
    # the background listener, tap its telemetry directly to avoid socket contention.
    cached_telemetry = None
    cached_printer_key = None
    if not use_simulator:
        from .streamer import DEFAULT_DYNAMIC_ISLAND_PATH
        if DEFAULT_DYNAMIC_ISLAND_PATH.exists():
            try:
                mtime = DEFAULT_DYNAMIC_ISLAND_PATH.stat().st_mtime
                if (time.time() - mtime) < 10.0:
                    with open(DEFAULT_DYNAMIC_ISLAND_PATH, "r", encoding="utf-8") as f:
                        c_data = json.load(f)
                    c_tel = c_data.get("telemetry", {})
                    c_printer = c_data.get("printer", "")
                    c_st = c_tel.get("state")
                    if c_tel and c_st and c_st not in ("DISCONNECTED", PrinterState.DISCONNECTED.value):
                        try:
                            parsed_st = PrinterState(c_st)
                        except Exception:
                            parsed_st = PrinterState.PRINTING if "PRINT" in str(c_st).upper() else PrinterState.IDLE
                        cached_telemetry = PrinterTelemetry(
                            printer_name=c_tel.get("printer_name", c_printer),
                            brand=c_tel.get("brand", "Elegoo"),
                            ip_address=c_tel.get("ip_address", ""),
                            state=parsed_st,
                            substate_code=c_tel.get("substate_code"),
                            nozzle_temp=float(c_tel.get("nozzle_temp", 0.0)),
                            nozzle_target=float(c_tel.get("nozzle_target", 0.0)),
                            bed_temp=float(c_tel.get("bed_temp", 0.0)),
                            bed_target=float(c_tel.get("bed_target", 0.0)),
                            chamber_temp=float(c_tel.get("chamber_temp", 0.0)),
                            current_layer=int(c_tel.get("current_layer", 0)),
                            total_layers=int(c_tel.get("total_layers", 0)),
                            progress_percent=float(c_tel.get("progress_percent", 0.0)),
                            current_file=c_tel.get("current_file"),
                            time_remaining_seconds=c_tel.get("time_remaining_seconds"),
                            loaded_spools=c_tel.get("loaded_spools", {}),
                            raw_telemetry=c_tel.get("raw_telemetry", {})
                        )
                        cached_printer_key = c_printer
            except Exception:
                cached_telemetry = None
                cached_printer_key = None

    if printer and printer.upper() != "AUTO":
        from .aliases import PrinterAliasManager
        target_key = PrinterAliasManager.resolve_to_key(printer)
        is_sim = use_simulator or target_key.startswith("VIRTUAL_") or target_key in ["SIM", "VIRTUAL"]
        if not is_sim and target_key not in config.printers:
            err_msg = (f"There is no printer called {printer!r}." if printer else "No printer is set up yet.")
            guidance = f"The printers set up here are: {', '.join(known)}." if known else "There are no printers set up yet. Offer to look for them on this network with a3d_discover."
            if as_json:
                print(json.dumps({
                    "error": err_msg,
                    "guidance": guidance,
                    "printers": known
                }, indent=2))
            else:
                console.print(f"[bold red]❌ {err_msg}[/bold red] {guidance}")
            raise typer.Exit(code=1)

        pkey = target_key
        printer_info = config.get_printer(pkey) or {}
        if cached_telemetry and (cached_printer_key == pkey or cached_telemetry.printer_name == pkey):
            telemetry = cached_telemetry
        else:
            driver = get_driver_for_printer(pkey, use_simulator=use_simulator)
            try:
                telemetry = asyncio.run(driver.get_telemetry())
            except Exception as e:
                telemetry = PrinterTelemetry(
                    printer_name=printer_info.get("name", pkey),
                    brand=printer_info.get("brand", "Elegoo"),
                    ip_address=printer_info.get("host") or "",
                    state=PrinterState.DISCONNECTED,
                    raw_telemetry={"error": str(e)}
                )
    else:
        if not known:
            err_msg = "No printers configured in fleet."
            guidance = "There are no printers set up yet. Offer to look for them on this network with a3d_discover."
            if as_json:
                print(json.dumps({
                    "error": err_msg,
                    "guidance": guidance,
                    "printers": []
                }, indent=2))
            else:
                console.print(f"[bold red]❌ {err_msg}[/bold red] {guidance}")
            raise typer.Exit(code=1)

        if cached_telemetry and cached_telemetry.state == PrinterState.PRINTING:
            pkey = cached_printer_key or config.default_printer
            printer_info = config.get_printer(pkey) or {}
            telemetry = cached_telemetry
            found_active = True
        else:
            candidate_printers = config.addressable_printer_keys()
            found_active = False
            for cand in candidate_printers:
                c_info = config.get_printer(cand) or {}
                if c_info.get("host"):
                    d = get_driver_for_printer(cand, use_simulator=use_simulator)
                    try:
                        t = asyncio.run(asyncio.wait_for(d.get_telemetry(), timeout=1.5))
                        if t.state == PrinterState.PRINTING:
                            pkey = cand
                            printer_info = c_info
                            telemetry = t
                            driver = d
                            found_active = True
                            break
                    except Exception:
                        pass
            if not found_active:
                pkey = config.default_printer if config.default_printer in config.printers else known[0]
                printer_info = config.get_printer(pkey) or {}
                driver = get_driver_for_printer(pkey, use_simulator=use_simulator)
                try:
                    telemetry = asyncio.run(driver.get_telemetry())
                except Exception as e:
                    telemetry = PrinterTelemetry(
                        printer_name=printer_info.get("name", pkey),
                        brand=printer_info.get("brand", "Elegoo"),
                        ip_address=printer_info.get("host") or "",
                        state=PrinterState.DISCONNECTED,
                        raw_telemetry={"error": str(e)}
                    )

    slots = FilamentVault.get_printer_slots(pkey)
    p_status = 1 if telemetry.state == PrinterState.PRINTING else 0
    status_label = "🟢 Printing" if p_status == 1 else ("⚪ Standby / Idle" if telemetry.state != PrinterState.DISCONNECTED else "🔴 Disconnected")

    eta_str = "N/A"
    if telemetry.time_remaining_seconds and telemetry.time_remaining_seconds > 0:
        hrs = telemetry.time_remaining_seconds // 3600
        mins = (telemetry.time_remaining_seconds % 3600) // 60
        eta_str = f"{hrs}h {mins}m" if hrs > 0 else f"{mins}m"

    if as_json:
        print(json.dumps({
            "printer": pkey,
            "name": printer_info.get("name", pkey),
            "host": printer_info.get("host") or "",
            "online": telemetry.state != PrinterState.DISCONNECTED,
            "state": telemetry.state.value if hasattr(telemetry.state, "value") else str(telemetry.state),
            **({"why": str((telemetry.raw_telemetry or {}).get("error"))[:300]}
               if telemetry.state == PrinterState.DISCONNECTED
               and (telemetry.raw_telemetry or {}).get("error") else {}),
            "telemetry": {} if telemetry.state == PrinterState.DISCONNECTED else {
                "nozzle_temp": round(telemetry.nozzle_temp, 1),
                "nozzle_target": round(telemetry.nozzle_target, 1),
                "bed_temp": round(telemetry.bed_temp, 1),
                "bed_target": round(telemetry.bed_target, 1),
                "chamber_temp": round(telemetry.chamber_temp, 1)
            },
            "print_info": {
                "status_code": p_status,
                "status_label": status_label,
                "current_layer": telemetry.current_layer,
                "total_layers": telemetry.total_layers,
                "progress_percent": round(telemetry.progress_percent, 1),
                "filename": telemetry.current_file,
                "time_remaining_seconds": telemetry.time_remaining_seconds,
                "formatted_eta": eta_str
            },
            "spool": slots.get("slot_1") if slots else (next(iter(slots.values()), None) if slots else None),
            "spools": slots,
            "purgex_savings_percent": None,
            **_job_view(pkey, printer_info.get("name", pkey), telemetry),
        }, indent=2))
        return

    spool_lines = []
    if slots:
        for s_k, s_v in slots.items():
            rem = s_v.get("remaining_grams", 1000.0)
            init = s_v.get("initial_grams", 1000.0)
            pct = (rem / init) * 100.0 if init > 0 else 0
            spool_lines.append(f"  • {s_k.upper()}: [cyan]{s_v.get('material')}[/cyan] ({s_v.get('color')}) -> [bold yellow]{rem:.1f}g[/bold yellow] ([green]{pct:.1f}%[/green])")
    else:
        spool_lines.append("  • [dim]No spool mapped in vault. Run 'a3d spool --load'[/dim]")

    spool_str = "\n".join(spool_lines)

    console.print(Panel(
        f"[bold white]Printer:[/bold white] [bold green]{printer_info.get('name', pkey)}[/bold green] ([dim]{printer_info.get('host', '127.0.0.1')}[/dim])\n"
        f"[bold white]Status:[/bold white] {status_label}\n\n"
        f"🌡️ [bold]Telemetry:[/bold]\n"
        f"  • Nozzle Temp: [bold yellow]{telemetry.nozzle_temp:.1f}°C[/bold yellow] (Target: {telemetry.nozzle_target:.1f}°C)\n"
        f"  • Bed Temp: [bold red]{telemetry.bed_temp:.1f}°C[/bold red] (Target: {telemetry.bed_target:.1f}°C)\n"
        f"  • Chamber Temp: [bold blue]{telemetry.chamber_temp:.1f}°C[/bold blue]\n\n"
        f"🧵 [bold]Loaded Spools (AMS):[/bold]\n"
        f"{spool_str}\n\n"
        f"📊 [bold]Print Progress:[/bold]\n"
        f"  • Current Layer: [cyan]{telemetry.current_layer} / {telemetry.total_layers}[/cyan]\n"
        f"  • Progress: [bold green]{telemetry.progress_percent:.1f}%[/bold green]\n"
        f"  • ETA: [bold cyan]{eta_str}[/bold cyan]\n"
        f"  • File: [dim]{telemetry.current_file or 'None'}[/dim]",
        title=f"🖨️ {pkey} Live Status",
        border_style="green"
    ))


@app.command("spool")
def cli_spool(
    printer: Optional[str] = typer.Option(None, "--printer", "-p", help="Printer name or key (default: your default printer)"),
    slot: int = typer.Option(1, "--slot", "-s", help="AMS Slot (1-4) or single spool"),
    load: Optional[str] = typer.Option(None, "--load", "-l", help="Load filament (e.g. 'Bambu Silk PLA')"),
    color: str = typer.Option("", "--color", "-c", help="Spool colour, if the person said one"),
    grams: Optional[float] = typer.Option(None, "--grams", "-g", help="Grams left on the spool"),
    new: Optional[str] = typer.Option(None, "--new", help="true for a sealed spool (full, --size grams)"),
    size: Optional[float] = typer.Option(None, "--size", help="What a full spool holds, in grams (default 1000)"),
    nozzle_temp: Optional[int] = typer.Option(None, "--nozzle-temp", help="Nozzle temperature the person wants for this spool"),
    bed_temp: Optional[int] = typer.Option(None, "--bed-temp", help="Bed temperature the person wants for this spool"),
    used: bool = typer.Option(False, "--used", "-u", help="Match an existing used spool from shelf inventory"),
    unload: bool = typer.Option(False, "--unload", help="Unload spool from slot and store in shelf inventory"),
    shelf: bool = typer.Option(False, "--shelf", help="List all spools in offline shelf inventory"),
    as_json: bool = typer.Option(False, "--json", "-j", help="Output machine-readable JSON")
):
    """
    🧵 Track active AMS slots, spool consumption, and offline shelf inventory.
    """
    printer = printer or config.default_printer
    target = printer.upper().replace("-", "_")

    # 1. List Shelf Inventory
    if shelf:
        items = FilamentVault.get_shelf_inventory()
        if as_json:
            print(json.dumps(items, indent=2))
            return
        if not items:
            console.print("[yellow]Shelf inventory is empty. Unload a spool to store it on the shelf.[/yellow]")
            return
        table = Table(title="📦 Offline Shelf Inventory", header_style="bold magenta")
        table.add_column("ID", style="dim", width=10)
        table.add_column("Material", style="bold white", width=25)
        table.add_column("Color", style="cyan", width=15)
        table.add_column("Remaining Mass", justify="right", style="bold yellow", width=16)
        table.add_column("Initial", justify="right", style="dim", width=10)
        for it in items:
            table.add_row(
                it.get("id", ""),
                it.get("material", ""),
                it.get("color", ""),
                f"{it.get('remaining_grams', 0):.1f}g",
                f"{it.get('initial_grams', 0):.1f}g"
            )
        console.print(table)
        return

    known = list(config.printers.keys())
    from .aliases import PrinterAliasManager
    target_key = PrinterAliasManager.resolve_to_key(printer)
    # Registering a spool is said the way people talk -- "on my Centauri
    # Carbon 2" -- and discovery may have named the machine after its network
    # id. With one printer there is only one it can mean. Only here: a PRINT
    # confirms with the name the person said, so it must never be redirected.
    if target_key not in config.printers and load and len(known) == 1:
        target_key = known[0]
    if target_key not in config.printers:
        err_msg = (f"There is no printer called {printer!r}." if printer else "No printer is set up yet.")
        guidance = f"The printers set up here are: {', '.join(known)}." if known else "There are no printers set up yet. Offer to look for them on this network with a3d_discover."
        if as_json:
            print(json.dumps({
                "error": err_msg,
                "guidance": guidance,
                "printers": known
            }, indent=2))
        else:
            console.print(f"[bold red]❌ {err_msg}[/bold red] {guidance}")
        raise typer.Exit(code=1)
    target = target_key

    # 2. Unload Spool to Shelf
    if unload:
        spool = FilamentVault.unload_spool(target, slot=slot)
        if as_json:
            print(json.dumps({"success": spool is not None, "unloaded_spool": spool}, indent=2))
            return
        if spool:
            console.print(f"[bold yellow]Unloaded [white]{spool['material']}[/white] ({spool['color']}) with [bold white]{spool['remaining_grams']:.1f}g[/bold white] from [cyan]{target} Slot {slot}[/cyan] to Shelf Inventory![/bold yellow]")
        else:
            console.print(f"[red]No spool found in {target} Slot {slot} to unload.[/red]")
        return

    # 3. Load Spool into Slot
    if load:
        is_new = str(new or "").strip().lower() in ("true", "1", "yes", "on")
        from .filaments.resolver import UnknownMaterial, describe, plan as plan_filament
        from .printer_models import model_for_key, slicer_profile
        model, entry, _ = model_for_key(target)
        prof = slicer_profile(model.printer_model, float(entry.get("nozzle") or 0.4)) if model else None
        try:
            fil = plan_filament(load, prof.family if prof else "ECC",
                                nozzle=nozzle_temp, bed=bed_temp, printer=model)
        except UnknownMaterial as e:
            if as_json:
                print(json.dumps({"error": str(e), "guidance": "Ask the user which material it "
                                  "is (PLA, PETG, ABS, ASA, PC, TPU...), then call again."}, indent=2))
            else:
                console.print(f"[yellow]{e}[/yellow]")
            raise typer.Exit(code=1)
        from .filaments.resolver import over_limit
        too_hot = over_limit(fil, model)
        if too_hot:
            msg = f"On {PrinterAliasManager.get_display_name(target).split(' (')[0]}, the {too_hot}."
            if as_json:
                print(json.dumps({"error": msg, "guidance": "Nothing was recorded. Tell the "
                                  "user the limit and ask which temperature to use."}, indent=2))
            else:
                console.print(f"[yellow]{msg}[/yellow]")
            raise typer.Exit(code=1)
        spool = FilamentVault.load_spool(target, slot=slot, material=load, color=color,
                                         grams=grams, is_used=used, is_new=is_new,
                                         size_grams=size, nozzle_temp=nozzle_temp,
                                         bed_temp=bed_temp)
        spool["prints_with"] = describe(fil)
        if as_json:
            # Spoken, so the callsign alone: "ZEUS", not "ZEUS (ELEGOO_7526B5)".
            name = PrinterAliasManager.get_display_name(target).split(" (")[0]
            what = " ".join(x for x in (spool.get("color"), spool.get("material")) if x)
            amount = (f"{spool['remaining_grams']:.0f} g left" if (is_new or grams is not None or used)
                      else "amount not recorded")
            print(json.dumps({
                "success": True,
                "summary": (f"{what} on {name}, slot {slot} ({amount}). Prints with "
                            f"{spool['prints_with']}."),
                "printer": target, "slot": slot, "loaded_spool": spool,
            }, indent=2))
            return
        if spool.get("matched_from_shelf"):
            console.print(f"[bold green]Restored USED spool from shelf: [white]{spool['material']}[/white] ({spool['color']}) with [bold yellow]{spool['remaining_grams']:.1f}g[/bold yellow] into [cyan]{target} Slot {slot}[/cyan]![/bold green]")
        else:
            console.print(f"[bold green]Loaded NEW spool: [white]{spool['material']}[/white] ({spool['color']}) with [bold white]{spool['remaining_grams']:.1f}g[/bold white] into [cyan]{target} Slot {slot}[/cyan]![/bold green]")
        return

    # 4. Display Active Slots on Printer
    slots = FilamentVault.get_printer_slots(target)
    if as_json:
        active_spool = slots.get("slot_1") if slots else (next(iter(slots.values()), None) if slots else None)
        print(json.dumps({"printer": target, "spool": active_spool, "slots": slots}, indent=2))
        return

    if not slots:
        console.print(Panel(f"[dim]No spools loaded in {target}. Use 'a3d spool -p {target} -l \"<material>\" -c <color>' to load.[/dim]", title=f"🧵 Active Spools: {target}", border_style="cyan"))
        return

    lines = []
    for s_name, s_data in slots.items():
        rem = s_data.get("remaining_grams", 1000.0)
        init = s_data.get("initial_grams", 1000.0)
        pct = (rem / init) * 100.0 if init > 0 else 0
        lines.append(f"[bold white]{s_name.upper()}:[/bold white] [cyan]{s_data.get('material')}[/cyan] ({s_data.get('color')}) -> [bold yellow]{rem:.1f}g[/bold yellow] / {init:.1f}g ([green]{pct:.1f}%[/green])")

    console.print(Panel("\n".join(lines), title=f"🧵 Active Spools: {target}", border_style="cyan"))


@app.command("vault")
def cli_vault(
    printer: Optional[str] = typer.Option(None, "--printer", "-p", help="Filter by specific printer"),
    as_json: bool = typer.Option(False, "--json", "-j", help="Output machine-readable JSON ledger")
):
    """
    🏛️ Unified FilamentVault memory ledger (all mounted AMS slots + offline shelf inventory).
    """
    raw_data = FilamentVault._load_data()
    mounted = raw_data.get("mounted", {})
    shelf = raw_data.get("shelf", [])

    if printer:
        pkey = printer.upper().replace("-", "_")
        mounted = {pkey: mounted.get(pkey, {})}

    if as_json:
        print(json.dumps({
            "mounted": mounted,
            "shelf": shelf,
            "total_mounted_slots": sum(len(v) for v in mounted.values()),
            "total_shelf_spools": len(shelf)
        }, indent=2))
        return

    console.print(Panel(
        f"🧵 [bold cyan]Mounted AMS Units:[/bold cyan] {len(mounted)} Printers | "
        f"📦 [bold yellow]Offline Shelf Spools:[/bold yellow] {len(shelf)} Spools",
        title="🏛️ Aethelark-3D FilamentVault Overview",
        border_style="magenta"
    ))

    # Table of mounted
    if mounted:
        t_mount = Table(title="🖨️ Active AMS Loaded Slots", header_style="bold cyan", show_lines=True)
        t_mount.add_column("Printer", style="bold white", width=12)
        t_mount.add_column("Slot", style="yellow", width=8)
        t_mount.add_column("Material", style="green", width=20)
        t_mount.add_column("Color", style="cyan", width=12)
        t_mount.add_column("Remaining", justify="right", style="bold yellow", width=14)

        for p_k, p_slots in mounted.items():
            for s_k, s_d in p_slots.items():
                rem = s_d.get("remaining_grams", 1000.0)
                init = s_d.get("initial_grams", 1000.0)
                pct = (rem / init * 100) if init > 0 else 0
                t_mount.add_row(
                    p_k,
                    s_k.upper(),
                    s_d.get("material", "PLA"),
                    s_d.get("color", "Black"),
                    f"{rem:.1f}g ({pct:.0f}%)"
                )
        console.print(t_mount)

    # Table of shelf
    if shelf:
        t_shelf = Table(title="📦 Offline Shelf Inventory", header_style="bold yellow", show_lines=True)
        t_shelf.add_column("Spool ID", style="dim", width=10)
        t_shelf.add_column("Material", style="bold white", width=22)
        t_shelf.add_column("Color", style="cyan", width=12)
        t_shelf.add_column("Remaining", justify="right", style="bold green", width=14)
        for s in shelf:
            t_shelf.add_row(
                s.get("id", ""),
                s.get("material", ""),
                s.get("color", ""),
                f"{s.get('remaining_grams', 0):.1f}g"
            )
        console.print(t_shelf)


@app.command("dispatch")
def cli_dispatch(
    model: str = typer.Argument(..., help="Model name or file path"),
    material: str = typer.Option("PLA Basic", "--material", "-m", help="Filament material (e.g. PLA-CF, ABS, Silk PLA)"),
    color: str = typer.Option("Black", "--color", "-c", help="Filament color"),
    grams: float = typer.Option(-1.0, "--grams", "-g", help="Model mass in grams. Read from the file when it is a sliced .3mf"),
    hours: float = typer.Option(0.0, "--hours", help="Print time in hours. Read from the file when it is a sliced .3mf"),
    multicolor: bool = typer.Option(False, "--multicolor", help="Flag if print requires multi-color AMS"),
    quote: bool = typer.Option(True, "--quote/--no-quote", help="Display commercial quote and unit economics"),
    as_json: bool = typer.Option(False, "--json", "-j", help="Output machine-readable JSON dispatch decision")
):
    """
    🏭 Intelligent constraint-aware fleet routing & commercial quoting.
    """
    from .dispatcher import FleetDispatcher, PrintJob
    dispatcher = FleetDispatcher()
    # The weight and the duration come off the file when it is a sliced .3mf.
    # Before this, neither did: `grams` defaulted to 30 and the print time was
    # never passed at all, so every job was costed as a 30-minute run. On a
    # 30-hour print that under-quoted by more than half while reporting the
    # margin as 65%.
    seconds = int(hours * 3600) if hours else 0
    info = read_slice_info(model) if Path(model).is_file() else None
    if info:
        grams = grams if grams != _GRAMS_UNSET else (info.get("grams") or _GRAMS_UNSET)
        seconds = seconds or int(info.get("eta_seconds") or 0)
    if grams == _GRAMS_UNSET:
        grams = 30.0

    job = PrintJob(
        id=f"job_{int(time.time())}",
        model_name=Path(model).name,
        material=material,
        color=color,
        mass_grams=grams,
        estimated_time_seconds=seconds or 1800,
        is_multicolor=multicolor,
        colors_required=[color] if not multicolor else ["Red", "Black"]
    )

    dec = dispatcher.dispatch_job(job)

    if as_json:
        print(json.dumps({
            "job_id": dec.job_id,
            "model_name": dec.model_name,
            "selected_printer": dec.selected_printer,
            "slot_index": dec.slot_index,
            "spool_swap_required": dec.spool_swap_required,
            "is_feasible": dec.is_feasible,
            "confidence_score": round(dec.confidence_score, 2),
            "rejection_reasons": dec.rejection_reasons,
            "rationale": dec.rationale,
            "quote": dec.quote
        }, indent=2))
        return

    if not dec.is_feasible:
        console.print(f"[bold red]❌ Dispatch Failed:[/bold red] {dec.rationale}")
        for p, r in dec.rejection_reasons.items():
            console.print(f"  • [yellow]{p}:[/yellow] {r}")
        return

    swap_str = "[bold yellow]Spool Swap Needed[/bold yellow]" if dec.spool_swap_required else "[bold green]Zero Swap Needed (Already Mounted)[/bold green]"
    
    quote_panel = ""
    if quote and dec.quote:
        q = dec.quote
        quote_panel = (
            f"\n💰 [bold]Commercial Quoting ({q['target_margin_percent']}% Target Margin):[/bold]\n"
            f"  • Material Cost: [green]${q['material_cost_usd']:.2f}[/green] | Power: [green]${q['electricity_cost_usd']:.2f}[/green] | Depreciation: [green]${q['depreciation_cost_usd']:.2f}[/green]\n"
            f"  • Total Cost: [bold white]${q['total_unit_cost_usd']:.2f}[/bold white] -> [bold yellow]Ask Price: ${q['recommended_selling_price_usd']:.2f}[/bold yellow] (Net Profit: [bold green]+${q['net_profit_usd']:.2f}[/bold green])"
        )

    console.print(Panel(
        f"[bold white]Optimal Fleet Assignment:[/bold white] [bold green]{dec.selected_printer}[/bold green] (Slot {dec.slot_index})\n"
        f"🧵 [bold]Material:[/bold] {material} ({color}) | [bold]Mass:[/bold] {grams:.1f}g\n"
        f"⚙️ [bold]Inventory Status:[/bold] {swap_str}\n"
        f"📝 [bold]Routing Rationale:[/bold] [dim]{dec.rationale}[/dim]"
        f"{quote_panel}",
        title=f"🎯 Fleet Dispatch: {job.model_name}",
        border_style="green"
    ))


@app.command("doctor")
def cli_doctor(
    printer: Optional[str] = typer.Option(None, "--printer", "-p", help="Printer name or key (default: your default printer)"),
    timeout: float = typer.Option(1.5, "--timeout", "-t", help="Probe timeout in seconds"),
    simulator: bool = typer.Option(False, "--simulator", "--sim", help="Probe Digital Twin simulator"),
    as_json: bool = typer.Option(False, "--json", "-j", help="Output machine-readable JSON diagnostic report")
):
    """
    🩺 Hardware Preflight Doctor: Sub-200ms probe for network, thermals, camera, and AMS.
    """
    printer = printer or config.default_printer
    known = list(config.printers.keys())
    from .aliases import PrinterAliasManager
    target_key = PrinterAliasManager.resolve_to_key(printer)
    is_sim = simulator or target_key.startswith("VIRTUAL_") or target_key in ["SIM", "VIRTUAL"]
    if not is_sim and target_key not in config.printers:
        err_msg = (f"There is no printer called {printer!r}." if printer else "No printer is set up yet.")
        guidance = f"The printers set up here are: {', '.join(known)}." if known else "There are no printers set up yet. Offer to look for them on this network with a3d_discover."
        if as_json:
            print(json.dumps({
                "error": err_msg,
                "guidance": guidance,
                "printers": known
            }, indent=2))
        else:
            console.print(f"[bold red]❌ {err_msg}[/bold red] {guidance}")
        raise typer.Exit(code=1)

    report: DoctorReport = HardwareDoctor.probe_printer(
        printer_key=target_key,
        timeout=timeout,
        use_simulator=simulator
    )

    if as_json:
        print(json.dumps(report.to_dict(), indent=2))
        return

    # Color code overall status
    status_color = "green" if report.overall_status == "HEALTHY" else ("yellow" if "WARNINGS" in report.overall_status or report.overall_status == "DEGRADED" else "red")
    status_badge = f"[bold {status_color}]{report.overall_status} (Score: {int(report.overall_score*100)}%)[/bold {status_color}]"

    net_badge = f"[green]Online ({report.network.latency_ms:.1f}ms)[/green]" if report.network.ping_ok else "[red]Offline / Unreachable[/red]"
    cam_badge = "[green]Stream Active (RTSP 554)[/green]" if report.camera.rtsp_reachable else "[yellow]Port Inactive[/yellow]"
    
    t = report.thermals
    therm_str = f"Nozzle: [yellow]{t.nozzle_temp_c}°C[/yellow] | Bed: [red]{t.bed_temp_c}°C[/red] | Chamber: [blue]{t.chamber_temp_c}°C[/blue]"

    spool_str = "[yellow]Low Mass Warning[/yellow]" if report.spools.has_low_warning else "[green]Nominal[/green]"

    recs = "\n".join([f"  • {r}" for r in report.recommendations])

    console.print(Panel(
        f"[bold white]Target Machine:[/bold white] [bold cyan]{report.printer_name}[/bold cyan] ({report.host})\n"
        f"[bold white]Overall Health:[/bold white] {status_badge} [dim]Probe Time: {report.duration_ms:.1f}ms[/dim]\n\n"
        f"🌐 [bold]Network Latency:[/bold] {net_badge} (HTTP 80: {'✅' if report.network.http_port_ok else '❌'}, SDCP 3030: {'✅' if report.network.sdcp_port_ok else '❌'})\n"
        f"🌡️ [bold]Thermal Sanity:[/bold] {therm_str}\n"
        f"📷 [bold]Chamber Camera:[/bold] {cam_badge} [dim]{report.camera.rtsp_url}[/dim]\n"
        f"🧵 [bold]FilamentVault:[/bold] {spool_str} ({len(report.spools.loaded_spools)} active slots)\n\n"
        f"📋 [bold]Doctor Recommendations:[/bold]\n"
        f"{recs}",
        title="🩺 Hardware Preflight Diagnostic",
        border_style=status_color
    ))


def _exit_with_parent(every_s: float = 2.0) -> None:
    """End this process when whatever started it is gone. A listener outliving
    the eagle keeps one of the printer's few connections for nothing."""
    import threading

    parent = os.getppid()

    def watch():
        while True:
            time.sleep(every_s)
            if os.getppid() != parent:
                os._exit(0)

    threading.Thread(target=watch, name="parent-watch", daemon=True).start()


@app.command("listen")
def cli_listen(
    printer: Optional[str] = typer.Option(None, "--printer", "-p", help="Printer name or key (default: your default printer)"),
    fleet: bool = typer.Option(False, "--fleet", "-f", help="Monitor and stream the entire fleet concurrently"),
    interval: float = typer.Option(2.0, "--interval", "-i", help="Telemetry polling interval in seconds"),
    event_file: Optional[Path] = typer.Option(None, "--event-file", "-o", help="Dynamic Island event JSON file (default: a3d_dynamic_island.json in your private runtime dir)"),
    once: bool = typer.Option(False, "--once", help="Poll a single frame, write event, and exit"),
    simulator: bool = typer.Option(False, "--simulator", "--sim", help="Stream from Digital Twin simulator"),
    as_json: bool = typer.Option(False, "--json", "-j", help="Output raw JSON stream to stdout")
):
    """
    🎧 Headless SDCP Event Streamer & Dynamic Island HUD Bridge.
    Emits live JSON progress rings, spool low alerts, and standby state.
    """
    printer = printer or config.default_printer
    from .streamer import SDCPEventStreamer, FleetSDCPStreamer

    if not once:
        _exit_with_parent()
    known = list(config.printers.keys())

    if fleet:
        f_streamer = FleetSDCPStreamer(
            printer_keys=config.addressable_printer_keys(),
            use_simulator=simulator,
            event_file=event_file
        )
        if not as_json:
            console.print(f"[bold cyan]🎧 Listening to Fleet ({', '.join(f_streamer.printer_keys)}) SDCP Stream...[/bold cyan] [dim](Writing to {f_streamer.event_file})[/dim]")

        async def run_fleet_listener():
            if once:
                if not f_streamer.printer_keys:
                    msg = {"event": "none", "detail": "No printers are set up yet."}
                    print(json.dumps(msg) if as_json else msg["detail"])
                    return
                event = await f_streamer.poll_fleet_frame()
                if as_json:
                    print(event.to_json())
                else:
                    console.print(f"[{event.color}]● [{event.printer}] {event.title}:[/] {event.detail} [dim]({event.badge})[/dim]")
                return

            async for event in f_streamer.stream_fleet_generator(interval=interval):
                if as_json:
                    print(event.to_json())
                    sys.stdout.flush()
                else:
                    console.print(f"[{event.color}]● [{event.printer}] {event.title}:[/] {event.detail} [dim]({event.badge})[/dim]")

        try:
            asyncio.run(run_fleet_listener())
        except KeyboardInterrupt:
            if not as_json:
                console.print("\n[dim]Fleet streamer terminated by user.[/dim]")
        return

    from .aliases import PrinterAliasManager
    target_key = PrinterAliasManager.resolve_to_key(printer)
    is_sim = simulator or target_key.startswith("VIRTUAL_") or target_key in ["SIM", "VIRTUAL"]
    if not is_sim and target_key not in config.printers:
        err_msg = (f"There is no printer called {printer!r}." if printer else "No printer is set up yet.")
        guidance = f"The printers set up here are: {', '.join(known)}." if known else "There are no printers set up yet. Offer to look for them on this network with a3d_discover."
        if as_json:
            print(json.dumps({
                "error": err_msg,
                "guidance": guidance,
                "printers": known
            }, indent=2))
        else:
            console.print(f"[bold red]❌ {err_msg}[/bold red] {guidance}")
        raise typer.Exit(code=1)

    streamer = SDCPEventStreamer(
        printer_key=target_key,
        use_simulator=simulator,
        event_file=event_file
    )

    if not as_json:
        console.print(f"[bold cyan]🎧 Listening to {target_key} SDCP Stream...[/bold cyan] [dim](Writing to {streamer.event_file})[/dim]")

    async def run_listener():
        if once:
            event = await streamer.poll_single_frame()
            if as_json:
                print(event.to_json())
            else:
                console.print(f"[{event.color}]● {event.title}:[/] {event.detail} [dim]({event.badge})[/dim]")
            return

        async for event in streamer.stream_generator(interval=interval):
            if as_json:
                print(event.to_json())
                sys.stdout.flush()
            else:
                console.print(f"[{event.color}]● {event.title}:[/] {event.detail} [dim]({event.badge})[/dim]")

    try:
        asyncio.run(run_listener())
    except KeyboardInterrupt:
        if not as_json:
            console.print("\n[dim]Streamer terminated by user.[/dim]")


@app.command("light")
def cli_light(
    state: str = typer.Argument(..., help="'on' or 'off'"),
    printer: Optional[str] = typer.Option(None, "--printer", "-p", help="Printer name or key (default: your default printer)"),
    as_json: bool = typer.Option(False, "--json", "-j", help="Output machine-readable JSON")
):
    """
    💡 Turn chamber lights ON or OFF on your printer.
    """
    printer = printer or config.default_printer

    known = list(config.printers.keys())
    from .aliases import PrinterAliasManager
    target_key = PrinterAliasManager.resolve_to_key(printer)
    if target_key not in config.printers:
        err_msg = (f"There is no printer called {printer!r}." if printer else "No printer is set up yet.")
        guidance = f"The printers set up here are: {', '.join(known)}." if known else "There are no printers set up yet. Offer to look for them on this network with a3d_discover."
        if as_json:
            print(json.dumps({
                "error": err_msg,
                "guidance": guidance,
                "printers": known
            }, indent=2))
        else:
            console.print(f"[bold red]❌ {err_msg}[/bold red] {guidance}")
        raise typer.Exit(code=1)

    printer_info = config.get_printer(target_key) or {}
    from aethelark3d.drivers.factory import is_addressable
    if not is_addressable(printer_info):
        raise typer.BadParameter(
            f"Printer '{target_key}' has no address configured. "
            f"Set one with: a3d fleet --set-ip {target_key} <ip>")
    turn_on = state.lower().strip() in ["on", "1", "true"]

    from aethelark3d.drivers.factory import get_driver_for_printer

    async def set_light():
        driver = get_driver_for_printer(target_key)
        try:
            return await driver.set_chamber_light(turn_on)
        finally:
            await driver.disconnect()

    try:
        success = bool(asyncio.run(set_light()))
    except Exception:
        success = False

    if not success:
        err = f"{printer_info.get('name') or target_key} did not accept the light command."
        guidance = ("Tell the user the light did not change. Check the printer "
                    "is on and reachable with a3d_status, then try once more.")
        if as_json:
            print(json.dumps({"success": False, "printer": target_key,
                              "error": err, "guidance": guidance}, indent=2))
        else:
            console.print(f"[red]{err}[/red] {guidance}")
        raise typer.Exit(code=1)

    if as_json:
        print(json.dumps({"success": True, "printer": target_key, "light_on": turn_on}, indent=2))
        return

    status_str = "[bold green]ON 💡[/bold green]" if turn_on else "[dim]OFF 🌑[/dim]"
    console.print(f"[bold cyan]{target_key}:[/bold cyan] Chamber light set to {status_str}")


@app.command("speed")
def cli_speed(
    mode: str = typer.Argument(..., help="silent | balanced | sport | ludicrous (or 0 | 1 | 2 | 3)"),
    printer: Optional[str] = typer.Option(None, "--printer", "-p", help="Printer name or key (default: your default printer)"),
    as_json: bool = typer.Option(False, "--json", "-j", help="Output machine-readable JSON")
):
    """
    ⚙️ Set the live print speed mode (silent/balanced/sport/ludicrous). Works mid-print.
    """
    printer = printer or config.default_printer
    from aethelark3d.drivers.factory import get_driver_for_printer
    from .aliases import PrinterAliasManager

    NAMES = ["silent", "balanced", "sport", "ludicrous"]
    raw = str(mode).strip().lower()
    canon = raw if raw in NAMES else (NAMES[int(raw)] if raw in ("0", "1", "2", "3") else None)
    if canon is None:
        msg = (f"'{mode}' is not a speed mode. Use one of: "
               f"silent (0), balanced (1), sport (2), ludicrous (3).")
        if as_json:
            print(json.dumps({"error": msg, "modes": NAMES}, indent=2))
        else:
            console.print(f"[bold red]❌ {msg}[/bold red]")
        raise typer.Exit(code=1)

    known = list(config.printers.keys())
    target_key = PrinterAliasManager.resolve_to_key(printer)
    if target_key not in config.printers:
        msg = f"There is no printer called {printer!r}."
        guidance = f"The printers set up here are: {', '.join(known)}." if known else "There are no printers set up yet. Offer to look for them on this network with a3d_discover."
        if as_json:
            print(json.dumps({"error": msg, "guidance": guidance, "printers": known}, indent=2))
        else:
            console.print(f"[bold red]❌ {msg}[/bold red] {guidance}")
        raise typer.Exit(code=1)

    driver = get_driver_for_printer(target_key)
    ok = asyncio.run(driver.set_speed_mode(canon))

    if not ok:
        name = (config.get_printer(target_key) or {}).get("name") or target_key
        msg = (f"{name} did not accept {canon} mode. It only changes speed while "
               f"a print is running.")
        if as_json:
            print(json.dumps({"success": False, "printer": target_key,
                              "mode": canon, "error": msg}, indent=2))
        else:
            console.print(f"[red]{msg}[/red]")
        raise typer.Exit(code=1)

    if as_json:
        print(json.dumps({"success": True, "printer": target_key, "mode": canon,
                          "speed_index": NAMES.index(canon)}, indent=2))
        return
    console.print(f"[bold cyan]{target_key}:[/bold cyan] print speed set to "
                  f"[bold green]{canon.upper()}[/bold green] ⚙️")


#: States in which there is a job on the machine to pause or stop.
_JOB_STATES = {"PRINTING", "PAUSED", "PREPARING", "HEATING", "LEVELING", "UPLOADING"}


def _control_one(action: str, target_key: str, use_simulator: bool = False):
    """Pause, resume or stop the job on one printer: (payload, exit code).

    The printer's own state is read first, so pausing an idle machine or
    stopping one with nothing on it is never reported as done.
    """
    from .drivers.factory import get_driver_for_printer

    name = (config.get_printer(target_key) or {}).get("name") or target_key
    driver = get_driver_for_printer(target_key, use_simulator=use_simulator)
    state = _printer_state(target_key, use_simulator)

    if state == "DISCONNECTED":
        return {"error": f"{name} is not answering, so nothing was sent.",
                "guidance": "Tell the user the printer is unreachable, and that "
                            "the printer's own screen can pause or stop it.",
                "printer": target_key, "state": state}, 1
    if action == "resume" and state != "PAUSED":
        return {"error": f"The print on {name} is not paused.",
                "guidance": "Tell the user there is nothing to resume.",
                "printer": target_key, "state": state}, 1
    if action in ("pause", "stop") and state not in _JOB_STATES:
        return {"error": f"Nothing is printing on {name} right now.",
                "guidance": "Tell the user nothing is printing.",
                "printer": target_key, "state": state}, 1
    if action == "pause" and state == "PAUSED":
        return {"ok": True, "printer": target_key, "action": action, "state": state,
                "message": f"The print on {name} is already paused."}, 0

    call = {"pause": driver.pause_print, "resume": driver.resume_print,
            "stop": driver.stop_print}[action]
    try:
        ok = bool(asyncio.run(call()))
    except Exception:
        ok = False
    done = {"pause": "paused", "resume": "resumed", "stop": "stopped"}[action]
    if not ok:
        return {"error": f"{name} did not confirm that the print {done}.",
                "guidance": "Tell the user it may not have worked and to check "
                            "the printer's screen.",
                "printer": target_key, "state": state}, 1
    return {"ok": True, "printer": target_key, "action": action,
            "message": f"The print on {name} is {done}."}, 0


def _printer_state(key: str, use_simulator: bool = False) -> str:
    from .drivers.factory import get_driver_for_printer
    try:
        t = asyncio.run(get_driver_for_printer(key, use_simulator=use_simulator).get_telemetry())
        st = str(getattr(t.state, "value", t.state)).upper()
    except Exception:
        st = "DISCONNECTED"
    return st.rsplit(".", 1)[-1]


def _control_print(action: str, printer: Optional[str], as_json: bool,
                   use_simulator: bool = False) -> None:
    """Pause, resume or stop a print on the named printer, on the one printer
    that is busy when none is named, or on every busy printer for 'all'."""
    from .aliases import PrinterAliasManager

    def _out(payload: dict, code: int) -> None:
        if as_json:
            print(json.dumps(payload, indent=2))
        elif code == 0:
            console.print(f"[bold green]✓[/bold green] {payload.get('message', '')}")
        else:
            console.print(f"[bold red]❌ {payload.get('error', '')}[/bold red] "
                          f"{payload.get('guidance', '')}")
        if code:
            raise typer.Exit(code=code)

    def _label(key: str) -> str:
        return (config.get_printer(key) or {}).get("name") or key

    said = str(printer or "").strip()
    everyone = said.lower() in ("all", "every", "everything", "all printers", "every printer")
    if everyone or not said:
        wanted = {"PAUSED"} if action == "resume" else _JOB_STATES
        busy = [k for k in config.addressable_printer_keys()
                if _printer_state(k, use_simulator) in wanted]
        if not busy:
            _out({"error": ("No print is paused on any printer." if action == "resume"
                            else "Nothing is printing on any printer right now."),
                  "guidance": f"Tell the user there is nothing to {action}."}, 1)
            return
        if len(busy) > 1 and not everyone:
            names = [_label(k) for k in busy]
            _out({"error": f"{', '.join(names[:-1])} and {names[-1]} are all busy.",
                  "guidance": f"Ask the user which printer to {action}, or all of them, "
                              "then call again with printer set to its name or 'all'.",
                  "printers": names}, 1)
            return
        if everyone:
            results = [_control_one(action, k, use_simulator) for k in busy]
            ok = [p for p, c in results if c == 0]
            bad = [p for p, c in results if c != 0]
            payload = {"ok": not bad, "action": action,
                       "message": " ".join(p["message"] for p in ok),
                       "printers": [p.get("printer") for p, _ in results]}
            if bad:
                payload["error"] = " ".join(p["error"] for p in bad)
                payload["guidance"] = bad[0].get("guidance", "")
            _out(payload, 0 if not bad else 1)
            return
        said = busy[0]

    target_key = PrinterAliasManager.resolve_to_key(said)
    is_sim = use_simulator or target_key.startswith("VIRTUAL_") or target_key in ("SIM", "VIRTUAL")
    if not is_sim and target_key not in config.printers:
        known = [_label(k) for k in config.printers]
        _out({"error": f"There is no printer called {said!r}.",
              "guidance": (f"The printers set up here are: {', '.join(known)}."
                           if known else "There are no printers set up yet. Offer to "
                           "look for them on this network with a3d_discover."),
              "printers": known}, 1)
        return
    payload, code = _control_one(action, target_key, use_simulator)
    _out(payload, code)


@app.command("pause")
def cli_pause(
    printer: Optional[str] = typer.Option(None, "--printer", "-p", help="Printer name or key (default: your default printer)"),
    use_simulator: bool = typer.Option(False, "--simulator", "--sim", help="Control the simulator"),
    as_json: bool = typer.Option(False, "--json", "-j", help="Output machine-readable JSON"),
):
    """⏸ Pause the print on a printer. It can be resumed."""
    _control_print("pause", printer, as_json, use_simulator)


@app.command("resume")
def cli_resume(
    printer: Optional[str] = typer.Option(None, "--printer", "-p", help="Printer name or key (default: your default printer)"),
    use_simulator: bool = typer.Option(False, "--simulator", "--sim", help="Control the simulator"),
    as_json: bool = typer.Option(False, "--json", "-j", help="Output machine-readable JSON"),
):
    """▶ Resume a paused print."""
    _control_print("resume", printer, as_json, use_simulator)


@app.command("stop")
def cli_stop(
    printer: Optional[str] = typer.Option(None, "--printer", "-p", help="Printer name or key (default: your default printer)"),
    use_simulator: bool = typer.Option(False, "--simulator", "--sim", help="Control the simulator"),
    as_json: bool = typer.Option(False, "--json", "-j", help="Output machine-readable JSON"),
):
    """⏹ Stop the print on a printer. It cannot be resumed afterwards."""
    _control_print("stop", printer, as_json, use_simulator)


@app.command("beacon")
def cli_beacon(
    printer: Optional[str] = typer.Argument(None, help="Printer key or alias (default: your default printer)"),
    mode: str = typer.Option("spotlight", "--mode", "-m", help="Beacon mode: spotlight, wave, chime, tap, display"),
    simulator: bool = typer.Option(False, "--simulator", "--sim", help="Run on simulator"),
    as_json: bool = typer.Option(False, "--json", "-j", help="Output machine-readable JSON")
):
    """
    🔦 Physical hardware beacon to locate and identify machines in a room.
    """
    printer = printer or config.default_printer
    from .beacon import PrinterBeacon, BeaconMode

    known = list(config.printers.keys())
    from .aliases import PrinterAliasManager
    target_key = PrinterAliasManager.resolve_to_key(printer)
    is_sim = simulator or target_key.startswith("VIRTUAL_") or target_key in ["SIM", "VIRTUAL"]
    if not is_sim and target_key not in config.printers:
        err_msg = (f"There is no printer called {printer!r}." if printer else "No printer is set up yet.")
        guidance = f"The printers set up here are: {', '.join(known)}." if known else "There are no printers set up yet. Offer to look for them on this network with a3d_discover."
        if as_json:
            print(json.dumps({
                "error": err_msg,
                "guidance": guidance,
                "printers": known
            }, indent=2))
        else:
            console.print(f"[bold red]❌ {err_msg}[/bold red] {guidance}")
        raise typer.Exit(code=1)

    b_mode = BeaconMode.SPOTLIGHT
    clean_mode = mode.lower().strip()
    if "wave" in clean_mode:
        b_mode = BeaconMode.WAVE
    elif "chime" in clean_mode:
        b_mode = BeaconMode.CHIME
    elif "tap" in clean_mode or "touch" in clean_mode:
        b_mode = BeaconMode.TAP
    elif "display" in clean_mode or "lcd" in clean_mode:
        b_mode = BeaconMode.DISPLAY

    if b_mode == BeaconMode.TAP:
        res = asyncio.run(PrinterBeacon.listen_for_bed_tap(use_simulator=simulator))
        out = {
            "success": res.detected,
            "tapped_printer": res.printer_key,
            "sensor": res.sensor_type,
            "pressure_grams": res.pressure_grams,
            "message": res.message
        }
    else:
        out = asyncio.run(PrinterBeacon.trigger_beacon(target_key, mode=b_mode, use_simulator=simulator))

    if as_json:
        print(json.dumps(out, indent=2))
        if not out.get("success"):
            raise typer.Exit(code=1)
        return

    if out.get("success"):
        console.print(f"[bold green]✓[/bold green] [bold cyan]{out.get('printer_key', printer)}[/bold cyan]: {out.get('message')}")
    else:
        console.print(f"[bold red]✗ Beacon failed:[/bold red] {out.get('error') or out.get('message', 'Error')}")


def _design_id(query_or_id: str) -> Optional[int]:
    from .providers.makerworld import MakerWorldProvider
    try:
        return MakerWorldProvider.parse_model_id(query_or_id)
    except ValueError:
        pass
    from .api import search_models
    found = search_models(query_or_id, limit=1)
    return found[0]["id"] if found else None


@app.command("reviews")
def cli_reviews(
    query_or_id: str = typer.Argument(..., help="Model id, link, or name"),
    as_json: bool = typer.Option(False, "--json", "-j", help="Output machine-readable JSON"),
):
    """⭐ What people who printed a model thought of it."""
    from .providers.makerworld import MakerWorldProvider
    design = _design_id(query_or_id)
    if design is None:
        out = {"error": f"No model matching {query_or_id!r}.",
               "guidance": "Ask which model they mean."}
    else:
        try:
            out = MakerWorldProvider().get_reviews(str(design))
        except Exception as e:
            out = {"error": f"MakerWorld did not answer: {e}"[:200],
                   "guidance": "Say the reviews could not be read right now."}
    if as_json:
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        console.print(out)
    raise typer.Exit(1 if "error" in out else 0)


@app.command("open")
def cli_open(
    query_or_id: str = typer.Argument(..., help="Model id, link, or name"),
    as_json: bool = typer.Option(False, "--json", "-j", help="Output machine-readable JSON"),
):
    """🌐 Open a model's page in the browser."""
    import webbrowser
    design = _design_id(query_or_id)
    if design is None:
        out = {"success": False, "error": f"No model matching {query_or_id!r}."}
    else:
        url = f"https://makerworld.com/en/models/{design}"
        opened = webbrowser.open(url)
        out = {"success": bool(opened), "url": url,
               "detail": "Opened its MakerWorld page." if opened else "No browser opened."}
    print(json.dumps(out, indent=2) if as_json else out)
    raise typer.Exit(0 if out.get("success") else 1)


def _learn_identity(key: str) -> None:
    """Name an elegoolink printer after the model it reports, once a code works."""
    slot = config.get_printer(key) or {}
    if str(slot.get("protocol") or "").lower() != "elegoolink" or not slot.get("host"):
        return
    try:
        import requests
        res = requests.get(f"http://{slot['host']}/system/info",
                           params={"X-Token": slot.get("access_code") or ""}, timeout=2)
        info = res.json().get("system_info", {}) if res.status_code == 200 else {}
    except Exception:
        return
    model = str(info.get("machine_model") or "").strip()
    if not model:
        return
    generic = not slot.get("name") or re.fullmatch(r"elegoo [0-9a-f]{6}", slot["name"].strip().lower())
    taken = {(p or {}).get("name") for k, p in config.printers.items() if k != key}
    slot["model"] = model
    if generic and model not in taken:
        slot["name"] = model
    fw = (info.get("software_version") or {}).get("ota_version")
    if fw:
        slot["firmware"] = fw
    config.save()


@app.command("pair")
def cli_pair(
    printer: str = typer.Argument(..., help="Printer key or alias (e.g. CC2, ELEGOO_7526B5)"),
    access_code: str = typer.Argument("", help="The code shown on the printer's touchscreen, e.g. Ab3dEf"),
    code: str = typer.Option("", "--code", help="Same as the positional access code"),
    as_json: bool = typer.Option(False, "--json", "-j", help="Output machine-readable JSON"),
):
    """
    🔑 Give a printer its access code, then prove the code works.

    A Centauri Carbon 2 and its kin gate the local API behind a code shown on
    the printer screen. Discovery finds the machine and marks it as needing
    one; this is how the code gets in. It stores the code and then CONNECTS,
    so the answer is "paired" or "that code was refused" — measured, not
    assumed.
    """
    import asyncio

    key = printer.strip().upper().replace("-", "_")
    slot = config.get_printer(key)
    if slot is None:
        # allow an alias
        from .aliases import PrinterAliasManager
        resolved = PrinterAliasManager.get_aliases().get(printer.strip().upper())
        if resolved:
            key, slot = resolved, config.get_printer(resolved)
    if slot is None:
        msg = f"No printer called {printer!r}. Run discover first, or check the name."
        print(json.dumps({"success": False, "error": msg}) if as_json else f"[red]{msg}[/red]")
        raise typer.Exit(1)

    access_code = (code or access_code or "").strip()
    uses_codes = str(slot.get("protocol") or "").lower() != "sdcp"
    if not access_code or not uses_codes:
        from .readiness import check
        c = check(key)
        name = slot.get("name") or key
        if c["ready"]:
            _learn_identity(key)
            name = (config.get_printer(key) or {}).get("name") or name
            saved = uses_codes and bool(slot.get("access_code"))
            out = {"success": True, "printer": key,
                   "detail": f"{name} is connected" + (" with its saved access code."
                             if saved else " and needs no access code.")}
        elif c["needs_access_code"]:
            out = {"success": False, "printer": key, "needs_access_code": True,
                   "error": f"{name} needs its access code.",
                   "guidance": "Ask the user to read the access code from the "
                               "printer's network settings, then call a3d_pair "
                               "with it."}
        else:
            out = {"success": False, "printer": key,
                   "error": f"{name} is not reachable: {c['why']}.",
                   "guidance": "Tell the user what to check. An access code "
                               "will not help."}
        if as_json:
            print(json.dumps(out, indent=2))
        else:
            console.print(out.get("detail") or out.get("error"))
        raise typer.Exit(0 if out["success"] else 2)

    # Store the code first so build_driver picks it up, but do NOT yet clear
    # the needs-code flag: a stored code that the printer refuses has not
    # paired anything, and clearing the flag before proof would report a wrong
    # code as success.
    slot = config.get_printer(key)
    slot["access_code"] = access_code.strip()

    ok, detail = False, ""
    try:
        from .drivers.factory import get_driver_for_printer
        driver = get_driver_for_printer(key)
        ok = asyncio.run(driver.connect())
        detail = "connected" if ok else "the printer refused the code"
    except Exception as e:
        detail = f"{type(e).__name__}: {e}"

    # Only a code the printer actually accepted retires the flag and is saved
    # as durable state. A refused code is left in the slot for this process but
    # the printer stays marked as still needing one.
    if ok:
        config.set_access_code(key, access_code)
        _learn_identity(key)
    else:
        config.save()

    if as_json:
        print(json.dumps({"success": ok, "printer": key,
                          "detail": detail if ok else f"code stored but {detail}"}, indent=2))
        raise typer.Exit(0 if ok else 2)
    if ok:
        console.print(f"[bold green]✓ Paired {key}.[/bold green] The code works.")
    else:
        console.print(f"[yellow]Stored the code for {key}, but the printer did not accept it:[/yellow] {detail}")
        console.print("[dim]Re-check the code on the printer's screen (network settings).[/dim]")
    raise typer.Exit(0 if ok else 2)


@app.command("camera")
def cli_camera(
    printer: str = typer.Option(..., "--printer", "-p", help="Printer name or key"),
    state: str = typer.Option("on", "--state", help="on or off"),
    as_json: bool = typer.Option(False, "--json", "-j", help="Output machine-readable JSON"),
):
    """📷 Switch a printer's chamber camera stream on or off; prints its URL."""
    from .aliases import PrinterAliasManager
    from .drivers.factory import get_driver_for_printer

    key = PrinterAliasManager.resolve_to_key(printer)
    if key not in config.printers:
        out = {"success": False, "url": "", "error": f"There is no printer called {printer!r}."}
    else:
        name = (config.get_printer(key) or {}).get("name") or key
        on = str(state).strip().lower() not in ("off", "0", "false", "no")

        async def switch():
            driver = get_driver_for_printer(key)
            try:
                return await driver.set_camera(on)
            finally:
                try:
                    await driver.disconnect()
                except Exception:
                    pass

        try:
            url = asyncio.run(switch()) if hasattr(get_driver_for_printer(key), "set_camera") else None
        except Exception:
            url = None
        if not on:
            out = {"success": True, "url": "", "printer": key}
        elif url:
            out = {"success": True, "url": url, "printer": key}
        else:
            out = {"success": False, "url": "", "printer": key,
                   "error": f"{name}'s camera did not start."}
    if as_json:
        print(json.dumps(out))
    else:
        console.print(out.get("url") or out.get("error") or "Camera off.")
    raise typer.Exit(0 if out["success"] else 1)


@app.command("session")
def cli_session(
    as_json: bool = typer.Option(False, "--json", "-j", help="Output machine-readable JSON")
):
    """
    🔑 Take a MakerWorld sign-in handed over by the eagle (JSON on stdin).

    The eagle's browser is the one place the user signs in. It passes this
    module that site's cookies on stdin -- never as arguments, which any
    process can read -- as {"site": ..., "cookies": [{name, value, ...}]}.
    """
    import sys

    def answer(ok: bool, **fields):
        out = {"success": ok, **fields}
        if as_json:
            print(json.dumps(out, indent=2))
        else:
            console.print(out.get("error") or f"Signed in to {out.get('site')}.")
        if not ok:
            raise typer.Exit(code=1)

    try:
        blob = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        answer(False, error="The session was not readable JSON.")
    site = str(blob.get("site") or "").strip().lower()
    if site != "makerworld.com":
        answer(False, error=f"{site or 'That site'} is not an account this module uses.")
    cookies = [c for c in (blob.get("cookies") or []) if isinstance(c, dict) and c.get("name")]
    jar = {str(c["name"]): str(c.get("value") or "") for c in cookies}
    if not jar.get("token"):
        answer(False, site=site, error="That browser session is not signed in to MakerWorld.")
    config.makerworld_cookie = "; ".join(f"{k}={v}" for k, v in jar.items())
    config.makerworld_token = jar["token"]
    config.save()
    expires = max((float(c.get("expires") or 0) for c in cookies if c["name"] == "token"),
                  default=0.0)
    answer(True, site=site,
           expires=(time.strftime("%Y-%m-%d", time.localtime(expires)) if expires > 0 else None))


@app.command("canvas")
def cli_canvas(
    printer: str = typer.Argument(..., help="Printer name or key"),
    state: str = typer.Argument(..., help="'on' if a CANVAS is fitted, 'off' if not"),
    slots: int = typer.Option(4, "--slots", help="Filament slots it adds (4 per CANVAS unit)"),
    as_json: bool = typer.Option(False, "--json", "-j", help="Output machine-readable JSON")
):
    """
    🎨 Record that a Centauri Carbon has a CANVAS fitted, or no longer has one.
    """
    from .aliases import PrinterAliasManager
    from .printer_models import display_name, model_of

    key = PrinterAliasManager.resolve_to_key(printer)
    entry = config.get_printer(key)

    def leave(err: str, guidance: str):
        if as_json:
            print(json.dumps({"success": False, "error": err, "guidance": guidance}, indent=2))
        else:
            console.print(f"[red]{err}[/red] {guidance}")
        raise typer.Exit(code=1)

    if entry is None:
        known = ", ".join(config.printers) or "none"
        leave(f"There is no printer called {printer!r}.",
              f"The printers set up here are: {known}.")
    model = model_of(entry, key)
    # A CANVAS can be fitted to a CC1 after purchase, and a CC1 cannot be asked
    # whether it has one without an unprobed command. ElegooLink printers are
    # asked on every discovery, so what the user says would only be
    # overwritten by what the printer says.
    if model is not None and model.protocol == "elegoolink":
        leave(f"The {model.name} reports its own CANVAS.",
              "Call a3d_discover instead; it asks the printer whether a CANVAS "
              "is connected and records the answer.")
    on = state.strip().lower() in ("on", "yes", "true", "1", "fitted", "installed")
    entry["canvas"] = on
    entry["canvas_slots"] = max(1, int(slots)) if on else 0
    config.save()
    out = {"success": True, "printer": key, "model": display_name(entry, key),
           "canvas": on, "canvas_slots": entry["canvas_slots"]}
    if as_json:
        print(json.dumps(out, indent=2))
    else:
        console.print(f"[bold cyan]{key}[/bold cyan]: {out['model']}")


@app.command("alias")
def cli_alias(
    action: str = typer.Argument("list", help="'list', 'set', or 'remove'"),
    alias_name: Optional[str] = typer.Option(None, "--name", "-n", help="Friendly human callsign (e.g. Zeus, Hera, Titan)"),
    printer: Optional[str] = typer.Option(None, "--printer", "-p", help="Target machine key (e.g. CC1, CC2, C2_COMBO)"),
    as_json: bool = typer.Option(False, "--json", "-j", help="Output machine-readable JSON")
):
    """
    🏷️ Manage custom human printer callsigns and fleet aliases.
    """
    from .aliases import PrinterAliasManager

    act = action.lower().strip()
    if act == "set" and alias_name and printer:
        aliases = PrinterAliasManager.set_alias(alias_name, printer)
        if as_json:
            print(json.dumps({"success": True, "alias": alias_name.upper(), "printer": printer.upper(), "aliases": aliases}, indent=2))
            return
        console.print(f"[bold green]✓ Assigned callsign:[/bold green] [bold yellow]{alias_name.upper()}[/bold yellow] ➔ [bold cyan]{printer.upper()}[/bold cyan]")
        return

    elif act == "remove" and alias_name:
        removed = PrinterAliasManager.remove_alias(alias_name)
        if as_json:
            print(json.dumps({"success": removed, "alias": alias_name.upper()}, indent=2))
            return
        if removed:
            console.print(f"[bold green]✓ Removed callsign:[/bold green] {alias_name.upper()}")
        else:
            console.print(f"[yellow]Alias '{alias_name}' not found.[/yellow]")
        return

    # List aliases
    aliases = PrinterAliasManager.get_aliases()
    fleet = config.get_fleet()
    if as_json:
        print(json.dumps({
            "aliases": aliases,
            "total_aliases": len(aliases),
            "fleet": fleet
        }, indent=2))
        return

    t = Table(title="🏷️ Fleet Callsigns & Aliases", header_style="bold cyan", show_lines=True)
    t.add_column("Callsign / Name", style="bold yellow", width=18)
    t.add_column("Machine Key", style="bold white", width=14)
    t.add_column("Model", style="dim", width=24)
    t.add_column("IP Host", style="green", width=16)

    for al, pkey in aliases.items():
        pinfo = fleet.get(pkey, {})
        t.add_row(al, pkey, pinfo.get("model", "Custom"), pinfo.get("host", "Unassigned"))

    console.print(t)


@app.command("slicer")
def cli_slicer(
    path: str = typer.Argument("", help="Where ElegooSlicer is. Leave out to see what is used."),
    as_json: bool = typer.Option(False, "--json", "-j", help="Machine-readable"),
):
    """🔪 Show or set the ElegooSlicer used to prepare prints."""
    from .slicers.elegoo import find_slicer
    if path:
        target = Path(path).expanduser()
        if not target.is_file():
            msg = f"There is no file at {target}."
            print(json.dumps({"error": msg}) if as_json else msg)
            raise typer.Exit(1)
        config.slicer_path = str(target)
    found = find_slicer()
    if as_json:
        print(json.dumps({"slicer": found}))
    else:
        console.print(found or "[yellow]ElegooSlicer was not found.[/yellow] "
                               "Install it, or tell me where it is: a3d slicer /path/to/it")
    if not found:
        raise typer.Exit(1)


def main():
    """The `a3d` command: every failure leaves as a sentence, never a traceback.

    One boundary here rather than a try in every command. The eagle hands a
    module's stderr to the voice model, which reads it to a person.
    """
    from .slicers.elegoo import SlicerMissing

    as_json = "--json" in sys.argv or "-j" in sys.argv
    try:
        app()
    except SlicerMissing as exc:
        _leave(str(exc), exc.guidance, as_json)
    except Exception as exc:                             # noqa: BLE001
        _leave(f"That did not work: {exc}",
               "Tell the user it failed, in plain words. Do not retry it blindly.",
               as_json)


def _leave(message: str, guidance: str, as_json: bool) -> None:
    if as_json:
        print(json.dumps({"success": False, "error": message, "guidance": guidance}))
    else:
        console.print(f"[bold red]{message}[/bold red]")
    raise SystemExit(1)




@app.command("quote")
def cli_quote(
    model: str = typer.Argument("", help="A .3mf or .stl file, or a name to look up locally"),
    grams: float = typer.Option(_GRAMS_UNSET, "--grams", "-g",
                                help="Weight in grams. Read from the file when it is a sliced .3mf"),
    hours: float = typer.Option(0.0, "--hours",
                                help="Print time in hours. Read from the file when it is a sliced .3mf"),
    material: str = typer.Option("", "--material", "-m", help="PETG, PLA, ..."),
    margin: float = typer.Option(-1.0, "--margin", help="Target margin percent"),
    as_json: bool = typer.Option(False, "--json", "-j", help="Machine-readable"),
):
    """💰 What a print costs you, and what to charge for it, in your currency."""
    rates = costing.Rates.from_config(config.costing)
    if margin >= 0:
        rates.margin_percent = margin
        rates.provided.add("margin_percent")

    seconds = int(hours * 3600) if hours else 0
    weight = None if grams == _GRAMS_UNSET else grams
    used_material = material or None

    path = Path(model).expanduser() if model else None
    if path is not None and path.is_file():
        info = read_slice_info(path)
        if info:
            weight = weight if weight is not None else info.get("grams")
            seconds = seconds or int(info.get("eta_seconds") or 0)
            if not used_material:
                plates = info.get("plates") or []
                used_material = (plates[0].get("filament_type")
                                 if plates else None)

    result = costing.quote(weight, seconds, rates, used_material)
    result["model"] = Path(model).name if model else None

    if as_json:
        print(json.dumps(result, indent=2))
        return

    if not result.get("ok"):
        console.print(f"[bold red]{result['message']}[/bold red]")
        console.print(f"[yellow]{result['guidance']}[/yellow]")
        raise typer.Exit(1)

    cur = result.get("currency") or ""
    t = Table(show_header=True, header_style="bold")
    t.add_column("what"); t.add_column(cur, justify="right")
    t.add_column("how"); t.add_column("", justify="right")
    for row in result["breakdown"]:
        tag = "" if row["source"] == "yours" else "[dim]assumed[/dim]"
        t.add_row(row["what"], f"{row['amount']:.2f}", f"[dim]{row['how']}[/dim]", tag)
    console.print(t)

    head = result.get("model") or "this print"
    console.print(Panel(
        f"[bold white]{head}[/bold white]  "
        f"[dim]{result['grams']:.0f} g · {humanise(int(result['hours']*3600))}[/dim]\n\n"
        f"Costs you [bold]{result['cost']:.2f} {cur}[/bold] to deliver\n"
        f"Charge [bold yellow]{result['price']:.2f} {cur}[/bold yellow] "
        f"→ [green]+{result['profit']:.2f} {cur}[/green] "
        f"at {result['margin_percent']:.0f}% margin",
        title="💰 Quote", border_style="yellow"))


@app.command("rates")
def cli_rates(
    show: bool = typer.Option(False, "--show", help="Print what is configured"),
    currency: str = typer.Option("", "--currency", help="What your prices are in: lei (the default), EUR, USD, ..."),
    electricity: float = typer.Option(-1.0, "--electricity", help="price per kWh"),
    filament: float = typer.Option(-1.0, "--filament", help="price per kg, general"),
    material: str = typer.Option("", "--material", help="Set the price for one material"),
    watts: float = typer.Option(-1.0, "--watts", help="What the printer draws"),
    printer_price: float = typer.Option(-1.0, "--printer-price", help="What the printer cost"),
    printer_life: float = typer.Option(-1.0, "--printer-life", help="Hours the printer will last"),
    failure_rate: float = typer.Option(-1.0, "--failure-rate", help="Fraction of runs that fail, 0-1"),
    labour_minutes: float = typer.Option(-1.0, "--labour-minutes", help="Handling time per job"),
    labour_rate: float = typer.Option(-1.0, "--labour-rate", help="what an hour of your time is worth"),
    margin: float = typer.Option(-1.0, "--margin", help="Target margin percent"),
    as_json: bool = typer.Option(False, "--json", "-j", help="Machine-readable"),
):
    """⚙️ What things cost you. Set once; every quote uses them."""
    setters = {
        "electricity_lei_per_kwh": electricity,
        "filament_lei_per_kg": filament,
        "printer_watts": watts,
        "printer_price_lei": printer_price,
        "printer_lifetime_hours": printer_life,
        "failure_rate": failure_rate,
        "labour_minutes": labour_minutes,
        "labour_lei_per_hour": labour_rate,
        "margin_percent": margin,
    }
    changed = {}
    for name, value in setters.items():
        if value >= 0:
            config.set_rate(name, value)
            changed[name] = value
    if material and filament >= 0:
        config.set_filament_price(material, filament)
        changed[f"filament_prices.{material.upper()}"] = filament
    if currency.strip():
        config.set_rate("currency", currency.strip())
        changed["currency"] = currency.strip()

    rates = costing.Rates.from_config(config.costing)
    payload = {
        "ok": True,
        "currency": rates.currency,
        "changed": changed,
        "rates": {k: v for k, v in vars(rates).items() if k != "provided"},
        "assumed": sorted(k for k in vars(rates)
                          if k not in ("provided",) and k not in rates.provided),
    }
    if as_json:
        print(json.dumps(payload, indent=2))
        return

    t = Table(show_header=True, header_style="bold")
    t.add_column("figure"); t.add_column("value", justify="right"); t.add_column("")
    for name, value in payload["rates"].items():
        tag = "" if name in rates.provided else "[dim]assumed[/dim]"
        t.add_row(name.replace("_", " "), str(value), tag)
    console.print(t)
    if changed:
        console.print(f"[green]Saved:[/green] {', '.join(changed)}")


@app.command("register")
def cli_register(
    path: str = typer.Option("", "--path",
                             help="Override the modules directory "
                                  "(default: ~/.aethelark/modules)."),
    as_json: bool = typer.Option(False, "--json", "-j",
                                 help="Machine-readable output."),
):
    """🔌 Install the Space-Eagle module socket for a3d.

    Copies the shipped manifest and its Dynamic Island assets into the modules
    directory the eagle reads. It COPIES, never re-renders: the manifest and the
    island cards are hand-authored data, and regenerating them would silently
    drop the card configuration. Run once after installing the module; safe to
    re-run. See register.py.
    """
    from .register import RegistrationError, register, registration_summary

    modules_dir = path or None
    try:
        manifest_path = register(modules_dir)
    except RegistrationError as exc:
        if as_json:
            print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        else:
            console.print(f"[bold red]❌ registration failed:[/bold red] {exc}")
        raise typer.Exit(code=1)

    names, cards = registration_summary(manifest_path)
    island_dir = manifest_path.parent / "island"
    assets = sorted(p.name for p in island_dir.iterdir()
                    if p.is_file()) if island_dir.is_dir() else []

    if as_json:
        print(json.dumps({
            "ok": True,
            "module": "a3d",
            "manifest": str(manifest_path),
            "tools": names,
            "tool_count": len(names),
            "island_cards": cards,
            "island_assets": assets,
        }, indent=2))
        return

    console.print(f"[bold green]✓ a3d registered[/bold green] → {manifest_path}")
    console.print(f"  {len(names)} tools, {len(assets)} island asset(s): "
                  f"{', '.join(assets) or 'none'}")
    console.print("[dim]Relaunch the eagle; its tools are now live.[/dim]")


# Last, not in the middle: under `python -m aethelark3d.cli` this runs as soon
# as it is reached, and every command defined below it -- quote, rates,
# register -- did not exist yet.
if __name__ == "__main__":
    main()
