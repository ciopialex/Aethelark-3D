"""A Centauri Carbon 2 connects out of the box, with nothing typed.

Elegoo's ElegooLink family ships with a fixed factory credential in force
whenever the on-screen Access Code toggle is off — the default state. It is the
same static value across the family, not derived from the serial (confirmed on
a real CC2 on 2026-09-11, read from the Elegoo slicer's own login). So the eagle
can connect to a freshly discovered CC2 with no code, no CLI, no pairing step.

The design is default-first, not default-only: the driver authenticates with
the user's code if they set one, otherwise the factory default. A printer with
a custom code (toggle on) still works — it just carries its own code here. So
this is robust whether or not the default is universal across every unit.

These are pure property checks — no printer, no network.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aethelark3d.drivers.elegoo import ElegooSDCPDriver, DEFAULT_CC2_ACCESS_CODE
from aethelark3d.drivers.base import PrinterCapability


def _driver(port, code=None, model="Elegoo 7526B5"):
    caps = PrinterCapability(brand="Elegoo", model=model, nozzle_diameter=0.4)
    return ElegooSDCPDriver(name="test", ip="192.0.2.1", port=port,
                            capabilities=caps, access_code=code)


# ── the zero-setup default ──────────────────────────────────────────────────

def test_a_discovered_cc2_with_no_code_uses_the_factory_default():
    d = _driver(port=80)  # elegoolink port, MAC-derived name, no code stored
    assert d._effective_code == DEFAULT_CC2_ACCESS_CODE, (
        "a freshly discovered CC2 has no factory default to connect with, so it "
        "cannot connect without the user typing a code — the opposite of zero setup")


def test_a_user_set_code_overrides_the_default():
    d = _driver(port=80, code="MyC0de")
    assert d._effective_code == "MyC0de", (
        "a printer with a custom access code must use it, not the factory default")


# ── routed to the MQTT path even before it is paired ────────────────────────

def test_an_elegoolink_printer_takes_the_cc2_path_by_port():
    d = _driver(port=80)  # no code, placeholder model
    assert d.is_cc2_family is True, (
        "a discovered CC2 on port 80 was not routed to the MQTT protocol — it "
        "would be driven over SDCP, which it does not speak")


def test_a_cc1_on_3030_is_not_treated_as_cc2():
    d = _driver(port=3030, model="Elegoo Centauri Carbon")
    assert d.is_cc2_family is False, (
        "a CC1 was misrouted to the CC2 MQTT path")


def test_a_named_cc2_model_is_still_cc2_on_any_port():
    """Belt-and-suspenders: an entry created with a real model name still routes
    correctly even if its port was recorded oddly."""
    d = _driver(port=3030, model="Elegoo Centauri Carbon 2")
    assert d.is_cc2_family is True


def test_the_default_is_a_nonempty_string():
    """An empty default is what the printer refuses — the whole bug this fixes."""
    assert DEFAULT_CC2_ACCESS_CODE and isinstance(DEFAULT_CC2_ACCESS_CODE, str)
