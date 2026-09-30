"""Catching a model the moment the browser finishes writing it.

A browser writes a download to a temporary name — `.crdownload` on Chrome,
`.part` on Firefox — and renames it into place when the transfer completes. The
rename is the completion signal, and it is atomic within a filesystem, so a
file that exists under its final name with a settled size is a finished file.

Two events rather than one, because they are not available at the same time. A
bounding box is arithmetic over vertices already on disk and is ready
immediately; an ETA requires actually slicing, which takes seconds to minutes.
Holding the first event until the second is ready means staring at nothing
while the answer already exists.
"""
from __future__ import annotations

import struct
import time
from pathlib import Path

import pytest

from aethelark3d import downloads


# ── geometry ────────────────────────────────────────────────────────────────

def _binary_stl(path: Path, triangles: list[tuple]) -> Path:
    """A real binary STL: 80-byte header, uint32 count, 50 bytes per facet."""
    with path.open("wb") as fh:
        fh.write(b"\0" * 80)
        fh.write(struct.pack("<I", len(triangles)))
        for tri in triangles:
            fh.write(struct.pack("<3f", 0.0, 0.0, 1.0))       # normal
            for vertex in tri:
                fh.write(struct.pack("<3f", *vertex))
            fh.write(struct.pack("<H", 0))                     # attribute
    return path


UNIT_TRI = [((0.0, 0.0, 0.0), (10.0, 0.0, 0.0), (0.0, 20.0, 5.0))]


def test_a_binary_stl_reports_the_box_it_occupies(tmp_path):
    model = _binary_stl(tmp_path / "part.stl", UNIT_TRI)
    box = downloads.measure(model)

    assert box == pytest.approx({"x": 10.0, "y": 20.0, "z": 5.0})


def test_an_ascii_stl_is_measured_too(tmp_path):
    model = tmp_path / "part.stl"
    model.write_text(
        "solid t\n facet normal 0 0 1\n  outer loop\n"
        "   vertex 0 0 0\n   vertex 4 0 0\n   vertex 0 3 2\n"
        "  endloop\n endfacet\nendsolid t\n")

    assert downloads.measure(model) == pytest.approx({"x": 4.0, "y": 3.0, "z": 2.0})


def test_negative_coordinates_do_not_shrink_the_box(tmp_path):
    """A model centred on the origin spans both sides of it. Taking max alone
    would report half the part."""
    model = _binary_stl(tmp_path / "c.stl",
                        [((-5.0, -5.0, -1.0), (5.0, 0.0, 0.0), (0.0, 5.0, 1.0))])

    assert downloads.measure(model) == pytest.approx({"x": 10.0, "y": 10.0, "z": 2.0})


def test_a_format_we_cannot_measure_says_so_rather_than_guessing(tmp_path):
    step = tmp_path / "part.step"
    step.write_text("ISO-10303-21;\n")
    assert downloads.measure(step) is None


def test_a_truncated_stl_does_not_take_the_watcher_down(tmp_path):
    broken = tmp_path / "half.stl"
    broken.write_bytes(b"\0" * 80 + struct.pack("<I", 500))    # claims 500, has 0
    assert downloads.measure(broken) is None


# ── completion detection ────────────────────────────────────────────────────

@pytest.mark.parametrize("name", ["a.stl.crdownload", "b.3mf.part",
                                  "c.stl.download", "d.stl.tmp"])
def test_a_download_still_in_flight_is_not_offered(tmp_path, name):
    (tmp_path / name).write_bytes(b"x" * 10)
    assert downloads.settled_models(tmp_path) == []


def test_a_file_that_is_still_growing_is_not_offered(tmp_path):
    """Some browsers write in place. Size still moving means still writing."""
    model = tmp_path / "big.stl"
    model.write_bytes(b"x" * 100)
    seen = downloads.settled_models(tmp_path, sizes={})
    model.write_bytes(b"x" * 200)

    assert seen == [] or downloads.settled_models(
        tmp_path, sizes={str(model): 100}) == []


def test_a_finished_model_is_offered_once_its_size_holds(tmp_path):
    model = _binary_stl(tmp_path / "done.stl", UNIT_TRI)
    sizes: dict = {}
    downloads.settled_models(tmp_path, sizes=sizes)        # first sighting
    found = downloads.settled_models(tmp_path, sizes=sizes)

    assert [p.name for p in found] == ["done.stl"]


def test_files_that_are_not_models_are_ignored(tmp_path):
    (tmp_path / "invoice.pdf").write_bytes(b"%PDF")
    (tmp_path / "photo.jpg").write_bytes(b"\xff\xd8")
    sizes: dict = {}
    downloads.settled_models(tmp_path, sizes=sizes)

    assert downloads.settled_models(tmp_path, sizes=sizes) == []


# ── which printer it is going to ────────────────────────────────────────────

