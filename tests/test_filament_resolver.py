from pathlib import Path

import pytest

from aethelark3d.filaments.resolver import UnknownMaterial, apply_overrides, parse, plan

SLICER = Path.home() / ".config" / "ElegooSlicer" / "system" / "Elegoo" / "filament" / "ECC"
pytestmark = pytest.mark.skipif(not SLICER.is_dir(), reason="ElegooSlicer profiles not installed")


@pytest.mark.parametrize("said, material", [
    ("eSUN ASA+", "asa"), ("ASA", "asa"), ("Elegoo PLA-CF", "pla"), ("PETG", "petg"),
    ("Bambu Silk Red", "pla"), ("ABS", "abs"), ("TPU 95A", "tpu")])
def test_the_material_said_is_the_material_sliced(said, material):
    p = plan(said, "ECC")
    assert p.material == material
    assert parse(p.name).material == material
    assert parse(p.name).composite == parse(said).composite


def test_the_users_temperatures_win_and_reach_the_profile():
    p = plan("eSUN ASA+", "ECC", nozzle=270, bed=110)
    assert (p.nozzle, p.bed) == (270, 110)
    body = apply_overrides({"nozzle_temperature": ["260"]}, p)
    assert body["nozzle_temperature"] == ["270"]
    assert body["textured_plate_temp"] == ["110"]


def test_an_unknown_material_is_refused_not_guessed():
    with pytest.raises(UnknownMaterial):
        plan("UnknownAlienPolymerX", "ECC")
