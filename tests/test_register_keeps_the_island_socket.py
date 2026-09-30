"""Registering the a3d module must not cost it its Dynamic Island.

The sibling module (Aethelark-Trade) once regenerated its manifest on every
install, and the renderer emitted no `[island]` section — so each install
silently deleted the card configuration and the module answered with a blank
card. a3d cannot regress that exact way, because it declares NO `[island]`
cards at all: its socket is found by DIRECTORY CONVENTION — the host reads
`island/template.html` next to the manifest. The failure mode it CAN have is
subtler: register "succeeds" but the convention entrypoint never ships or never
copies, and the module installs a socket that draws nothing.

These tests hold that line. They pin a3d's actual shape (a faceless,
convention-island module), and they fail if registration stops copying the
template, stops shipping every tool, or writes to the wrong path.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from aethelark3d import register as register_mod

ENTRYPOINT = register_mod.CONVENTION_ISLAND_ENTRYPOINT  # "template.html"


def _shipped_manifest() -> dict:
    src = register_mod.shipped_module_dir() / "manifest.toml"
    return tomllib.loads(src.read_text(encoding="utf-8"))


def test_the_module_is_faceless_convention_island_shaped():
    """a3d declares no [island] cards; its socket is the shipped island/ dir.

    This is the invariant register.py is built around — if someone adds an
    [island] section to the manifest, the convention-vs-named question reopens
    and this test is the place that says so out loud.
    """
    island = _shipped_manifest().get("island") or {}
    assert not island.get("cards") and not island.get("template") and not island.get("css"), (
        "a3d's [island] names cards or files — its cards are a convention "
        "(island/template.html), not manifest-named tables")
    src = register_mod.shipped_module_dir()
    assert (src / "island" / ENTRYPOINT).is_file(), (
        "the convention entrypoint island/template.html is not shipped — "
        "the module would draw nothing")


def test_register_installs_the_convention_entrypoint(tmp_path):
    """The card the host actually reads lands next to the manifest."""
    target = Path(register_mod.register(tmp_path))
    entry = target.parent / "island" / ENTRYPOINT
    assert entry.is_file(), "island/template.html was not installed"
    assert entry.stat().st_size > 0


def test_register_copies_every_island_asset(tmp_path):
    """Not just the template — the whole island/ dir rides along (style.css)."""
    src_island = register_mod.shipped_module_dir() / "island"
    shipped = {p.name for p in src_island.iterdir() if p.is_file()}
    target = Path(register_mod.register(tmp_path))
    installed = {p.name for p in (target.parent / "island").iterdir() if p.is_file()}
    assert installed == shipped, f"island assets changed on install: {shipped ^ installed}"


def test_register_installs_every_shipped_tool(tmp_path):
    """Every tool in the shipped manifest survives the copy, none invented."""
    target = register_mod.register(tmp_path)
    names, cards = register_mod.registration_summary(target)
    shipped_names = [t["name"] for t in _shipped_manifest()["tools"]]
    assert names == shipped_names
    assert len(names) >= 1


def test_registration_summary_reports_zero_declared_cards(tmp_path):
    """The count is the manifest's own [island] declaration, which is empty —
    NOT a claim that the module draws nothing. The socket draws by convention."""
    target = register_mod.register(tmp_path)
    _, cards = register_mod.registration_summary(target)
    assert cards == 0


def test_register_is_idempotent(tmp_path):
    """Running it twice installs the same bytes and loses nothing — the case
    that used to delete the sibling module's island."""
    first = Path(register_mod.register(tmp_path))
    before = first.read_text(encoding="utf-8")
    entry_before = (first.parent / "island" / ENTRYPOINT).read_text(encoding="utf-8")
    second = Path(register_mod.register(tmp_path))
    assert second == first
    assert second.read_text(encoding="utf-8") == before
    assert (second.parent / "island" / ENTRYPOINT).read_text(encoding="utf-8") == entry_before


def test_register_writes_where_the_bus_reads(tmp_path):
    """<key>/manifest.toml wins the loader's sorted glob; <key>.toml loses."""
    target = Path(register_mod.register(tmp_path))
    assert target.name == "manifest.toml"
    assert target.parent.name == register_mod.MODULE_KEY  # "a3d"


def test_a_shipped_manifest_with_no_tools_is_refused(tmp_path, monkeypatch):
    """A socket with no tools is a dead module — fail loudly, don't install it."""
    broken = tmp_path / "pkg"
    (broken / "island").mkdir(parents=True)
    (broken / "island" / ENTRYPOINT).write_text("<html></html>", encoding="utf-8")
    (broken / "manifest.toml").write_text(
        'key = "a3d"\nbinary = "a3d"\ntools = []\n', encoding="utf-8")
    monkeypatch.setattr(register_mod, "shipped_module_dir", lambda: broken)

    with pytest.raises(register_mod.RegistrationError, match="no tools"):
        register_mod.register(tmp_path / "modules")


def test_a_manifest_with_the_wrong_key_is_refused(tmp_path, monkeypatch):
    """The manifest's key routes every tool call; a mismatch installs a module
    the bus will never find under its own name."""
    broken = tmp_path / "pkg"
    (broken / "island").mkdir(parents=True)
    (broken / "island" / ENTRYPOINT).write_text("<html></html>", encoding="utf-8")
    (broken / "manifest.toml").write_text(
        'key = "not_a3d"\nbinary = "a3d"\n[[tools]]\nname = "x"\n', encoding="utf-8")
    monkeypatch.setattr(register_mod, "shipped_module_dir", lambda: broken)

    with pytest.raises(register_mod.RegistrationError, match="key"):
        register_mod.register(tmp_path / "modules")
