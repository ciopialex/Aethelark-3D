"""Finding printers by asking the network, not by being told an IP.

A printer's address is DHCP's to change, and until now a printer that moved was
simply "offline" forever — the fleet was whatever someone had typed. Discovery
is the SDCP v3.0.0 broadcast: `M99999` to UDP 3000, and every board answers
with its identity.

The identity that matters is MainboardID. It is burned into the board, so it
survives the address changing; the IP and the user-editable name do not.

These drive the real socket code against a responder on loopback rather than
broadcasting, so nothing here reaches a printer that might be mid-print.
"""
from __future__ import annotations

import json
import socket
import threading

import pytest

from aethelark3d.discovery import (
    DISCOVERY_PAYLOAD, Device, adopt, discover, parse_reply, reconcile,
)


def _reply(board="000000000001d354", ip="192.168.1.2", name="CC2",
           machine="Elegoo Centauri Carbon 2"):
    return json.dumps({"Id": "uuid", "Data": {
        "Name": name, "MachineName": machine, "BrandName": "Elegoo",
        "MainboardIP": ip, "MainboardID": board,
        "ProtocolVersion": "V3.0.0", "FirmwareVersion": "V1.0.0"}}).encode()


class FakePrinter:
    """A UDP responder that answers M99999 the way a board does."""

    def __init__(self, *replies):
        self.replies = replies or (_reply(),)
        self.seen = []
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        self.sock.settimeout(0.2)
        while not self._stop.is_set():
            try:
                payload, sender = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                return
            self.seen.append(payload)
            for reply in self.replies:
                try:
                    self.sock.sendto(reply, sender)
                except OSError:
                    return

    def close(self):
        self._stop.set()
        self.sock.close()
        self.thread.join(timeout=1)


@pytest.fixture
def printer():
    p = FakePrinter()
    yield p
    p.close()


# ── the wire ────────────────────────────────────────────────────────────────

def test_the_broadcast_payload_is_the_one_the_protocol_specifies():
    assert DISCOVERY_PAYLOAD == b"M99999"


def test_a_real_board_reply_is_parsed(printer):
    found = discover(timeout=1.0, targets=["127.0.0.1"], port=printer.port)
    assert printer.seen and set(printer.seen) == {b"M99999"}, "the board was asked something else"
    assert len(found) == 1
    d = found[0]
    assert d.mainboard_id == "000000000001d354"
    assert d.ip == "192.168.1.2"
    assert d.name == "CC2"
    assert d.machine == "Elegoo Centauri Carbon 2"


def test_two_boards_both_answer():
    p = FakePrinter(_reply(board="aaa", ip="10.0.0.1", name="One"),
                    _reply(board="bbb", ip="10.0.0.2", name="Two"))
    try:
        found = discover(timeout=1.0, targets=["127.0.0.1"], port=p.port)
    finally:
        p.close()
    assert sorted(d.mainboard_id for d in found) == ["aaa", "bbb"]


def test_the_same_board_answering_twice_is_one_printer():
    p = FakePrinter(_reply(board="aaa"), _reply(board="aaa"))
    try:
        found = discover(timeout=1.0, targets=["127.0.0.1"], port=p.port)
    finally:
        p.close()
    assert len(found) == 1


def test_nothing_answering_is_an_empty_list_not_a_hang():
    found = discover(timeout=0.3, targets=["127.0.0.1"], port=9)
    assert found == []


@pytest.mark.parametrize("junk", [
    b"not json", b"[]", b"{}", b'{"Data": {}}',
    b'{"Data": {"MainboardID": ""}}',
    b'{"Data": {"MainboardIP": "1.2.3.4"}}',       # no board id
])
def test_a_reply_that_is_not_a_printer_is_ignored(junk):
    assert parse_reply(junk) is None


def test_the_sender_address_is_used_when_the_board_reports_none():
    """A board behind NAT reports an address right for its segment, not ours."""
    blob = json.dumps({"Data": {"MainboardID": "abc", "Name": "X"}}).encode()
    d = parse_reply(blob, sender_ip="10.1.2.3")
    assert d is not None and d.ip == "10.1.2.3"


