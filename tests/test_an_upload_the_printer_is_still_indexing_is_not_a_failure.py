import asyncio

from aethelark3d.drivers import elegoo
from aethelark3d.drivers.elegoo import ElegooSDCPDriver


class _Ok:
    status_code = 200
    text = "{}"

    def json(self):
        return {"success": True}


def _driver(monkeypatch, listings):
    d = ElegooSDCPDriver("CC1", "10.0.0.9")
    monkeypatch.setattr(ElegooSDCPDriver, "is_cc2_family", property(lambda self: False))
    monkeypatch.setattr(elegoo.requests, "post", lambda *a, **k: _Ok())
    monkeypatch.setattr(elegoo, "_remember_thumb", lambda *a: None)
    calls = iter(listings)

    async def send(cmd, data, **kw):
        return {"FileList": [{"name": n} for n in next(calls)]}

    async def no_wait(_):
        return None

    monkeypatch.setattr(d, "_send_sdcp", send)
    monkeypatch.setattr(elegoo.asyncio, "sleep", no_wait)
    return d


def test_the_file_appearing_a_moment_later_is_an_upload_that_worked(monkeypatch, tmp_path):
    job = tmp_path / "stand.gcode"
    job.write_bytes(b"G28\n")
    d = _driver(monkeypatch, [["/local/old.gcode"], ["/local/old.gcode"],
                              ["/local/old.gcode", "/local/stand.gcode"]])
    assert asyncio.run(d.upload_job(job)) == "/local/stand.gcode"


def test_a_file_that_never_appears_is_still_a_failure(monkeypatch, tmp_path):
    job = tmp_path / "stand.gcode"
    job.write_bytes(b"G28\n")
    d = _driver(monkeypatch, [["/local/old.gcode"]] * 12)
    try:
        asyncio.run(d.upload_job(job))
    except RuntimeError as e:
        assert "not on the printer" in str(e)
    else:
        raise AssertionError("an upload the printer never listed was reported as done")
