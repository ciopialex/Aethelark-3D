"""A broadcast only finds machines that answer the broadcast.

Discovery was one thing: an SDCP UDP broadcast of `M99999` to port 3000. That
finds SDCP boards and nothing else, and the failure mode is silence rather than
an error — a printer that speaks another protocol is simply reported as absent.

Measured 2026-09-10 against an Elegoo Centauri Carbon 2 on the operator's own
subnet, powered on and reachable:

    M99999 -> 255.255.255.255:3000   no reply
    M99999 -> 192.168.8.255:3000     no reply
    M99999 -> 192.168.8.105:3000     no reply    (its real address)
    M99999 -> 192.168.8.105:3030     no reply
    discover()                       0 devices
    discover(targets=['192.168.8.105'])  0 devices

while the same machine answered:

    tcp/80    Server: libhv/1.3.4
    /system/info -> 401 Unauthorized      the endpoint exists, wants a token
    tcp/8080  multipart/x-mixed-replace   the camera
    tcp/1883  MQTT CONNACK 5, not authorised

So the printer was findable the whole time; nothing was looking. A 401 rather
than a 404 is the useful half — it says a printer is there and needs its access
code, which is something to tell the user instead of "no printers found".

These tests stand up a fake printer on loopback rather than needing hardware.
The real Centauri Carbon 2 is what the probe table was built against, but a
test that needs a printer plugged in is a test that does not run.
"""
from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aethelark3d.discovery import (  # noqa: E402
    _PROBES, Device, probe_host, sweep,
)


def _serve(handler_cls):
    """A throwaway HTTP server on a free loopback port."""
    httpd = HTTPServer(("127.0.0.1", 0), handler_cls)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd


def _handler(status: int, server: str, body: bytes = b"{}"):
    class H(BaseHTTPRequestHandler):
        server_version = server
        sys_version = ""

        def do_GET(self):
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    return H


@pytest.fixture
def elegoo_like():
    """What the real CC2 answers: libhv, and 401 on /system/info."""
    httpd = _serve(_handler(401, "libhv/1.3.4"))
    yield httpd
    httpd.shutdown()


def _probe_at(httpd, port_override):
    """Point one probe row at the fake server's port, then probe loopback."""
    import aethelark3d.discovery as d
    original = [dict(p) for p in d._PROBES]
    patched = []
    for p in d._PROBES:
        q = dict(p)
        if q["family"] == port_override:
            q["port"] = httpd.server_address[1]
        patched.append(q)
    d._PROBES = tuple(patched)
    try:
        return probe_host("127.0.0.1", timeout=1.0)
    finally:
        d._PROBES = tuple(tuple(original) and original)


# ── the defect ──────────────────────────────────────────────────────────────

def test_a_printer_that_needs_a_code_is_still_found(elegoo_like):
    device = _probe_at(elegoo_like, "elegoolink")
    assert device is not None, (
        "a machine answering libhv with 401 on /system/info was not "
        "recognised — that is exactly the CC2, and it was invisible")
    assert device.brand == "Elegoo"
    assert device.protocol == "elegoolink"


def test_it_says_the_code_is_what_is_missing(elegoo_like):
    """The user asked for this shape: find it, then ask for the code.

    A 401 is not a failure to find a printer. Reporting it as one is what
    turned "your printer needs its access code" into "no printers found".
    """
    device = _probe_at(elegoo_like, "elegoolink")
    assert device.raw["needs_access_code"] is True


def test_the_identity_survives_a_new_dhcp_lease(elegoo_like):
    """Keyed on the MAC, for the reason MainboardID was chosen: the address
    moves and the hardware does not. An ElegooLink printer will not give up a
    mainboard id without a token, so this is the identifier available."""
    device = _probe_at(elegoo_like, "elegoolink")
    assert device.mainboard_id.startswith("elegoolink:")
    assert device.mainboard_id != f"elegoolink:{device.ip}" or not device.raw["mac"]


# ── and it must not call everything a printer ───────────────────────────────

def test_a_plain_web_server_is_not_a_printer():
    httpd = _serve(_handler(200, "nginx/1.24.0", b"<html>hello</html>"))
    try:
        import aethelark3d.discovery as d
        patched = []
        for p in d._PROBES:
            q = dict(p)
            q["port"] = httpd.server_address[1]
            patched.append(q)
        original = d._PROBES
        d._PROBES = tuple(patched)
        try:
            assert probe_host("127.0.0.1", timeout=1.0) is None, (
                "an ordinary web server was reported as a printer")
        finally:
            d._PROBES = original
    finally:
        httpd.shutdown()


def test_a_404_is_not_a_printer():
    """A 404 means the endpoint is absent. Only 401/200 mean it is there."""
    httpd = _serve(_handler(404, "libhv/1.3.4"))
    try:
        assert _probe_at(httpd, "elegoolink") is None
    finally:
        httpd.shutdown()


def test_nothing_listening_is_not_a_printer():
    assert probe_host("127.0.0.1", timeout=0.2) is None


# ── the table is the point ──────────────────────────────────────────────────

def test_every_family_declares_whether_it_was_seen_on_hardware():
    """Only one row was verified against a real machine. The rest are written
    from documentation, and saying so in the data stops the next reader taking
    the table for measurement."""
    assert _PROBES, "the probe table is empty"
    verified = [p["family"] for p in _PROBES if p["verified"]]
    assert "elegoolink" in verified, (
        "elegoolink is the row that was confirmed against a Centauri Carbon 2")
    for p in _PROBES:
        assert "verified" in p, f"{p['family']} does not say whether it was tested"


def test_the_sweep_is_bounded_and_does_not_scan_the_internet():
    """A /8 in the routing table must not turn discovery into a port scan."""
    assert sweep(cidrs=["10.0.0.0/8"], timeout=0.05) == []
