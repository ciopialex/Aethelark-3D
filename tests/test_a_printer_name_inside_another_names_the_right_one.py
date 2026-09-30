import pytest

from aethelark3d.aliases import PrinterAliasManager
from aethelark3d.config import config


@pytest.fixture
def carbons(monkeypatch):
    fleet = {"ELEGOO_7526B5": {"name": "Centauri Carbon 2", "model": "Centauri Carbon 2", "host": "10.0.0.5"},
             "CENTAURI_CARBON": {"name": "Centauri Carbon", "model": "Centauri Carbon", "host": "10.0.0.6"}}
    monkeypatch.setitem(config._data, "printers", fleet)
    monkeypatch.setitem(config._data, "aliases", {})


@pytest.mark.parametrize("said, key", [
    ("Centauri Carbon", "CENTAURI_CARBON"),
    ("the Centauri Carbon printer", "CENTAURI_CARBON"),
    ("Centauri Carbon 2", "ELEGOO_7526B5"),
    ("carbon 2", "ELEGOO_7526B5"),
])
def test_the_printer_named_is_the_printer_reached(carbons, said, key):
    assert PrinterAliasManager.resolve_to_key(said) == key


def test_a_name_that_fits_both_reaches_neither(carbons):
    assert PrinterAliasManager.resolve_to_key("Carbon") not in config.printers


def test_a_print_addressed_by_name_reaches_the_printer(carbons):
    """2026-09-29: 'Centauri Carbon' passed the confirmation and the print then
    failed with "There is no printer called 'Centauri Carbon'"."""
    from aethelark3d.api import find_and_prepare_print
    assert config.get_printer("Centauri Carbon")["host"] == "10.0.0.6"
    res = find_and_prepare_print(query="anything", printer_key="Centauri Carbon",
                                 filament="Unobtainium", auto_start=False)
    assert res["printer"] == "CENTAURI_CARBON"
    assert "no printer called" not in str(res.get("error", ""))
