"""
Unit tests verifying universal --json flags across all Aethelark-3D CLI commands.
Machine-contract guarantee for Space-Eagle and external orchestrators.
"""

import json
from pathlib import Path
from typer.testing import CliRunner
from unittest.mock import patch, MagicMock

from aethelark3d.cli import app
from aethelark3d.models import UniversalDesign, UniversalProfile, SearchResult

runner = CliRunner()


def test_cli_search_json():
    fake_item = UniversalDesign(
        id=99999,
        title="Speed Starship",
        url="http://fake/99999",
        download_count=400,
        like_count=120,
        print_count=80,
        profiles=[UniversalProfile(id=1, title="Default", is_default=True, weight_g=35.0, prediction_s=2400)]
    )
    fake_res = SearchResult(query="starship", total=1, platform="makerworld", designs=[fake_item])

    with patch("aethelark3d.providers.makerworld.MakerWorldProvider.search", return_value=fake_res):
        res = runner.invoke(app, ["search", "starship", "--json"])
        assert res.exit_code == 0
        data = json.loads(res.stdout)
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["id"] == 99999
        assert data[0]["title"] == "Speed Starship"


def test_cli_fleet_json():
    res = runner.invoke(app, ["fleet", "--json"])
    assert res.exit_code == 0
    data = json.loads(res.stdout)
    assert isinstance(data, list)
    assert len(data) >= 3
    
    # Check CC1, CC2, C2_COMBO are present
    keys = [item["key"] for item in data]
    assert "CC1" in keys
    assert "CC2" in keys
    assert "C2_COMBO" in keys

    # Verify schema fields
    cc1 = next(item for item in data if item["key"] == "CC1")
    assert "capabilities" in cc1
    assert cc1["capabilities"]["nozzle_material"] == "hardened_steel"
    assert "loaded_spools" in cc1


def test_cli_status_json():
    # Load a known spool first so the `spool` alias below has something
    # real to carry, regardless of what any other test left mounted.
    load_res = runner.invoke(app, ["spool", "-p", "CC1", "-s", "1", "-l", "Bambu Silk PLA", "-c", "Red", "-g", "14"])
    assert load_res.exit_code == 0

    res = runner.invoke(app, ["status", "--printer", "CC1", "--sim", "--json"])
    assert res.exit_code == 0
    data = json.loads(res.stdout)
    assert data["printer"] == "CC1"
    assert "telemetry" in data
    if data["online"]:
        assert {"nozzle_temp", "bed_temp"} <= set(data["telemetry"])
    else:
        assert data["telemetry"] == {}
    assert "print_info" in data
    # MODULE_CONTRACT.md structural key for the a3d card's filament row:
    # `spool` must be an object, not just present under `spools`.
    assert "spool" in data
    assert data["spool"]["material"] == "Bambu Silk PLA"
    assert data["spool"]["remaining_grams"] == 14.0
    # Deferred per WORK_BRIEF item 6 (PurgeX opt-in): the key the card reads
    # is present so it resolves cleanly, but stays null -- no fabricated
    # savings number until item 5's naming and item 6's opt-in are settled.
    assert "purgex_savings_percent" in data
    assert data["purgex_savings_percent"] is None


def test_cli_spool_and_vault_json(tmp_path):
    # 1. Spool load
    res = runner.invoke(app, ["spool", "-p", "CC1", "-s", "1", "-l", "eSUN PETG", "-c", "Blue", "-g", "950", "--json"])
    assert res.exit_code == 0
    data = json.loads(res.stdout)
    assert data["success"] is True
    assert data["loaded_spool"]["material"] == "eSUN PETG"

    # 2. Spool status query
    res = runner.invoke(app, ["spool", "-p", "CC1", "--json"])
    assert res.exit_code == 0
    data = json.loads(res.stdout)
    assert data["printer"] == "CC1"
    assert "slot_1" in data["slots"]
    # Same `spool` alias the printer card reads, on `a3d spool` as well as
    # `a3d status` -- both must carry the object shape, not just the
    # slot-keyed dict under `slots`.
    assert data["spool"]["material"] == "eSUN PETG"
    assert data["spool"]["color"] == "Blue"

    # 3. Vault command
    res = runner.invoke(app, ["vault", "--json"])
    assert res.exit_code == 0
    data = json.loads(res.stdout)
    assert "mounted" in data
    assert "shelf" in data
    assert data["total_mounted_slots"] >= 1


def test_cli_dispatch_json():
    res = runner.invoke(app, ["dispatch", "bracket.stl", "-m", "PLA-CF", "-c", "Black", "-g", "45", "--json"])
    assert res.exit_code == 0
    data = json.loads(res.stdout)
    assert data["is_feasible"] is True
    assert data["selected_printer"] in ["CC1", "CC2"]
    assert "quote" in data
    assert data["quote"]["material_cost_usd"] > 0


def test_cli_doctor_json():
    res = runner.invoke(app, ["doctor", "--printer", "CC1", "--sim", "--json"])
    assert res.exit_code == 0
    data = json.loads(res.stdout)
    assert data["printer_key"] == "CC1"
    assert data["overall_status"] in ["HEALTHY", "HEALTHY_WITH_WARNINGS", "DEGRADED"]
    assert data["network"]["ping_ok"] is True
    assert data["thermals"]["nozzle_healthy"] is True
    assert data["camera"]["rtsp_reachable"] is True


def test_cli_listen_once_json(tmp_path):
    event_file = tmp_path / "test_pill_event.json"
    res = runner.invoke(app, ["listen", "--printer", "CC1", "--sim", "--once", "--json", "--event-file", str(event_file)])
    assert res.exit_code == 0
    data = json.loads(res.stdout)
    assert data["module"] == "3d"
    assert data["event"] in ["print_progress", "printer_standby", "spool_low_alert"]
    assert event_file.exists()
