"""A printer the factory default unlocks is found READY, not "needs a code".

Discovery probes a printer's HTTP API with no token and sees 401 — the API
wants a credential. For the ElegooLink family that 401 does not mean a human
must type anything: the factory default is in force out of the box. So
discovery then tries the default (a plain HTTP GET with the default token), and
only a printer that refuses even that is marked as needing a person.

This is what makes the whole flow zero-setup: found -> ready -> the driver
connects with the default -> the user did nothing. Verified live against a real
CC2 on 2026-09-11: a fresh rediscover marked it needs_access_code=False.

`_default_credential_opens` is exercised against a throwaway HTTP server here,
and the branch that consumes it is checked by monkeypatching that function so no
network or printer is required.
"""
from __future__ import annotations

import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aethelark3d.discovery as disc


def _server(status_for_token: int, status_no_token: int = 401):
    """A fake printer: <status_no_token> without a token, <status_for_token>
    when the default token is present in the query string."""
    from aethelark3d.drivers.elegoo import DEFAULT_CC2_ACCESS_CODE

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            has_token = DEFAULT_CC2_ACCESS_CODE in (self.path or "")
            code = status_for_token if has_token else status_no_token
            self.send_response(code)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    httpd = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


# NOTE: _default_credential_opens is hardcoded to port 80, so these drive it
# through a monkeypatched port via the connection it builds. Simpler and just
# as honest: point it at a fake server by patching HTTPConnection's port.

def test_the_default_opening_the_api_means_no_code_needed(monkeypatch):
    httpd = _server(status_for_token=200)
    port = httpd.server_address[1]
    import http.client
    real = http.client.HTTPConnection
    monkeypatch.setattr(http.client, "HTTPConnection",
                        lambda ip, p=80, timeout=5: real("127.0.0.1", port, timeout=timeout))
    try:
        assert disc._default_credential_opens("192.0.2.9", "/system/info", 2.0) is True
    finally:
        httpd.shutdown()


def test_the_default_being_refused_means_a_code_is_needed(monkeypatch):
    httpd = _server(status_for_token=401)   # even with the token: refused
    port = httpd.server_address[1]
    import http.client
    real = http.client.HTTPConnection
    monkeypatch.setattr(http.client, "HTTPConnection",
                        lambda ip, p=80, timeout=5: real("127.0.0.1", port, timeout=timeout))
    try:
        assert disc._default_credential_opens("192.0.2.9", "/system/info", 2.0) is False
    finally:
        httpd.shutdown()


def test_nothing_listening_is_not_treated_as_open():
    """The safe fallback: an unreachable printer is 'needs a code', never 'ready'."""
    assert disc._default_credential_opens("127.0.0.1", "/system/info", 0.2) is False
