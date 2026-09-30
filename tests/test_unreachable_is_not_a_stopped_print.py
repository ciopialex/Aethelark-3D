"""A printer we cannot reach is not a printer that stopped mid-print.

`poll_single_frame` collapsed DISCONNECTED and ERROR into one event:

    printer_error / "CC1 STOPPED" / "Connection lost or feed stalled •
    Hotend protected" / "Check Printer" / #FF453A / priority 100

For a genuine fault that is right. For a printer that is switched off, on
another network, or simply not there, every clause is invented: nothing
stopped, no feed stalled, and we know nothing about the hotend -- we could not
open a socket. Observed on 2026-09-04: two of three printers sat on a subnet
the laptop could not reach, and the island carried a red hardware alarm about a
print that never existed, for thirteen hours.

Priority made it worse. printer_error outranks print_progress 100 to 50, so an
unreachable printer's fabricated alarm displaced a real print running on a
different machine.

ERROR keeps the alarm. DISCONNECTED reports what is actually known: we could
not reach it.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict

import pytest

from aethelark3d.drivers.base import PrinterState, PrinterTelemetry
from aethelark3d.streamer import FleetSDCPStreamer, SDCPEventStreamer


def _telemetry(state: PrinterState) -> PrinterTelemetry:
    return PrinterTelemetry(
        printer_name="CC1", brand="Elegoo", ip_address="192.168.8.106", state=state)


def _event_for(state: PrinterState):
    streamer = SDCPEventStreamer("CC1", use_simulator=False)

    async def _fake_telemetry():
        return _telemetry(state)

    streamer.driver.get_telemetry = _fake_telemetry     # type: ignore[assignment]
    return asyncio.run(streamer.poll_single_frame())


def test_an_unreachable_printer_does_not_raise_a_hardware_alarm():
    event = _event_for(PrinterState.DISCONNECTED)

    assert event.event != "printer_error", (
        "an unreachable printer raised the same alarm as a thermal fault")
    assert "STOPPED" not in event.title.upper(), (
        f"title claims the printer stopped: {event.title!r}")
    assert "hotend" not in event.detail.lower(), (
        f"detail claims knowledge of a hotend we never reached: {event.detail!r}")
    assert "stalled" not in event.detail.lower(), (
        f"detail claims a feed stalled: {event.detail!r}")


def test_an_unreachable_printer_does_not_outrank_a_real_print():
    """A fabricated alarm must not displace a job that is genuinely running."""
    event = _event_for(PrinterState.DISCONNECTED)
    priority = FleetSDCPStreamer.EVENT_PRIORITY
    assert priority.get(event.event, 0) < priority["print_progress"], (
        f"{event.event!r} outranks an active print, so an unreachable machine "
        f"hides a real one")


def test_a_real_fault_still_raises_the_alarm():
    """The fix must not silence the case the alarm exists for."""
    event = _event_for(PrinterState.ERROR)
    assert event.event == "printer_error"
    assert FleetSDCPStreamer.EVENT_PRIORITY[event.event] == 100


def test_the_unreachable_event_is_ranked():
    """An event nobody ranked sorts below every ranked one in the fleet's own
    sort, and sits mid-band on the host -- over a running print's progress."""
    declared = set(FleetSDCPStreamer.EVENT_PRIORITY)
    event = _event_for(PrinterState.DISCONNECTED)
    assert event.event in declared, (
        f"{event.event!r} is not in the declared event set {sorted(declared)}")
