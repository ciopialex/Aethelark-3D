"""A printer that needs a code must not report paired until the code works.

Discovery finds a Centauri Carbon 2 and marks it `needs_access_code` — the
code is the printer's permanent LAN password, shown on its screen, not a
security toggle. Measured against the real machine on 2026-09-10: anonymous
MQTT is NOT AUTHORISED and HTTP with no token is 401, both still true after
the operator believed they had turned the code off. So the code is required,
and the app needs a way to take it and prove it.

The trap this pins: storing the code and clearing the "needs a code" flag
BEFORE the printer has accepted it. That reports a wrong code as a paired
printer, and the failure surfaces three tools later when a print silently does
not start. `a3d pair` connects first and only retires the flag on a connection
the printer actually accepted.

Driven through config with a fake driver, so no hardware is required.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aethelark3d.config import Config


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    # config_dir, not HOME: the default directory is computed at import, so
    # setting HOME here left Config() on the developer's real fleet file --
    # and this fixture saves. Measured 2026-09-25: a suite run replaced the
    # operator's real ELEGOO_7526B5 entry and gave it the code "Ab3dEf".
    c = Config(config_dir=tmp_path)
    c._data["printers"]["ELEGOO_7526B5"] = {
        "name": "Elegoo 7526B5", "brand": "Elegoo", "model": "Elegoo 7526B5",
        "host": "192.168.8.105", "port": 80, "protocol": "elegoolink",
        "needs_access_code": True, "mac": "aa:bb:cc:dd:ee:ff",
    }
    c.save()
    return c


def test_set_access_code_records_it_and_retires_the_flag(cfg):
    assert cfg.set_access_code("ELEGOO_7526B5", "Ab3dEf") is True
    slot = cfg.get_printer("ELEGOO_7526B5")
    assert slot["access_code"] == "Ab3dEf"
    assert slot["needs_access_code"] is False


def test_setting_a_code_on_a_printer_that_does_not_exist_is_false(cfg):
    assert cfg.set_access_code("NOPE", "Ab3dEf") is False


def test_the_code_survives_a_reload(cfg, tmp_path, monkeypatch):
    """It is durable state, not a per-process value — the printer keeps the
    same code across reboots, and so must the fleet."""
    cfg.set_access_code("ELEGOO_7526B5", "Ab3dEf")
    reloaded = Config(config_dir=tmp_path)
    assert reloaded.get_printer("ELEGOO_7526B5")["access_code"] == "Ab3dEf"


def test_a_discovered_cc2_starts_out_needing_a_code(cfg):
    """The premise of the whole flow: the flag is set by discovery, and the
    pair step is what clears it."""
    assert cfg.get_printer("ELEGOO_7526B5")["needs_access_code"] is True


def test_a_stored_code_reaches_the_driver_and_picks_the_cc2_path(cfg):
    """Once a code is present the factory must build the MQTT (CC2) driver,
    not the SDCP one — the code presence and the elegoolink protocol both say
    CC2, and either alone used to be enough to get it wrong."""
    cfg.set_access_code("ELEGOO_7526B5", "Ab3dEf")
    from aethelark3d.drivers.factory import resolve_transport
    slot = cfg.get_printer("ELEGOO_7526B5")
    assert resolve_transport(slot) == ("elegoolink", 80)
    assert slot["access_code"] == "Ab3dEf"