# ── matching the fleet ──────────────────────────────────────────────────────

FLEET = {
    "CC1": {"name": "Centauri 1", "host": "172.20.10.4",
            "mainboard_id": "aaa"},
    "CC2": {"name": "CC2", "host": "192.168.1.55"},
    "C2":  {"name": "Spare", "host": ""},
}


def test_a_printer_that_moved_is_matched_by_its_board_not_its_address():
    """The whole point: the IP changed, the board did not."""
    moved, learned, unknown = reconcile(
        [Device(mainboard_id="aaa", ip="10.0.0.9", name="Centauri 1")], FLEET)
    assert moved == {"CC1": "10.0.0.9"}
    assert learned == {} and unknown == []


def test_a_printer_with_no_recorded_board_is_matched_by_name_once():
    moved, learned, unknown = reconcile(
        [Device(mainboard_id="bbb", ip="10.0.0.8", name="CC2")], FLEET)
    assert learned == {"CC2": "bbb"}, "its identity was not recorded"
    assert moved == {"CC2": "10.0.0.8"}
    assert unknown == []


def test_an_identified_slot_is_not_stolen_by_a_name_match():
    """CC1 already has a board id; a different board calling itself
    'Centauri 1' must not take its slot."""
    _, _, unknown = reconcile(
        [Device(mainboard_id="zzz", ip="10.0.0.7", name="Centauri 1")], FLEET)
    assert [d.mainboard_id for d in unknown] == ["zzz"]


def test_a_printer_nobody_has_a_slot_for_is_reported_not_dropped():
    _, _, unknown = reconcile(
        [Device(mainboard_id="new", ip="10.0.0.5", name="Kitchen")], FLEET)
    assert [d.name for d in unknown] == ["Kitchen"]


def test_a_printer_at_the_address_already_recorded_is_not_a_move():
    moved, _, _ = reconcile(
        [Device(mainboard_id="aaa", ip="172.20.10.4")], FLEET)
    assert moved == {}


# ── writing it back ─────────────────────────────────────────────────────────

class FakeConfig:
    def __init__(self, fleet):
        self.fleet = {k: dict(v) for k, v in fleet.items()}
        self.saves = 0
    def get_fleet(self):
        return self.fleet
    def get_printer(self, key):
        return self.fleet.get(key.upper().replace("-", "_"))
    def set_printer_ip(self, key, ip):
        self.fleet[key.upper().replace("-", "_")]["host"] = ip.strip()
    def save(self):
        self.saves += 1


def test_adopting_updates_the_address_and_records_the_identity():
    cfg = FakeConfig(FLEET)
    out = adopt([Device(mainboard_id="bbb", ip="10.0.0.8", name="CC2")], cfg=cfg)
    assert cfg.fleet["CC2"]["host"] == "10.0.0.8"
    assert cfg.fleet["CC2"]["mainboard_id"] == "bbb"
    assert out["moved"] == {"CC2": "10.0.0.8"} and cfg.saves == 1


def test_adopting_adds_a_printer_nobody_configured():
    cfg = FakeConfig(FLEET)
    out = adopt([Device(mainboard_id="new", ip="10.0.0.5", name="Kitchen Printer",
                        machine="Centauri Carbon")], cfg=cfg)
    assert "KITCHEN_PRINTER" in cfg.fleet
    assert cfg.fleet["KITCHEN_PRINTER"]["host"] == "10.0.0.5"
    assert cfg.fleet["KITCHEN_PRINTER"]["mainboard_id"] == "new"
    assert out["added"] == {"KITCHEN_PRINTER": "10.0.0.5"}


def test_a_printer_that_did_not_answer_is_left_alone():
    """Far likelier to be switched off than to have ceased to exist."""
    cfg = FakeConfig(FLEET)
    adopt([Device(mainboard_id="aaa", ip="172.20.10.4")], cfg=cfg)
    assert cfg.fleet["CC2"]["host"] == "192.168.1.55"
    assert "C2" in cfg.fleet


