"""Every ElegooLink session is closed, including the ones that fail.

The CC2's MQTT broker accepts a limited number of clients, and the listener
opens one session per poll, every 2 seconds. The driver used to close only on
success, so each timeout stranded a connection. Measured 2026-09-24: an
orphaned `a3d listen --fleet` held 340 ESTABLISHED connections to the printer
after 56 minutes. Against the real printer with `status` forced to time out,
the old driver kept one more socket open per failed poll (1, 2, 3, 4, 5) and
this one kept none.

The seam is driver <-> pycentauri, so pycentauri is what is faked: a client
whose request fails, and which counts whether it was closed. No printer, no
network.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pycentauri

from aethelark3d.drivers.base import PrinterCapability
from aethelark3d.drivers.elegoo import ElegooSDCPDriver


class _FailingClient:
    opened = 0
    closed = 0

    @classmethod
    async def connect(cls, *a, **k):
        cls.opened += 1
        return cls()

    async def close(self):
        type(self).closed += 1

    async def _fail(self, *a, **k):
        raise asyncio.TimeoutError("the printer did not answer")

    status = pause = resume = stop = set_light = set_print_speed = _fail
    _cc2_request = upload_file = _fail


@pytest.fixture
def cc2(monkeypatch):
    _FailingClient.opened = _FailingClient.closed = 0
    monkeypatch.setattr(pycentauri, "CC2Printer", _FailingClient)
    caps = PrinterCapability(brand="Elegoo", model="Elegoo 7526B5", nozzle_diameter=0.4)
    return ElegooSDCPDriver(name="test", ip="192.0.2.1", port=80, capabilities=caps)


CALLS = {
    "get_telemetry": lambda d: d.get_telemetry(),
    "send_gcode": lambda d: d.send_gcode("M115"),
    "pause_print": lambda d: d.pause_print(),
    "resume_print": lambda d: d.resume_print(),
    "stop_print": lambda d: d.stop_print(),
    "set_chamber_light": lambda d: d.set_chamber_light(True),
    "set_speed_mode": lambda d: d.set_speed_mode("silent"),
    "start_print": lambda d: d.start_print("/local/x.gcode"),
}


@pytest.mark.parametrize("name", sorted(CALLS))
def test_a_failed_call_still_closes_the_session(cc2, name):
    asyncio.run(CALLS[name](cc2))
    assert _FailingClient.opened == 1
    assert _FailingClient.closed == 1, (
        f"{name} failed and left its connection to the printer open; the "
        f"listener polls every 2 s, so this fills the printer's broker")


def test_a_failed_upload_still_closes_the_session(cc2, tmp_path):
    job = tmp_path / "x.gcode"
    job.write_text("G28\n")
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(cc2.upload_job(job))
    assert _FailingClient.closed == _FailingClient.opened == 1
