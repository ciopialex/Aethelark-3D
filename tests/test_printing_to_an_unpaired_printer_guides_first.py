"""Don't download and slice for ten minutes, then fail at a locked printer.

A print aimed at a printer that still needs its access key must say so up front
— before any download or slice — because the frictionless answer is one
sentence: read the key off the screen and pair it. The old flow ran the whole
pipeline and only met the locked printer at the very end (and with --no-start
even reported success), which is the least helpful moment to find out.

The pre-flight fires only when the print will actually be SENT (auto_start) to a
real printer (not the simulator). It is proven here by making download_model
explode: if the pre-flight is doing its job, that explosion is never reached.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aethelark3d import api


class _Fleet:
    def __init__(self, slot):
        self._slot = slot

    def get_printer(self, key):
        return self._slot

    def key_for(self, said):
        return said


def _boom(*a, **k):
    raise AssertionError("download_model was called — the pre-flight did not "
                         "short-circuit a print to an unpaired printer")


def test_a_locked_printer_stops_the_pipeline_before_any_download():
    locked = {"name": "Elegoo 7526B5", "needs_access_code": True, "access_code": None}
    with patch("aethelark3d.config.config", _Fleet(locked)), \
         patch.object(api, "download_model", _boom):
        res = api.find_and_prepare_print(query="calibration cube",
                                         printer_key="ELEGOO_7526B5", auto_start=True)
    assert res["success"] is False
    assert res["needs_pairing"] is True
    assert "key" in res["error"].lower() and "pair" in res["error"].lower()


def test_a_paired_printer_is_not_blocked():
    """A printer with a key set proceeds — the check must not block real prints."""
    paired = {"name": "CC2", "needs_access_code": False, "access_code": "Ab3dEf"}
    reached = {"download": False}

    def stop(*a, **k):
        reached["download"] = True
        return {"success": False, "error": "stopped after the gate on purpose"}

    with patch("aethelark3d.config.config", _Fleet(paired)), \
         patch.object(api, "download_model", stop):
        api.find_and_prepare_print(query="x", printer_key="CC2", auto_start=True, filament="PLA")
    assert reached["download"], "a paired printer was wrongly blocked by the pairing gate"


def test_no_start_does_not_trip_the_gate():
    """--no-start means 'prepare only, don't send', so a missing key is moot."""
    locked = {"name": "CC2", "needs_access_code": True, "access_code": None}
    reached = {"download": False}

    def stop(*a, **k):
        reached["download"] = True
        return {"success": False, "error": "stopped"}

    with patch("aethelark3d.config.config", _Fleet(locked)), \
         patch.object(api, "download_model", stop):
        api.find_and_prepare_print(query="x", printer_key="CC2", auto_start=False, filament="PLA")
    assert reached["download"], "the pairing gate fired even though nothing was being sent"
