"""Tests for unknown printer refusal and printer removal.

Covers:
1. Unknown printer refusal with real keys named and no invented telemetry (Task 1 / BEHAVIOR_SPEC 12.3 & 5.13).
2. Refusal when fleet is empty without invented telemetry.
3. Explicit printer removal via API and CLI, persisting across reloads and cleaning references (Task 2).
"""

import json
from unittest.mock import patch
import pytest
from typer.testing import CliRunner

from aethelark3d.cli import app
from aethelark3d.config import Config, config
from aethelark3d.drivers.factory import get_driver_for_printer
from aethelark3d.spools import FilamentVault


@pytest.fixture
def runner():
    return CliRunner()


# ============================================================================
# TASK 1: UNKNOWN PRINTER REFUSAL
# ============================================================================

def test_unknown_printer_status_json_refusal(runner):
    """An unknown printer key must be refused, naming valid printers and returning no telemetry."""
    res = runner.invoke(app, ["status", "--printer", "CC9", "--json"])
    assert res.exit_code == 1

    data = json.loads(res.stdout)
    assert "There is no printer called 'CC9'." in data["error"]
    assert "The printers set up here are:" in data["guidance"]
    assert "CC1" in data["printers"]
    assert "telemetry" not in data
    assert "print_info" not in data
    assert "state" not in data


def test_unknown_printer_status_human_refusal(runner):
    """Human-formatted refusal output contains the spec-prescribed message."""
    res = runner.invoke(app, ["status", "--printer", "CC9"])
    assert res.exit_code == 1
    assert "There is no printer called 'CC9'." in res.stdout
    assert "The printers set up here are:" in res.stdout


def test_empty_fleet_status_refusal(runner):
    """With an empty fleet, status refuses without returning a telemetry block."""
    with patch.object(config, "_data", {"printers": {}, "default_printer": ""}):
        # Querying an unknown key on empty fleet
        res = runner.invoke(app, ["status", "--printer", "CC9", "--json"])
        assert res.exit_code == 1
        data = json.loads(res.stdout)
        assert data["error"] == "There is no printer called 'CC9'."
        # The guidance names the next step: an empty fleet is an invitation to
        # discover, not a dead end.
        assert data["guidance"] == ("There are no printers set up yet. Offer to look "
                                    "for them on this network with a3d_discover.")
        assert data["printers"] == []
        assert "telemetry" not in data

        # Querying default status on empty fleet
        res_default = runner.invoke(app, ["status", "--json"])
        assert res_default.exit_code == 1
        data_def = json.loads(res_default.stdout)
        assert data_def["error"] == "No printers configured in fleet."
        assert data_def["guidance"] == ("There are no printers set up yet. Offer to "
                                        "look for them on this network with a3d_discover.")
        assert "telemetry" not in data_def


def test_driver_factory_raises_for_unknown_printer():
    """get_driver_for_printer must raise KeyError for unconfigured printer, never dialing 127.0.0.1."""
    with pytest.raises(KeyError) as exc_info:
        get_driver_for_printer("CC9", use_simulator=False)
    assert "There is no printer called 'CC9'." in str(exc_info.value)


def test_other_cli_commands_refuse_unknown_printer(runner):
    """Doctor, spool, light, beacon, and listen all refuse unknown printer keys."""
    commands = [
        ["doctor", "--printer", "NONEXISTENT", "--json"],
        ["spool", "--printer", "NONEXISTENT", "--json"],
        ["light", "on", "--printer", "NONEXISTENT", "--json"],
        ["beacon", "NONEXISTENT", "--json"],
        ["listen", "--printer", "NONEXISTENT", "--json", "--once"],
    ]
    for cmd in commands:
        res = runner.invoke(app, cmd)
        assert res.exit_code == 1, f"Failed for {cmd}"
        data = json.loads(res.stdout)
        assert "There is no printer called 'NONEXISTENT'." in data["error"]
        assert "printers" in data


# ============================================================================
# TASK 2: PRINTER REMOVAL & PERSISTENCE
# ============================================================================

