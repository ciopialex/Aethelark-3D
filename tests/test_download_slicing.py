"""Turning a finished download into an ETA, without making the user wait for it.

Event 1 carries what is knowable the instant the file lands: a bounding box is
arithmetic over vertices already on disk. Event 2 carries what a slicer says,
which costs seconds to minutes of real work. They are separate because holding
the first until the second is ready shows nothing while the answer is already
known.

The build-volume check comes first and is not an optimisation. Slicing a part
that cannot fit the machine spends minutes to produce a G-code file that
nothing can print, so a part that does not fit is refused before the slicer is
started rather than after it finishes.
"""
from __future__ import annotations

import struct
import time
from pathlib import Path

import pytest

from aethelark3d import downloads

FLEET = {
    "CC1": {"model": "Elegoo Centauri Carbon", "host": "172.20.10.4"},
    "CC2": {"model": "Elegoo Centauri Carbon 2", "host": "192.168.1.55"},
    "GHOST": {"model": "Elegoo Centauri 2", "host": ""},
}


def _stl(path: Path, span: tuple[float, float, float]) -> Path:
    x, y, z = span
    tri = [((0.0, 0.0, 0.0), (x, 0.0, 0.0), (0.0, y, z))]
    with path.open("wb") as fh:
        fh.write(b"\0" * 80)
        fh.write(struct.pack("<I", len(tri)))
        for facet in tri:
            fh.write(struct.pack("<3f", 0.0, 0.0, 1.0))
            for vertex in facet:
                fh.write(struct.pack("<3f", *vertex))
            fh.write(struct.pack("<H", 0))
    return path


class _Slicer:
    """Stands in for the real slicer, which costs minutes and a toolchain."""

    def __init__(self, seconds: int = 4830, grams: float = 41.2,
                 fail: str | None = None):
        self.calls: list[tuple[Path, str]] = []
        self.seconds, self.grams, self.fail = seconds, grams, fail

    def __call__(self, model: Path, printer: str):
        self.calls.append((Path(model), printer))
        if self.fail:
            raise RuntimeError(self.fail)
        return {"estimated_time_seconds": self.seconds,
                "filament_mass_grams": self.grams}


# ── build volume ────────────────────────────────────────────────────────────

def test_a_machine_reports_the_volume_its_driver_declares():
    volume = downloads.build_volume_for("CC1", FLEET)
    assert volume == {"x": 256.0, "y": 256.0, "z": 256.0}


def test_an_unknown_machine_still_reports_something_usable():
    assert downloads.build_volume_for("NOPE", {}) is not None


# ── the oversized case ──────────────────────────────────────────────────────

def test_a_part_too_big_for_the_machine_is_never_sliced(tmp_path):
    """Slicing it would spend minutes producing G-code nothing can print."""
    model = _stl(tmp_path / "huge.stl", (400.0, 400.0, 400.0))
    slicer = _Slicer()
    events: list[dict] = []

    downloads.estimate(model, printer="CC1", fleet=FLEET,
                       on_event=events.append, slicer=slicer)

    assert slicer.calls == [], "the slicer ran on a part that cannot fit"
    assert [e["event"] for e in events] == ["build_volume_exceeded"]


def test_the_refusal_says_what_did_not_fit(tmp_path):
    model = _stl(tmp_path / "huge.stl", (400.0, 10.0, 10.0))
    events: list[dict] = []

    downloads.estimate(model, printer="CC1", fleet=FLEET,
                       on_event=events.append, slicer=_Slicer())

    event = events[0]
    assert event["dimensions"]["x"] == pytest.approx(400.0)
    assert event["build_volume"]["x"] == pytest.approx(256.0)
    assert event["printer"] == "CC1"
    assert event["name"] == "huge.stl"


def test_a_part_that_fits_exactly_is_allowed(tmp_path):
    """256mm on a 256mm bed fits. An off-by-one here refuses real parts."""
    model = _stl(tmp_path / "snug.stl", (256.0, 256.0, 256.0))
    slicer = _Slicer()

    downloads.estimate(model, printer="CC1", fleet=FLEET,
                       on_event=lambda e: None, slicer=slicer)

    assert len(slicer.calls) == 1


# ── the estimate ────────────────────────────────────────────────────────────

def test_a_fitting_part_gets_an_eta_and_a_material_cost(tmp_path):
    model = _stl(tmp_path / "bracket.stl", (40.0, 40.0, 25.0))
    events: list[dict] = []

    downloads.estimate(model, printer="CC1", fleet=FLEET,
                       on_event=events.append, slicer=_Slicer())

    assert [e["event"] for e in events] == ["model_estimated"]
    event = events[0]
    assert event["eta_seconds"] == 4830
    assert event["filament_grams"] == pytest.approx(41.2)
    assert event["printer"] == "CC1"


