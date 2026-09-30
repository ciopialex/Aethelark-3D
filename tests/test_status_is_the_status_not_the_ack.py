"""The SDCP status request returns two messages, and the driver read the wrong one.

Captured from a real Centauri Carbon (mainboard 5c5c12d8...) on 2026-09-04, idle
and at room temperature. Sending `Cmd: 0` produces, in order:

    sdcp/response/<id>   {"Data": {"Cmd": 0, "Data": {"Ack": 0}}}
    sdcp/status/<id>     {"Status": {"TempOfNozzle": 29.58, ...}}

`_send_sdcp` matched on `Data.Cmd == cmd`, so it returned the acknowledgement and
never saw the status. Every field then fell to its default: 0.0 degrees, 0 layers,
0% progress, and -- because the absent `Status` key defaulted to 0, which maps to
IDLE -- a confident "idle" regardless of what the machine was doing.

That is worse than an error. An unreachable printer reports OFFLINE, which is
honest; this reported a plausible idle printer with a nozzle at absolute zero.

The field names were wrong too, independently of the nesting: the wire says
TempTargetNozzle, not TargetTempOfNozzle; PrintInfo.TotalLayer singular, not
TotalLayers; PrintInfo.Progress, not PrintProgress.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from aethelark3d.drivers.base import PrinterState
from aethelark3d.drivers.elegoo import ElegooSDCPDriver

MAINBOARD = "5c5c12d80103147000001c0000000000"

#: Exactly what the printer sent, in the order it sent it.
ACK_FIRST = {
    "Id": "probe",
    "Topic": f"sdcp/response/{MAINBOARD}",
    "Data": {"Cmd": 0, "Data": {"Ack": 0}, "RequestID": "probe", "MainboardID": MAINBOARD},
}
STATUS_SECOND = {
    "Topic": f"sdcp/status/{MAINBOARD}",
    "MainboardID": MAINBOARD,
    "TimeStamp": 1788537000,
    "Status": {
        "CurrentStatus": [0],
        "TimeLapseStatus": 0,
        "PlatFormType": 1,
        "AmsConnectStatus": 0,
        "TempOfHotbed": 28.20092988411857,
        "TempOfNozzle": 29.58878404894325,
        "TempOfBox": 27.94892514848891,
        "TempTargetHotbed": 0,
        "TempTargetNozzle": 0,
        "TempTargetBox": 0,
        "CurrenCoord": "0.00,0.00,0.00",
        "CurrentFanSpeed": {"ModelFan": 0, "AuxiliaryFan": 0, "BoxFan": 0},
        "ZOffset": 0.0,
        "LightStatus": {"SecondLight": 0, "RgbLight": [0, 0, 0]},
        "PrintInfo": {
            "Status": 0, "CurrentLayer": 0, "TotalLayer": 0, "CurrentTicks": 0,
            "TotalTicks": 0, "Filename": "", "TaskId": "", "PrintSpeedPct": 100,
            "Progress": 0,
        },
    },
}

#: The same shape, mid-print. Same field names, non-zero values -- this is the
#: case the old parse could never have reported, because it reported IDLE always.
STATUS_PRINTING = {
    "Topic": f"sdcp/status/{MAINBOARD}",
    "MainboardID": MAINBOARD,
    "Status": {
        "CurrentStatus": [1],
        "TempOfHotbed": 60.0,
        "TempOfNozzle": 213.4,
        "TempOfBox": 31.2,
        "TempTargetHotbed": 60,
        "TempTargetNozzle": 215,
        "PrintInfo": {
            "Status": 13, "CurrentLayer": 47, "TotalLayer": 300,
            "CurrentTicks": 600000, "TotalTicks": 3600000,
            "Filename": "benchy.gcode", "Progress": 15,
        },
    },
}


class _ScriptedSocket:
    """A websocket that replays captured frames and records what was sent."""

    def __init__(self, frames):
        self._frames = list(frames)
        self.sent = []

    async def send(self, payload):
        self.sent.append(json.loads(payload))

    async def recv(self):
        if not self._frames:
            raise asyncio.TimeoutError("no more frames")
        return json.dumps(self._frames.pop(0))

    async def close(self):
        pass


def _driver_reading(frames) -> ElegooSDCPDriver:
    driver = ElegooSDCPDriver(name="CC1", ip="192.168.8.106")
    assert not driver.is_cc2_family, "this test covers the plain SDCP path"
    driver._ws = _ScriptedSocket(frames)
    return driver


def test_the_temperatures_are_the_ones_the_printer_reported():
    """s10. A printer at room temperature reports room temperature, not zero."""
    driver = _driver_reading([ACK_FIRST, STATUS_SECOND])
    telemetry = asyncio.run(driver.get_telemetry())

    assert telemetry.nozzle_temp == pytest.approx(29.5887, abs=0.001), (
        f"the printer said 29.59 C and the driver said {telemetry.nozzle_temp}. "
        f"Zero is not a missing value here -- it is a claim that the nozzle is "
        f"at absolute zero.")
    assert telemetry.bed_temp == pytest.approx(28.2009, abs=0.001)
    assert telemetry.chamber_temp == pytest.approx(27.9489, abs=0.001)


def test_the_ack_arriving_first_does_not_win():
    """The acknowledgement is not the answer; it only says the request landed."""
    driver = _driver_reading([ACK_FIRST, STATUS_SECOND])
    telemetry = asyncio.run(driver.get_telemetry())
    assert telemetry.raw_telemetry.get("TempOfNozzle") is not None, (
        "raw_telemetry carries the ack payload {'Ack': 0} instead of the status")


def test_a_running_print_is_not_reported_as_idle():
    """The failure that hid this: every state collapsed to IDLE."""
    driver = _driver_reading([ACK_FIRST, STATUS_PRINTING])
    telemetry = asyncio.run(driver.get_telemetry())

    assert telemetry.state == PrinterState.PRINTING, (
        f"a printer 47 layers into a job reported {telemetry.state}")
    assert telemetry.current_layer == 47
    assert telemetry.total_layers == 300, "the wire field is TotalLayer, singular"
    assert telemetry.progress_percent == pytest.approx(15.0)
    assert telemetry.current_file == "benchy.gcode"
    assert telemetry.nozzle_target == pytest.approx(215.0), (
        "the wire field is TempTargetNozzle, not TargetTempOfNozzle")
    assert telemetry.bed_target == pytest.approx(60.0)


def test_an_unreachable_printer_still_reports_disconnected():
    """The fix must not turn a real failure into a fabricated reading."""
    driver = _driver_reading([])          # socket yields nothing
    telemetry = asyncio.run(driver.get_telemetry())
    assert telemetry.state == PrinterState.DISCONNECTED
    assert telemetry.nozzle_temp == 0.0
