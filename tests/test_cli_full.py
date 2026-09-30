"""
Comprehensive CLI Test Suite for Aethelark-3D.
Uses Typer CliRunner to exercise every command, option, flag, and error branch in aethelark3d/cli.py.
"""

from typer.testing import CliRunner
from pathlib import Path
from aethelark3d.cli import app
from aethelark3d.spools import FilamentVault
from tests.test_real_artifacts import create_synthetic_binary_stl

runner = CliRunner()


def test_cli_help():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "⚡ Aethelark-3D" in result.stdout
    assert "dispatch" in result.stdout


def test_cli_printers():
    result = runner.invoke(app, ["printers"])
    assert result.exit_code == 0
    assert "CC1" in result.stdout
    assert "Centauri" in result.stdout


def test_cli_fleet_overview():
    result = runner.invoke(app, ["fleet"])
    assert result.exit_code == 0
    assert "Aethelark-3D Fleet" in result.stdout


def test_cli_fleet_assign_ip():
    result = runner.invoke(app, ["fleet", "--set-ip", "CC2", "--ip", "192.168.1.55"])
    assert result.exit_code == 0
    assert "192.168.1.55" in result.stdout


def test_cli_filament_resolver():
    # 1. Silk PLA
    res_silk = runner.invoke(app, ["filament", "Bambu Silk Red"])
    assert res_silk.exit_code == 0
    assert "Outer Wall Speed: 60 mm/s" in res_silk.stdout

    # 2. High-speed PLA+
    res_hs = runner.invoke(app, ["filament", "Elegoo Rapid PLA+ Black"])
    assert res_hs.exit_code == 0
    assert "Max Volumetric Flow: 22.0 mm³/s" in res_hs.stdout

    # 3. TPU
    res_tpu = runner.invoke(app, ["filament", "Overture TPU 95A"])
    assert res_tpu.exit_code == 0
    assert "Max Volumetric Flow: 4.0 mm³/s" in res_tpu.stdout


def test_cli_spool_management():
    # 1. Load spool into CC1
    res_load = runner.invoke(app, ["spool", "-p", "CC1", "-l", "Bambu Silk PLA", "-c", "Sapphire Blue", "-g", "950"])
    assert res_load.exit_code == 0
    assert "Bambu Silk PLA" in res_load.stdout
    assert "CC1 Slot 1" in res_load.stdout

    # 2. Inspect active slots
    res_show = runner.invoke(app, ["spool", "-p", "CC1"])
    assert res_show.exit_code == 0
    assert "Sapphire Blue" in res_show.stdout

    # 3. Unload spool to shelf
    res_unload = runner.invoke(app, ["spool", "-p", "CC1", "-s", "1", "--unload"])
    assert res_unload.exit_code == 0
    assert "Unloaded" in res_unload.stdout

    # 4. View shelf inventory
    res_shelf = runner.invoke(app, ["spool", "--shelf"])
    assert res_shelf.exit_code == 0
    assert "Shelf Inventory" in res_shelf.stdout or "empty" in res_shelf.stdout.lower()


def test_cli_dispatch():
    # 1. Standard PLA dispatch
    res_pla = runner.invoke(app, ["dispatch", "calibration_cube.stl", "-m", "PLA Basic", "-c", "White", "-g", "20.0"])
    assert res_pla.exit_code == 0
    assert "Optimal Fleet Assignment" in res_pla.stdout
    assert "Ask Price" in res_pla.stdout

    # 2. Abrasive PLA-CF dispatch (Must route to CC1/CC2)
    res_cf = runner.invoke(app, ["dispatch", "drone_arm.stl", "-m", "PLA-CF", "-c", "Black", "-g", "45.0"])
    assert res_cf.exit_code == 0
    assert "CC1" in res_cf.stdout or "CC2" in res_cf.stdout


def test_cli_status_and_light(tmp_path):
    # Status
    res_stat = runner.invoke(app, ["status", "-p", "CC1"])
    assert res_stat.exit_code == 0

    # The shipped fleet leaves CC1's address blank on purpose, and `light`
    # refuses a printer it cannot reach (typer.BadParameter, exit 2). Give it
    # an address first -- nothing listens there, so the light cannot change.
    res_ip = runner.invoke(app, ["fleet", "--set-ip", "CC1", "--ip", "127.0.0.1"])
    assert res_ip.exit_code == 0

    # A light that did not change is a failure, and says so. It used to exit 0
    # and print "light_on": true, and the eagle told the user the light was on.
    for state in ("on", "off"):
        res_light = runner.invoke(app, ["light", state, "-p", "CC1", "--json"])
        assert res_light.exit_code == 1
        assert '"success": false' in res_light.stdout
        assert "light_on" not in res_light.stdout


def test_cli_headless_print_simulation(tmp_path):
    stl_file = tmp_path / "part.stl"
    create_synthetic_binary_stl(stl_file, size_mm=15.0)

    res_print = runner.invoke(app, ["print", str(stl_file), "-f", "Elegoo Rapid PLA+", "-p", "CC1", "--no-start"])
    assert res_print.exit_code == 0
    assert "Pipeline Complete" in res_print.stdout or "Dispatched" in res_print.stdout
