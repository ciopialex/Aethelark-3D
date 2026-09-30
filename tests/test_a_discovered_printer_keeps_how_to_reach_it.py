"""Finding a printer is useless if the fleet forgets how to drive it.

Discovery learns three things about a probed machine that a bare IP does not
carry: which protocol speaks to it, which port, and whether it answered with a
401 — that is, whether it is found AND locked.

Before this, `adopt()` wrote every discovered printer with `port: 3030` and no
protocol at all. 3030 is the Centauri Carbon 1's SDCP port. A Centauri Carbon 2
is ElegooLink on 80, driven over MQTT — measured against the real machine on
2026-09-10 — so it was recorded as if it spoke a protocol it does not, and the
driver factory, guessing from a model name the printer never gave up, fell
through to the SDCP default.

The `needs_access_code` flag matters most. The operator asked for exactly one
behaviour: try to connect, and mention the access code ONLY when you hit one.
That is impossible if the 401 the probe already saw does not survive into the
fleet — the eagle would have to blindly fail a connection to rediscover a fact
discovery already knew.

These use a fake fleet config in tmp_path; nothing here touches the operator's
real printers.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aethelark3d.discovery import Device, adopt, transport_for
from aethelark3d.drivers.factory import resolve_transport


class _FakeConfig:
    """Just enough of config for adopt() to write and read a fleet."""

    def __init__(self):
        self._fleet: dict = {}
        self.saved = False

    def get_fleet(self):
        return self._fleet

    def get_printer(self, key):
        return self._fleet.get(key)

    def set_printer_ip(self, key, ip):
        self._fleet[key]["host"] = ip

    def save(self):
        self.saved = True


def _cc2(ip="192.168.8.105", mac="aa:bb:cc:dd:ee:ff") -> Device:
    """What the probe builds for a real Centauri Carbon 2: 401, ElegooLink."""
    return Device(
        mainboard_id=f"elegoolink:{mac}", ip=ip, name="Elegoo 7526B5",
        brand="Elegoo", protocol="elegoolink",
        raw={"needs_access_code": True, "mac": mac, "status": 401},
    )


def _cc1(ip="192.168.8.106") -> Device:
    """A broadcast SDCP board — the old path, no probe fields."""
    return Device(mainboard_id="000000000001d354", ip=ip, name="CC1",
                  machine="Elegoo Centauri Carbon", brand="Elegoo", protocol="")


# ── the protocol and port survive ───────────────────────────────────────────

def test_a_discovered_cc2_is_recorded_as_elegoolink_not_sdcp():
    cfg = _FakeConfig()
    adopt([_cc2()], cfg=cfg)
    slot = next(iter(cfg.get_fleet().values()))
    assert slot["protocol"] == "elegoolink", slot
    assert slot["port"] == 80, (
        f"a CC2 was stored on port {slot['port']}; 3030 is the CC1's SDCP "
        f"port and the CC2 does not speak it")


def test_the_factory_then_drives_it_on_the_right_protocol():
    cfg = _FakeConfig()
    adopt([_cc2()], cfg=cfg)
    slot = next(iter(cfg.get_fleet().values()))
    proto, port = resolve_transport(slot)
    assert (proto, port) == ("elegoolink", 80)


def test_transport_for_maps_each_family():
    assert transport_for(_cc2())[0] == "elegoolink"
    assert transport_for(_cc1())[0] == "sdcp"


# ── found AND locked both reach the fleet ───────────────────────────────────

def test_a_401_printer_is_marked_as_needing_a_code():
    cfg = _FakeConfig()
    adopt([_cc2()], cfg=cfg)
    slot = next(iter(cfg.get_fleet().values()))
    assert slot["needs_access_code"] is True, (
        "the 401 the probe saw did not survive into the fleet, so the eagle "
        "cannot know to ASK for the code")


def test_the_mac_is_kept_so_the_printer_survives_a_dhcp_move():
    cfg = _FakeConfig()
    adopt([_cc2()], cfg=cfg)
    slot = next(iter(cfg.get_fleet().values()))
    assert slot["mac"] == "aa:bb:cc:dd:ee:ff"


# ── the old broadcast path is unharmed ──────────────────────────────────────

def test_a_broadcast_cc1_is_still_sdcp_on_3030():
    cfg = _FakeConfig()
    adopt([_cc1()], cfg=cfg)
    slot = next(iter(cfg.get_fleet().values()))
    assert slot["protocol"] == "sdcp"
    assert slot["port"] == 3030
    assert slot["needs_access_code"] is False


def test_an_explicit_protocol_beats_a_misleading_model_name():
    """The CC2's stored model is its MAC-derived name, in no transport table.
    Only the explicit protocol saves it."""
    assert resolve_transport(
        {"protocol": "elegoolink", "model": "Elegoo 7526B5"}) == ("elegoolink", 80)
    # and a config with no protocol still works off the model name
    assert resolve_transport(
        {"model": "Elegoo Centauri Carbon"}) == ("sdcp", 3030)