def test_the_estimate_names_the_same_file_the_detection_did(tmp_path):
    model = _stl(tmp_path / "same.stl", (40.0, 40.0, 25.0))
    first = downloads.detected_event(model, FLEET)
    events: list[dict] = []
    downloads.estimate(model, printer=first["printer"], fleet=FLEET,
                       on_event=events.append, slicer=_Slicer())

    assert events[0]["path"] == first["path"]


def test_a_slicer_that_fails_reports_it_rather_than_vanishing(tmp_path):
    """A missing slicer toolchain is common and is not a crash."""
    model = _stl(tmp_path / "bracket.stl", (40.0, 40.0, 25.0))
    events: list[dict] = []

    downloads.estimate(model, printer="CC1", fleet=FLEET,
                       on_event=events.append,
                       slicer=_Slicer(fail="ElegooSlicer not installed"))

    assert [e["event"] for e in events] == ["model_estimate_failed"]
    assert "ElegooSlicer" in events[0]["detail"]


def test_an_unmeasurable_model_is_still_sliced(tmp_path):
    """A STEP body has no bounding box we can read, but the slicer can open it.
    Refusing it for not fitting a volume we never measured would be inventing a
    reason."""
    step = tmp_path / "part.step"
    step.write_text("ISO-10303-21;\n")
    slicer = _Slicer()

    downloads.estimate(step, printer="CC1", fleet=FLEET,
                       on_event=lambda e: None, slicer=slicer)

    assert len(slicer.calls) == 1


# ── the two events together ─────────────────────────────────────────────────

def test_a_dropped_model_produces_detection_then_estimate(tmp_path):
    """End to end: the file lands, the box is reported immediately, and the ETA
    follows when the slicer is done."""
    import threading

    seen: list[dict] = []
    slicer = _Slicer()
    watcher = threading.Thread(
        target=downloads.watch, args=(tmp_path, seen.append),
        kwargs={"fleet": FLEET, "interval": 0.05, "iterations": 30,
                "slicer": slicer},
        daemon=True)
    watcher.start()

    time.sleep(0.12)
    _stl(tmp_path / "bracket.stl", (40.0, 40.0, 25.0))
    watcher.join(timeout=5.0)
    downloads.wait_for_estimates(2.0)

    kinds = [e["event"] for e in seen]
    assert kinds[0] == "model_detected", f"got {kinds}"
    assert "model_estimated" in kinds, f"the ETA never arrived: {kinds}"


def test_detection_does_not_wait_for_the_slicer(tmp_path):
    """The whole reason for two events. If Event 1 blocked on slicing, the
    island would show nothing while the bounding box was already known."""
    import threading

    class _Slow(_Slicer):
        def __call__(self, model, printer):
            time.sleep(1.5)
            return super().__call__(model, printer)

    seen: list[dict] = []
    watcher = threading.Thread(
        target=downloads.watch, args=(tmp_path, seen.append),
        kwargs={"fleet": FLEET, "interval": 0.05, "iterations": 12,
                "slicer": _Slow()},
        daemon=True)
    watcher.start()
    time.sleep(0.1)
    _stl(tmp_path / "slow.stl", (40.0, 40.0, 25.0))

    deadline = time.monotonic() + 1.0
    while not seen and time.monotonic() < deadline:
        time.sleep(0.02)

    assert seen and seen[0]["event"] == "model_detected", (
        "detection was held up by a slicer that takes 1.5s")
    watcher.join(timeout=5.0)
    downloads.wait_for_estimates(4.0)


def test_watching_without_a_slicer_still_detects(tmp_path, monkeypatch):
    """A machine with no slicer toolchain still gets Event 1. Simulated rather
    than assumed: this machine has a slicer, so without the patch the test
    would be asserting that a working install does not work."""
    import threading

    monkeypatch.setattr(downloads, "_slicer_available", lambda: False)
    seen: list[dict] = []
    watcher = threading.Thread(
        target=downloads.watch, args=(tmp_path, seen.append),
        kwargs={"fleet": FLEET, "interval": 0.05, "iterations": 20},
        daemon=True)
    watcher.start()
    time.sleep(0.1)
    _stl(tmp_path / "plain.stl", (10.0, 10.0, 10.0))
    watcher.join(timeout=5.0)

    assert [e["event"] for e in seen] == ["model_detected"]
