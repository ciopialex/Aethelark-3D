import asyncio

from aethelark3d.drivers.base import PrinterState
from aethelark3d.drivers.elegoo import ElegooSDCPDriver


def _state(monkeypatch, current, job):
    d = ElegooSDCPDriver("CC1", "10.0.0.9")
    monkeypatch.setattr(ElegooSDCPDriver, "is_cc2_family", property(lambda self: False))

    async def send(cmd, data, **kw):
        return {"CurrentStatus": [current], "PrintInfo": {"Status": job, "CurrentLayer": 3,
                                                          "TotalLayer": 100}}
    monkeypatch.setattr(d, "_send_sdcp", send)
    monkeypatch.setattr(d, "connect", lambda: asyncio.sleep(0, True))
    return asyncio.run(d.get_telemetry()).state


def test_a_file_arriving_is_not_a_paused_print(monkeypatch):
    """2026-09-29: CurrentStatus 2 (file transferring) was read as PAUSED, so
    every print raised a 'paused' alert eleven seconds after it started."""
    assert _state(monkeypatch, 2, 13) != PrinterState.PAUSED


def test_a_paused_job_is_paused_and_a_running_one_prints(monkeypatch):
    assert _state(monkeypatch, 1, 6) == PrinterState.PAUSED
    assert _state(monkeypatch, 1, 13) == PrinterState.PRINTING


import contextlib
from types import SimpleNamespace

import pytest


def _cc2_state(monkeypatch, machine_status, job_code):
    d = ElegooSDCPDriver("CC2", "10.0.0.5")
    monkeypatch.setattr(ElegooSDCPDriver, "is_cc2_family", property(lambda self: True))
    stat = SimpleNamespace(
        print_info=SimpleNamespace(status=job_code, progress=40, current_layer=3, total_layer=100,
                                   filename="x.gcode", print_speed_pct=100),
        raw={"_cc2": {"machine_status": machine_status, "remaining_time_sec": 600}},
        temp_nozzle=270, temp_nozzle_target=270, temp_bed=110, temp_bed_target=110, temp_chamber=40)

    class _Printer:
        async def status(self, timeout=None):
            return stat

    @contextlib.asynccontextmanager
    async def fake(enable_control=False):
        yield _Printer()
    monkeypatch.setattr(d, "_cc2", fake)
    return asyncio.run(d.get_telemetry()).state


@pytest.mark.parametrize("job, want", [
    (13, PrinterState.PRINTING), (6, PrinterState.PAUSED), (5, PrinterState.PAUSED),
    (16, PrinterState.HEATING), (15, PrinterState.LEVELING), (9, PrinterState.COMPLETED)])
def test_both_printers_read_a_job_the_same_way(monkeypatch, job, want):
    """The Carbon 2 reports the Carbon 1's job codes through pycentauri. A paused
    Carbon 2 used to read as printing, so 'resume' said it was not paused."""
    assert _state(monkeypatch, 1, job) == want
    assert _cc2_state(monkeypatch, 2, job) == want


def test_an_idle_carbon_2_is_idle(monkeypatch):
    assert _cc2_state(monkeypatch, 1, 0) == PrinterState.IDLE
