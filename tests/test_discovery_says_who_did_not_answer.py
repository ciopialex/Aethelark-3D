"""Discovery reported what answered and said nothing about what did not.

Observed 2026-09-04. Three printers were powered on and on the wifi. One spoke
SDCP; the other two run MQTT and are deaf to the M99999 broadcast. `a3d
discover --json` returned:

    {"found": 1, "moved": {"CC1": "192.168.8.106"}, "identified": {},
     "added": {}, "printers": [ ...one printer... ]}

A person with three printers was told about one, with no hint the others had
been looked for, let alone missed. The fleet already held slots for both --
the information needed to say "CC2 and C2_COMBO did not answer" was sitting in
the config the whole time and simply was not reported.

`found: 1` is true and useless. Saying which configured printers stayed silent
costs nothing, needs no new probe, and is the difference between "discovery
works" and "two of your printers are missing".
"""
from __future__ import annotations

from aethelark3d.discovery import Device, adopt


class _Config:
    """A fleet in memory, matching the surface `adopt` uses."""

    def __init__(self, fleet):
        self._fleet = fleet

    def get_fleet(self):
        return self._fleet

    def get_printer(self, key):
        return self._fleet.get(key)

    def set_printer_ip(self, key, ip):
        self._fleet.setdefault(key, {})["host"] = ip

    def save(self):
        pass


def _fleet_of_three():
    return {
        "CC1": {"name": "Centauri Carbon", "host": "192.168.8.106",
                "mainboard_id": "5c5c12d80103147000001c0000000000"},
        "CC2": {"name": "Centauri Carbon 2", "host": "192.168.1.55"},
        "C2_COMBO": {"name": "Centauri 2 Combo", "host": "172.20.10.3"},
    }


CC1_ANSWERED = Device(
    mainboard_id="5c5c12d80103147000001c0000000000",
    ip="192.168.8.106", name="Centauri Carbon", machine="Centauri Carbon",
    brand="ELEGOO")


def test_the_printers_that_did_not_answer_are_named():
    cfg = _Config(_fleet_of_three())
    result = adopt([CC1_ANSWERED], cfg=cfg)

    assert "silent" in result, (
        "the report says what answered and nothing about what did not; a person "
        "with three printers is told about one and cannot tell the difference "
        "between 'you have one printer' and 'two of yours are missing'")

    silent = result["silent"]
    assert set(silent) == {"CC2", "C2_COMBO"}, (
        f"expected the two that stayed quiet, got {silent}")


def test_a_silent_printer_reports_where_it_was_last_seen():
    """The last known address is the first thing a person needs to check."""
    cfg = _Config(_fleet_of_three())
    silent = adopt([CC1_ANSWERED], cfg=cfg)["silent"]
    assert silent["CC2"] == "192.168.1.55"
    assert silent["C2_COMBO"] == "172.20.10.3"


def test_a_printer_that_answered_is_not_reported_silent():
    cfg = _Config(_fleet_of_three())
    result = adopt([CC1_ANSWERED], cfg=cfg)
    assert "CC1" not in result["silent"]


def test_a_fleet_where_everyone_answers_reports_nobody_silent():
    cfg = _Config({"CC1": {"name": "Centauri Carbon",
                           "mainboard_id": CC1_ANSWERED.mainboard_id,
                           "host": "192.168.8.106"}})
    assert adopt([CC1_ANSWERED], cfg=cfg)["silent"] == {}


def test_a_slot_with_no_address_was_never_asked():
    """An empty slot did not stay silent -- nothing was ever sent to it."""
    fleet = _fleet_of_three()
    fleet["C2"] = {"name": "reserved slot"}          # no host
    silent = adopt([CC1_ANSWERED], cfg=_Config(fleet))["silent"]
    assert "C2" not in silent, (
        "a slot with no address was reported as not answering, but no probe "
        "could have reached it")


def test_silence_never_deletes_or_rewrites_a_slot():
    """Reporting a printer as quiet must not touch its configuration."""
    fleet = _fleet_of_three()
    cfg = _Config(fleet)
    adopt([CC1_ANSWERED], cfg=cfg)
    assert fleet["CC2"]["host"] == "192.168.1.55", "a silent printer was rewritten"
    assert set(fleet) == {"CC1", "CC2", "C2_COMBO"}, "a silent printer was dropped"