def test_printer_removal_via_config_api():
    """remove_printer removes key, cleans aliases, unmounts spools, and saves."""
    test_key = "CC_UNIT_TEST"
    config._data["printers"][test_key] = {
        "name": "Unit Test Machine",
        "model": "Elegoo Centauri Carbon",
        "host": "192.168.1.123",
        "enclosed": True,
        "has_ams": False
    }
    config.set_alias("UNIT_ALIAS", test_key)
    orig_default = config.default_printer
    config.default_printer = test_key
    FilamentVault.load_spool(test_key, slot=1, material="Test PLA", color="Blue", grams=800.0)

    assert test_key in config.printers
    assert "UNIT_ALIAS" in config.aliases
    assert FilamentVault.get_mounted_spool(test_key, slot=1) is not None

    # Remove printer
    success = config.remove_printer(test_key)
    assert success is True
    assert test_key not in config.printers
    assert "UNIT_ALIAS" not in config.aliases
    assert config.default_printer != test_key
    assert FilamentVault.get_mounted_spool(test_key, slot=1) is None

    # Verify spool was returned to shelf
    shelf = FilamentVault.get_shelf_inventory()
    assert any(s.get("material") == "Test PLA" for s in shelf)

    # Removing non-existent printer returns False
    assert config.remove_printer(test_key) is False


def test_printer_removal_persists_across_reloads(tmp_path):
    """Removing a printer must persist in config file and not be resurrected on load."""
    cfg = Config(config_dir=tmp_path)

    # Initial state with custom printer
    cfg._data["printers"]["TO_DELETE"] = {"name": "Delete Me", "host": "10.0.0.1"}
    cfg.save()

    # Load into fresh instance
    cfg2 = Config(config_dir=tmp_path)
    assert "TO_DELETE" in cfg2.printers

    # Remove printer and save
    cfg2.remove_printer("TO_DELETE")
    assert "TO_DELETE" not in cfg2.printers

    # Load again: TO_DELETE must NOT be resurrected
    cfg3 = Config(config_dir=tmp_path)
    assert "TO_DELETE" not in cfg3.printers


def test_cli_printer_removal_and_subsequent_refusal(runner):
    """Add a printer, remove it via CLI, confirm a3d printers omits it and status refuses it."""
    test_key = "CC_CLI_REMOVE"
    config._data["printers"][test_key] = {
        "name": "CLI Remove Test",
        "model": "Elegoo Centauri Carbon",
        "host": "192.168.1.199"
    }
    config.save()

    # 1. Verify it appears in printers
    res_list = runner.invoke(app, ["printers", "--json"])
    assert res_list.exit_code == 0
    assert test_key in json.loads(res_list.stdout)

    # 2. Remove via `a3d fleet --remove`
    res_rm = runner.invoke(app, ["fleet", "--remove", test_key, "--json"])
    assert res_rm.exit_code == 0
    rm_data = json.loads(res_rm.stdout)
    assert rm_data["success"] is True
    assert rm_data["removed"] == test_key

    # 3. Confirm a3d printers no longer lists it
    res_list_after = runner.invoke(app, ["printers", "--json"])
    assert res_list_after.exit_code == 0
    assert test_key not in json.loads(res_list_after.stdout)

    # 4. Confirm assigning or querying status is refused by name
    res_status = runner.invoke(app, ["status", "--printer", test_key, "--json"])
    assert res_status.exit_code == 1
    stat_data = json.loads(res_status.stdout)
    assert f"There is no printer called '{test_key}'." in stat_data["error"]
    assert test_key not in stat_data["printers"]


def test_cli_positional_printer_removal_syntaxes(runner):
    """Test alternative CLI removal syntaxes: printer remove, fleet remove, printers remove."""
    for p_name, cmd in [
        ("P_POS1", ["printer", "remove", "P_POS1", "--json"]),
        ("P_POS2", ["fleet", "remove", "P_POS2", "--json"]),
        ("P_POS3", ["printers", "remove", "P_POS3", "--json"])
    ]:
        config._data["printers"][p_name] = {"name": p_name, "host": "10.0.0.99"}
        config.save()
        assert p_name in config.printers

        res = runner.invoke(app, cmd)
        assert res.exit_code == 0, f"Command {cmd} failed: {res.stdout}"
        assert p_name not in config.printers
