"""SHIP_1.0 B8 — the fleet contradicted itself.

Six printers were listed and three were accepted when one had to be chosen,
because four places in the source carried the same hardcoded triple:

    ["CC1", "CC2", "C2_COMBO"]

    aethelark3d/streamer.py   the fleet streamer's default
    aethelark3d/cli.py  x2    status --printer AUTO, and listen --fleet
    aethelark3d/agent.py      the agent's fleet streamer

Those are the operator's own three machines, written into a module that ships to
people who own other ones. Someone with twenty Bambu P1S printers gets a module
that watches three names it invented for them.

What actually distinguishes a printer worth choosing is not its name. It is
whether there is anywhere to reach it.
"""
from __future__ import annotations

import pathlib

import pytest

from aethelark3d.config import Config

REPO = pathlib.Path(__file__).resolve().parent.parent
HARDCODED = ('["CC1", "CC2", "C2_COMBO"]', '["CC2", "CC1", "C2_COMBO"]')


def _config_with(printers) -> Config:
    cfg = Config.__new__(Config)
    cfg._data = {"printers": printers}
    return cfg


def test_addressable_keys_are_the_ones_with_somewhere_to_be_reached():
    cfg = _config_with({
        "CC1": {"host": "192.168.8.106"},
        "C2": {"host": ""},
        "CC2": {"host": "192.168.1.55"},
        "CC2_COMBO": {},
    })
    assert cfg.addressable_printer_keys() == ["CC1", "CC2"]


def test_a_fleet_where_nothing_has_an_address_still_offers_the_names():
    """Better to try a configured printer than to claim the fleet is empty."""
    cfg = _config_with({"C2": {"host": ""}, "CC2_COMBO": {}})
    assert cfg.addressable_printer_keys() == ["C2", "CC2_COMBO"]


def test_an_empty_fleet_is_empty():
    assert _config_with({}).addressable_printer_keys() == []


def test_a_printer_named_anything_at_all_is_included():
    """The point of B8: a fleet is not three names someone typed once."""
    cfg = _config_with({
        "BAMBU_P1S_07": {"host": "10.0.0.7"},
        "prusa-core-one": {"host": "10.0.0.9"},
    })
    assert cfg.addressable_printer_keys() == ["BAMBU_P1S_07", "prusa-core-one"]


@pytest.mark.parametrize("source", ["aethelark3d/streamer.py",
                                    "aethelark3d/cli.py",
                                    "aethelark3d/agent.py"])
def test_no_module_file_still_carries_the_operators_three_printers(source):
    text = (REPO / source).read_text()
    found = [h for h in HARDCODED if h in text]
    assert not found, (
        f"{source} still hardcodes {found} — a module that ships to other "
        f"people cannot carry this machine's printer names in its source")
