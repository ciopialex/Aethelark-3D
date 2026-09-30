"""Resolve "the dice in downloads" and "CC2" to the real file and printer.

A voice model strips context. "print the YES_NO_DICE in downloads on the cc2"
arrives as query_or_id="YES_NO_DICE", printer="CC2" — a bare name and a
callsign. Two gaps that made the pipeline reach for the wrong thing:

  - a bare name went straight to a MakerWorld web search, fetching a stranger's
    model instead of the file the user is looking at;
  - "CC2" is a callsign, not a fleet key (discovery named the printer
    ELEGOO_7526B5), so the driver factory raised "no printer called 'CC2'".

Both are resolved now: a bare name is matched against the folders a person
keeps models in first, and a callsign is resolved through the alias table
wherever a printer is looked up by name.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aethelark3d import api
from aethelark3d.config import Config


# ── local model resolution ──────────────────────────────────────────────────

def test_a_bare_name_finds_a_local_file(tmp_path, monkeypatch):
    d = tmp_path / "Downloads"
    d.mkdir()
    (d / "YES_NO_DICE.3mf").write_bytes(b"PK\x03\x04fake")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    assert api._find_local_model("YES_NO_DICE") == d / "YES_NO_DICE.3mf"


def test_case_and_separators_do_not_matter(tmp_path, monkeypatch):
    d = tmp_path / "Downloads"; d.mkdir()
    (d / "YES_NO_DICE.3mf").write_bytes(b"x")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    assert api._find_local_model("yes no dice") is not None
    assert api._find_local_model("yes-no-dice") is not None


def test_a_model_folder_named_the_obvious_way_is_searched(tmp_path, monkeypatch):
    d = tmp_path / "3D_Prints"; d.mkdir()
    (d / "benchy.stl").write_bytes(b"x")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    assert api._find_local_model("benchy") == d / "benchy.stl"


def test_a_name_with_no_local_file_falls_through_to_the_web(tmp_path, monkeypatch):
    (tmp_path / "Downloads").mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    assert api._find_local_model("some model nobody has") is None


def test_a_path_is_not_treated_as_a_bare_name(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    assert api._find_local_model("/etc/passwd") is None  # has a slash → not a name


# ── printer callsign resolution ─────────────────────────────────────────────

@pytest.fixture
def cfg(tmp_path, monkeypatch):
    c = Config(config_dir=tmp_path)   # not HOME: see test_pairing_a_locked_printer
    c._data["printers"]["ELEGOO_7526B5"] = {"name": "Elegoo 7526B5", "host": "1.2.3.4"}
    c._data.setdefault("aliases", {})["CC2"] = "ELEGOO_7526B5"
    return c


def test_a_callsign_resolves_to_the_real_printer(cfg):
    slot = cfg.get_printer("CC2")
    assert slot is not None and slot["name"] == "Elegoo 7526B5"


def test_a_real_key_still_resolves(cfg):
    assert cfg.get_printer("ELEGOO_7526B5") is not None


def test_an_unknown_name_is_none(cfg):
    assert cfg.get_printer("nope") is None
