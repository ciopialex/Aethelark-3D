"""Slice for the printer in the room, not the printer the file was made for.

A downloaded .3mf is very often a PROJECT exported from another slicer for
another machine. YES_NO_DICE.3mf was a Bambu Lab P2S project — Bambu machine
start-gcode, Bambu bed type, the works. ElegooSlicer honours those embedded
settings, so the Centauri Carbon 2 was sent gcode for a Bambu P2S / (after a
half-fix) a Centauri Carbon 1, accepted the job name, and hung with no heat.

Three things had to be true for a correct slice, and each is pinned here:

  1. the file is reduced to geometry, so no foreign project settings survive;
  2. the FILAMENT profile is loaded with --load-filaments, not --load-settings
     (the slicer silently ignores a filament in the settings list, and the
     nozzle falls to a 200C default instead of the profile's 220C);
  3. a build plate is named, because the unset default is "Cool Plate" at 35C,
     too cold for PLA to stick.

The slice command is captured by stubbing subprocess.run, so these run without
ElegooSlicer or a printer. The one live-ish check converts the real dice file
with trimesh, which is a pure geometry operation.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aethelark3d.slicers.elegoo import ElegooSlicerBackend, SliceConfig


class _FakeCompleted:
    returncode = 0
    stdout = ""
    stderr = ""


def _capture_cmd(tmp_path):
    """Run slice() far enough to build the command, capturing it."""
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        # leave a plate_1.gcode so slice() does not raise looking for output
        out = Path([cmd[i + 1] for i, a in enumerate(cmd) if a == "--outputdir"][0])
        (out / "plate_1.gcode").write_text("; test\nM109 S220\n", encoding="latin1")
        return _FakeCompleted()

    backend = ElegooSlicerBackend()
    model = tmp_path / "thing.stl"
    model.write_text("solid x\nendsolid x\n")  # a bare mesh, so no conversion
    cfg = SliceConfig(printer_key="CC2", filament_query="Elegoo PLA Basic",
                      enable_purgex=False, output_dir=tmp_path)
    with patch("aethelark3d.slicers.elegoo.subprocess.run", fake_run):
        try:
            backend.slice(model, cfg)
        except Exception:
            pass
    return captured.get("cmd", [])


def test_the_filament_is_loaded_with_its_own_flag(tmp_path):
    cmd = _capture_cmd(tmp_path)
    assert "--load-filaments" in cmd, (
        "the filament is not passed via --load-filaments, so the slicer ignores "
        "it and the nozzle falls to a wrong default temperature")
    # and the filament json must NOT be smuggled into --load-settings
    if "--load-settings" in cmd:
        import os
        settings = ";".join(os.path.basename(p) for p in cmd[cmd.index("--load-settings") + 1].split(";"))
        assert "filament" not in settings.lower(), (
            "a filament json is in --load-settings, where the slicer ignores it")


def test_a_build_plate_is_named(tmp_path):
    cmd = _capture_cmd(tmp_path)
    assert "--curr-bed-type" in cmd, (
        "no build plate is named, so the slicer defaults to Cool Plate at 35C — "
        "too cold for PLA")
    bed = cmd[cmd.index("--curr-bed-type") + 1]
    assert "cool" not in bed.lower(), f"defaulted to a cold plate: {bed}"


def test_the_target_machine_profile_is_loaded_for_every_file_type(tmp_path):
    """The machine identity must be loaded even for a .3mf, which it was not —
    it was gated behind is_raw_stl."""
    cmd = _capture_cmd(tmp_path)
    settings = cmd[cmd.index("--load-settings") + 1] if "--load-settings" in cmd else ""
    assert "Centauri Carbon 2" in settings, (
        f"the CC2 machine profile was not loaded: {settings}")


def test_a_3mf_project_is_reduced_to_geometry():
    """The real dice file: a Bambu project reduced to a bare STL, with the
    foreign settings gone. Skips cleanly if the sample file is not present."""
    dice = Path.home() / "Downloads" / "YES_NO_DICE.3mf"
    if not dice.is_file():
        pytest.skip("sample dice not present on this machine")
    backend = ElegooSlicerBackend()
    import tempfile
    out = Path(tempfile.mkdtemp())
    result = backend._geometry_only(dice, out)
    assert result.suffix.lower() == ".stl", "a .3mf project was not reduced to geometry"
    assert result.stat().st_size > 0
    text = result.read_bytes()[:512]
    assert b"Bambu" not in text and b"P2S" not in text


def test_the_filament_inheritance_is_flattened_to_real_temps(tmp_path):
    """The slicer keeps only a filament leaf's own keys via --load-filaments, so
    a leaf that sets only the nozzle loses the bed temperature. Flattening the
    inheritance chain restores the manufacturer's real values.

    Measured: Elegoo PLA Basic sliced at bed 45 / nozzle 200 (fallbacks) before
    flattening, and bed 60 / nozzle 220 (the profile chain) after.
    """
    import json
    backend = ElegooSlicerBackend()
    # a tiny synthetic chain: leaf (nozzle only) -> base (the bed temp)
    fam = backend.datadir / "system" / "Elegoo" / "filament" / "TESTFAM"
    fam.mkdir(parents=True, exist_ok=True)
    base = fam / "Test Base.json"
    base.write_text(json.dumps({
        "name": "Test Base", "textured_plate_temp": ["60"],
        "hot_plate_temp": ["60"]}))
    leaf = fam / "Test PLA.json"
    leaf.write_text(json.dumps({
        "name": "Test PLA", "inherits": "Test Base",
        "nozzle_temperature": ["220"]}))
    backend._fil_idx = None  # rebuild the index to see the new files
    flat_path = backend._flattened_filament(leaf, tmp_path)
    flat = json.loads(flat_path.read_text())
    assert flat["nozzle_temperature"] == ["220"], "lost the leaf's own value"
    assert flat["textured_plate_temp"] == ["60"], (
        "the bed temperature was not inherited from the base — the flatten "
        "did not walk the chain")
    assert "inherits" not in flat, "a flattened profile must not still inherit"
    base.unlink(); leaf.unlink()
    try:
        fam.rmdir()
    except OSError:
        pass