def test_discovering_nothing_changes_nothing():
    cfg = FakeConfig(FLEET)
    out = adopt([], cfg=cfg)
    assert cfg.saves == 0
    assert out["found"] == 0 and out["moved"] == {} and out["added"] == {}


def test_two_unnamed_printers_do_not_collide_on_one_slot():
    cfg = FakeConfig({})
    adopt([Device(mainboard_id="a", ip="10.0.0.1", name="Centauri"),
           Device(mainboard_id="b", ip="10.0.0.2", name="Centauri")], cfg=cfg)
    assert sorted(cfg.fleet) == ["CENTAURI", "CENTAURI_2"]


# ── the address healing itself, without anyone being told ───────────────────

def test_a_stale_address_is_rediscovered_and_the_job_continues(monkeypatch):
    """DHCP moved the printer. Nobody should have to find out or type anything.

    Drives the real connect_with_rediscovery, not a copy of it — an earlier
    version of this test re-implemented the logic in the test body, which
    proves nothing about the code that ships.
    """
    import asyncio
    from aethelark3d.api import connect_with_rediscovery

    attempts = []

    class Driver:
        def __init__(self, host):
            self.host = host
        async def connect(self):
            attempts.append(self.host)
            if self.host == "192.168.1.55":
                raise OSError("no route to host")

    monkeypatch.setattr("aethelark3d.discovery.find_and_adopt",
                        lambda *a, **k: {"moved": {"CC2": "10.0.0.8"}})
    out = asyncio.run(connect_with_rediscovery(
        Driver("192.168.1.55"), "CC2", lambda: Driver("10.0.0.8")))
    assert attempts == ["192.168.1.55", "10.0.0.8"]
    assert out.host == "10.0.0.8", "the caller kept the driver that cannot connect"


def test_a_working_printer_never_triggers_discovery(monkeypatch):
    """The ordinary path must not pay for the broken one."""
    import asyncio
    from aethelark3d.api import connect_with_rediscovery

    def boom(*a, **k):
        raise AssertionError("discovery ran on a healthy connection")

    monkeypatch.setattr("aethelark3d.discovery.find_and_adopt", boom)

    class Driver:
        async def connect(self):
            return None

    d = Driver()
    assert asyncio.run(connect_with_rediscovery(d, "CC1", boom)) is d


def test_a_printer_that_is_switched_off_fails_as_itself(monkeypatch):
    """Not as a confusing discovery error."""
    import asyncio
    from aethelark3d.api import connect_with_rediscovery

    monkeypatch.setattr("aethelark3d.discovery.find_and_adopt",
                        lambda *a, **k: {"moved": {}})

    class Driver:
        async def connect(self):
            raise OSError("no route to host")

    with pytest.raises(OSError, match="no route to host"):
        asyncio.run(connect_with_rediscovery(Driver(), "CC2", Driver))


def test_it_only_retries_once(monkeypatch):
    """A second failure after re-discovery is a real one."""
    import asyncio
    from aethelark3d.api import connect_with_rediscovery

    monkeypatch.setattr("aethelark3d.discovery.find_and_adopt",
                        lambda *a, **k: {"moved": {"CC2": "10.0.0.8"}})
    tries = []

    class Driver:
        async def connect(self):
            tries.append(1)
            raise OSError("still unreachable")

    with pytest.raises(OSError):
        asyncio.run(connect_with_rediscovery(Driver(), "CC2", Driver))
    assert len(tries) == 2


def test_discovery_finding_nothing_lets_the_original_failure_stand(monkeypatch):
    """A printer that is switched off must fail as a printer that is switched
    off, not as a confusing discovery error."""
    from aethelark3d.discovery import adopt

    class Cfg:
        def get_fleet(self):
            return {"CC2": {"host": "192.168.1.55"}}
        def get_printer(self, k):
            return self.get_fleet().get(k)
        def set_printer_ip(self, k, ip):
            raise AssertionError("nothing should have been written")
        def save(self):
            raise AssertionError("nothing should have been saved")

    out = adopt([], cfg=Cfg())
    assert out["moved"] == {} and out["found"] == 0
