import json

import pytest
import typer

from aethelark3d import cli
from aethelark3d.config import config
from aethelark3d.drivers import factory


class _Driver:
    def __init__(self, key, log):
        self.key, self.log = key, log

    async def pause_print(self):
        self.log.append(("pause", self.key))
        return True

    async def resume_print(self):
        self.log.append(("resume", self.key))
        return True

    async def stop_print(self):
        self.log.append(("stop", self.key))
        return True


@pytest.fixture
def fleet(monkeypatch):
    states, log = {}, []
    monkeypatch.setattr(config, "addressable_printer_keys", lambda: list(states))
    monkeypatch.setattr(cli, "_printer_state", lambda key, sim=False: states.get(key, "DISCONNECTED"))
    monkeypatch.setattr(factory, "get_driver_for_printer",
                        lambda key, use_simulator=False: _Driver(key, log))
    return states, log


def _run(action, printer, capsys):
    code = 0
    try:
        cli._control_print(action, printer, True)
    except typer.Exit as e:
        code = e.exit_code
    return json.loads(capsys.readouterr().out), code


def test_unnamed_stop_reaches_the_only_busy_printer(fleet, capsys):
    states, log = fleet
    states.update({"CC1": "IDLE", "CC2": "PRINTING"})
    out, code = _run("stop", None, capsys)
    assert code == 0 and log == [("stop", "CC2")]


def test_unnamed_stop_with_two_busy_printers_touches_neither(fleet, capsys):
    states, log = fleet
    states.update({"CC1": "PRINTING", "CC2": "PAUSED"})
    out, code = _run("stop", None, capsys)
    assert code == 1 and log == []
    assert "which printer" in out["guidance"]


def test_all_pauses_every_busy_printer_and_skips_idle_ones(fleet, capsys):
    states, log = fleet
    states.update({"CC1": "PRINTING", "CC2": "PRINTING", "C2": "IDLE"})
    out, code = _run("pause", "all", capsys)
    assert code == 0 and sorted(log) == [("pause", "CC1"), ("pause", "CC2")]


def test_a_named_printer_is_the_only_one_touched(fleet, capsys):
    states, log = fleet
    states.update({"CC1": "PRINTING", "CC2": "PRINTING"})
    out, code = _run("stop", "CC1", capsys)
    assert code == 0 and log == [("stop", "CC1")]


def test_resume_without_a_name_finds_the_paused_one(fleet, capsys):
    states, log = fleet
    states.update({"CC1": "PRINTING", "CC2": "PAUSED"})
    out, code = _run("resume", None, capsys)
    assert code == 0 and log == [("resume", "CC2")]
