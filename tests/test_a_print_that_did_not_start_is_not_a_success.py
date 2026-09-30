"""A prepared print that never reached the printer is a failure, and says why.

`find_and_prepare_print` returned success=True as soon as slicing worked, with
the driver's error swallowed. `a3d print --json` then exited 0, the eagle read
exit 0 as ok=true -- and the voice model, told to trust ok, said the print had
started while the printer was unreachable. print_batch already refused to call
that "started"; the single print agrees with it now, and every failure exits
non-zero so the harness can see it.
"""
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aethelark3d import api, cli


class _Unreachable:
    async def upload_job(self, path):
        raise ConnectionError("no route to host")

    async def start_print(self, path, auto_level=True):
        raise AssertionError("never reached")


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    model = tmp_path / "stand.3mf"
    model.write_bytes(b"x")
    monkeypatch.setattr(api, "download_model", lambda **k: {
        "success": True, "file_path": str(model), "title": "Phone stand",
        "weight": "12g"})
    monkeypatch.setattr(api, "slice_headless", lambda **k: tmp_path / "stand.gcode")

    async def connect(driver, key, make):
        return driver
    monkeypatch.setattr(api, "connect_with_rediscovery", connect)

    import aethelark3d.drivers.factory as factory
    monkeypatch.setattr(factory, "get_driver_for_printer",
                        lambda *a, **k: _Unreachable())
    import aethelark3d.spools as spools
    monkeypatch.setattr(spools.FilamentVault, "check_sufficiency",
                        staticmethod(lambda *a, **k: (True, 1000, "")))
    monkeypatch.setattr(api.config, "get_printer", lambda key: {"name": key})


def test_an_unreachable_printer_is_not_a_started_print(pipeline):
    res = api.find_and_prepare_print("phone stand", printer_key="P1", filament="PLA")
    assert res["success"] is False
    assert res["print_command_sent"] is False
    assert "could not be reached" in res["error"]
    assert "no route to host" in res["error"]
    assert res["guidance"]


def test_the_cli_exits_non_zero_so_the_eagle_sees_it(pipeline, monkeypatch):
    out = CliRunner().invoke(cli.app, ["print", "phone stand", "--printer", "P1",
                                       "--json"])
    assert out.exit_code == 1
    assert '"success": false' in out.stdout


def test_a_download_that_failed_exits_non_zero(monkeypatch):
    monkeypatch.setattr(api, "download_model",
                        lambda **k: {"success": False, "error": "nothing matched"})
    out = CliRunner().invoke(cli.app, ["download", "zzz", "--json"])
    assert out.exit_code == 1