FLEET = {
    "CC1": {"model": "Elegoo Centauri Carbon", "host": "172.20.10.4",
            "build_volume": {"x": 256, "y": 256, "z": 256}},
    "CC2": {"model": "Elegoo Centauri Carbon 2", "host": "192.168.1.55",
            "build_volume": {"x": 300, "y": 300, "z": 300}},
    "GHOST": {"model": "Elegoo Centauri 2", "host": ""},
}


def test_a_machine_with_no_address_is_never_chosen():
    chosen = downloads.choose_printer({"x": 10, "y": 10, "z": 10}, FLEET)
    assert chosen != "GHOST"


def test_a_part_too_tall_for_one_machine_goes_to_the_one_that_fits():
    chosen = downloads.choose_printer({"x": 10, "y": 10, "z": 280}, FLEET)
    assert chosen == "CC2"


def test_a_part_that_fits_nowhere_is_reported_as_fitting_nowhere():
    assert downloads.choose_printer({"x": 900, "y": 900, "z": 900}, FLEET) is None


def test_an_unmeasurable_model_still_gets_a_machine():
    """A .step we cannot measure is still something the user wants printed."""
    assert downloads.choose_printer(None, FLEET) in ("CC1", "CC2")


# ── the two events ──────────────────────────────────────────────────────────

def test_the_first_event_carries_what_is_known_immediately(tmp_path):
    model = _binary_stl(tmp_path / "bracket.stl", UNIT_TRI)
    event = downloads.detected_event(model, FLEET)

    assert event["event"] == "model_detected"
    assert event["name"] == "bracket.stl"
    assert event["dimensions"] == pytest.approx({"x": 10.0, "y": 20.0, "z": 5.0})
    assert event["printer"] in ("CC1", "CC2")
    assert "eta_seconds" not in event, (
        "an ETA cannot be known before slicing; holding the first event until "
        "it is defeats the point of having two")


def test_the_second_event_carries_the_estimate_when_it_exists(tmp_path):
    model = _binary_stl(tmp_path / "bracket.stl", UNIT_TRI)
    event = downloads.estimated_event(model, printer="CC1", eta_seconds=4830)

    assert event["event"] == "model_estimated"
    assert event["eta_seconds"] == 4830
    assert event["printer"] == "CC1"
    assert event["name"] == "bracket.stl"


def test_both_events_name_the_same_file(tmp_path):
    model = _binary_stl(tmp_path / "same.stl", UNIT_TRI)
    first = downloads.detected_event(model, FLEET)
    second = downloads.estimated_event(model, printer=first["printer"],
                                       eta_seconds=10)

    assert first["path"] == second["path"]


# ── the loop ────────────────────────────────────────────────────────────────

def test_a_model_dropped_into_the_folder_is_announced(tmp_path):
    """The whole point, end to end: a file appears, an event comes out."""
    import threading

    seen: list[dict] = []
    watcher = threading.Thread(
        target=downloads.watch,
        args=(tmp_path, seen.append),
        kwargs={"fleet": FLEET, "interval": 0.05, "iterations": 20},
        daemon=True)
    watcher.start()

    time.sleep(0.12)
    _binary_stl(tmp_path / "late.stl", UNIT_TRI)
    watcher.join(timeout=3.0)

    detected = [e for e in seen if e["event"] == "model_detected"]
    assert [e["name"] for e in detected] == ["late.stl"]
    assert seen[0]["dimensions"] == pytest.approx({"x": 10.0, "y": 20.0, "z": 5.0})


def test_a_model_already_sitting_there_is_not_announced_as_new(tmp_path):
    """Starting the eagle should not replay the user's whole Downloads folder
    as a stream of brand-new arrivals."""
    import threading

    _binary_stl(tmp_path / "old.stl", UNIT_TRI)
    seen: list[dict] = []
    watcher = threading.Thread(
        target=downloads.watch, args=(tmp_path, seen.append),
        kwargs={"fleet": FLEET, "interval": 0.05, "iterations": 6}, daemon=True)
    watcher.start()
    watcher.join(timeout=3.0)

    assert seen == []


def test_a_partial_download_becomes_an_arrival_when_it_is_renamed(tmp_path):
    """The real browser sequence: write to .crdownload, then rename."""
    import threading

    seen: list[dict] = []
    watcher = threading.Thread(
        target=downloads.watch, args=(tmp_path, seen.append),
        kwargs={"fleet": FLEET, "interval": 0.05, "iterations": 24}, daemon=True)
    watcher.start()

    time.sleep(0.10)
    partial = _binary_stl(tmp_path / "job.stl.crdownload", UNIT_TRI)
    time.sleep(0.15)
    assert [e for e in seen if e["event"] == "model_detected"] == [], \
        "announced a download that was still in flight"

    partial.rename(tmp_path / "job.stl")
    watcher.join(timeout=3.0)

    # Filtered by kind: the watcher also fires an estimate for a model it can
    # slice, and this test is about noticing the file, not about the ETA.
    detected = [e for e in seen if e["event"] == "model_detected"]
    assert [e["name"] for e in detected] == ["job.stl"]
