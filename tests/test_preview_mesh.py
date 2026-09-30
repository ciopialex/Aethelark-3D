"""Getting a model's shape onto a 220px card, through a 16KB pipe.

The Glance card shows the part turning on a turntable, so it needs geometry.
A real printable part is tens of thousands of triangles, which is megabytes as
JSON floats.

So the module sends a preview, not the model: the mesh reduced by vertex
clustering to a few thousand triangles, normalised into a unit cube and
quantised to int16, base64'd. It travels under a key beginning with an
underscore, which is how the module bus knows to keep it for the island and
strip it from what the voice model hears — the mesh is useless to the model
and expensive in its context.

The numbers the user actually reads (the bounding box) are measured from the
*whole* model before any of this, so decimation never changes them.
"""
from __future__ import annotations

import base64
import struct
from pathlib import Path

import pytest

from aethelark3d import downloads


def _stl(path: Path, triangles) -> Path:
    with path.open("wb") as fh:
        fh.write(b"\0" * 80)
        fh.write(struct.pack("<I", len(triangles)))
        for facet in triangles:
            fh.write(struct.pack("<3f", 0.0, 0.0, 1.0))
            for vertex in facet:
                fh.write(struct.pack("<3f", *vertex))
            fh.write(struct.pack("<H", 0))
    return path


def _cube(size: float = 40.0):
    """Twelve triangles, the corners of a box — enough to have a silhouette."""
    s = size
    pts = [(0, 0, 0), (s, 0, 0), (s, s, 0), (0, s, 0),
           (0, 0, s), (s, 0, s), (s, s, s), (0, s, s)]
    faces = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7),
             (0, 1, 5), (0, 5, 4), (2, 3, 7), (2, 7, 6),
             (1, 2, 6), (1, 6, 5), (0, 4, 7), (0, 7, 3)]
    return [tuple(tuple(float(c) for c in pts[i]) for i in f) for f in faces]


def _decode(preview: dict) -> list[float]:
    raw = base64.b64decode(preview["vertices"])
    count = len(raw) // 2
    return list(struct.unpack(f"<{count}h", raw))


# ── it fits ─────────────────────────────────────────────────────────────────

def test_the_mesh_never_reaches_the_model(tmp_path):
    """The constraint that decided the whole design, corrected.

    This used to assert the whole payload fitted the bus's 16000-character
    ceiling, and that was true of the wrong thing. The bus clips what the MODEL
    hears -- `model_view(parsed)`, which has already dropped every key starting
    with an underscore -- while the island is handed `result=parsed` whole and
    uncapped. A mesh under an underscored key is never measured against that
    ceiling at all.

    So the contract this module owes is not a size. It is the underscore: every
    key carrying geometry must be one the bus will strip, or a few thousand
    quantised triangles land in a voice model's context, where they cost real
    money and can push the fields it actually needs off the end.

    `model_view`'s rule is reproduced here rather than imported because it lives
    in the other repository. That is the point of the test: this side has to
    honour a rule it cannot see.
    """
    import json
    big = [((float(i), 0.0, 0.0), (float(i) + 1, 0.0, 0.0),
            (float(i), 1.0, float(i % 7))) for i in range(4000)]
    model = _stl(tmp_path / "big.stl", big)

    event = downloads.detected_event(model, {"CC1": {"host": "1.2.3.4"}})

    def model_view(value):
        """core/module_bus/bus.py — `_`-prefixed keys removed, at any depth."""
        if isinstance(value, dict):
            return {k: model_view(v) for k, v in value.items()
                    if not (isinstance(k, str) and k.startswith("_"))}
        if isinstance(value, list):
            return [model_view(v) for v in value]
        return value

    heard = json.dumps(model_view(event))
    assert "vertices" not in heard, (
        "the model was sent quantised triangles it cannot read")
    assert len(heard) < 2000, (
        f"what the model hears is {len(heard)} chars; the mesh is leaking "
        f"through a key the bus does not strip")


def test_the_island_still_gets_the_mesh(tmp_path):
    """The other half. Stripping it from the model must not lose it."""
    model = _stl(tmp_path / "cube.stl", _cube())
    event = downloads.detected_event(model, {"CC1": {"host": "1.2.3.4"}})

    assert event["_preview"] is not None
    assert event["_preview"]["vertices"]


def test_a_small_part_is_sent_whole(tmp_path):
    """Decimating twelve triangles would be losing detail to save nothing."""
    preview = downloads.preview_mesh(_stl(tmp_path / "cube.stl", _cube()))
    assert preview["triangles"] == 12


def test_a_big_part_is_reduced_to_the_budget(tmp_path):
    """At most the budget, not exactly it. Bucketing by position returns one
    triangle per occupied cell, and a part that occupies few cells — a rod, a
    sheet — legitimately previews with fewer. Padding it back up to 300 would
    mean sending duplicates to hit a number."""
    big = [((float(i), 0.0, 0.0), (float(i) + 1, 0.0, 0.0),
            (float(i), 1.0, 2.0)) for i in range(4000)]
    preview = downloads.preview_mesh(_stl(tmp_path / "big.stl", big),
                                     max_triangles=300)

    assert 0 < preview["triangles"] <= 300
    assert preview["triangles"] < 4000, "nothing was reduced"


# ── it is still the same shape ──────────────────────────────────────────────

def test_the_preview_is_normalised_into_a_unit_cube(tmp_path):
    """Sent as shape, not size: the card draws it at 220px and reads the real
    millimetres from the telemetry beside it."""
    preview = downloads.preview_mesh(_stl(tmp_path / "cube.stl", _cube(40.0)))
    values = _decode(preview)

    assert max(values) <= 32767 and min(values) >= -32768
    # a cube fills its own bounding box, so it should reach both extremes
    assert max(values) > 32000
    assert min(values) < -32000


def test_the_same_shape_at_two_scales_previews_identically(tmp_path):
    """Normalisation means a 4mm part and a 400mm part of the same design
    produce the same picture, which is what a shape preview is for."""
    small = downloads.preview_mesh(_stl(tmp_path / "s.stl", _cube(4.0)))
    large = downloads.preview_mesh(_stl(tmp_path / "l.stl", _cube(400.0)))

    assert _decode(small) == _decode(large)


def test_the_vertex_count_matches_the_triangle_count(tmp_path):
    preview = downloads.preview_mesh(_stl(tmp_path / "cube.stl", _cube()))
    assert len(_decode(preview)) == preview["triangles"] * 9


def test_a_flat_part_does_not_divide_by_its_own_zero_thickness(tmp_path):
    """A sheet has no extent in Z. Normalising by a zero range is a crash on a
    perfectly ordinary model."""
    flat = [((0.0, 0.0, 0.0), (10.0, 0.0, 0.0), (10.0, 10.0, 0.0)),
            ((0.0, 0.0, 0.0), (10.0, 10.0, 0.0), (0.0, 10.0, 0.0))]
    preview = downloads.preview_mesh(_stl(tmp_path / "flat.stl", flat))

    assert preview["triangles"] == 2
    assert all(isinstance(v, int) for v in _decode(preview))


# ── what it is not ──────────────────────────────────────────────────────────

def test_a_format_we_cannot_read_previews_as_nothing(tmp_path):
    step = tmp_path / "part.step"
    step.write_text("ISO-10303-21;\n")
    assert downloads.preview_mesh(step) is None


def test_a_truncated_file_does_not_take_the_card_down(tmp_path):
    broken = tmp_path / "half.stl"
    broken.write_bytes(b"\0" * 80 + struct.pack("<I", 9000))
    assert downloads.preview_mesh(broken) is None


def test_decimation_never_changes_the_reported_dimensions(tmp_path):
    """The bounding box is measured from the whole model. If the preview drove
    it, a decimated card would quietly report a smaller part than the one
    about to be printed."""
    big = [((float(i) * 0.1, 0.0, 0.0), (float(i) * 0.1 + 1, 0.0, 0.0),
            (float(i) * 0.1, 50.0, 7.0)) for i in range(3000)]
    model = _stl(tmp_path / "big.stl", big)

    measured = downloads.measure(model)
    downloads.preview_mesh(model, max_triangles=50)

    assert downloads.measure(model) == measured
    assert measured["y"] == pytest.approx(50.0)


# ── it reaches the card ─────────────────────────────────────────────────────

def test_the_detection_event_carries_the_preview(tmp_path):
    model = _stl(tmp_path / "cube.stl", _cube())
    event = downloads.detected_event(model, {"CC1": {"host": "1.2.3.4"}})

    assert event["_preview"] is not None
    assert event["_preview"]["triangles"] == 12
    assert event["dimensions"]["x"] == pytest.approx(40.0)


def test_an_unpreviewable_model_still_produces_an_event(tmp_path):
    step = tmp_path / "part.step"
    step.write_text("ISO-10303-21;\n")
    event = downloads.detected_event(step, {"CC1": {"host": "1.2.3.4"}})

    assert event["event"] == "model_detected"
    assert event["_preview"] is None


# ── the preview still has to look like the part ─────────────────────────────

def test_the_preview_still_spans_the_whole_part(tmp_path):
    """Keeping the largest triangles preserves area, not form.

    On the real cartridge it kept the big outer faces and threw away the
    structure between them, so the turntable drew a flat sliver — measured at
    a z-extent of a few percent of the model's. A preview has to occupy the
    same box the part does, or it is a picture of something else.
    """
    # A tall part whose large faces are its two flat ends and whose form lives
    # in many small triangles up the middle — the shape that broke it.
    triangles = []
    for i in range(1500):
        z = i * 0.1
        triangles.append(((0.0, 0.0, z), (1.0, 0.0, z), (0.0, 1.0, z + 0.1)))
    big = 60.0
    triangles.append(((-big, -big, 0.0), (big, -big, 0.0), (big, big, 0.0)))
    triangles.append(((-big, -big, 0.0), (big, big, 0.0), (-big, big, 0.0)))

    model = _stl(tmp_path / "tall.stl", triangles)
    full = downloads.measure(model)
    preview = downloads.preview_mesh(model, max_triangles=200)
    values = _decode(preview)

    zs = values[2::3]
    span = (max(zs) - min(zs)) / 65535.0
    full_ratio = full["z"] / max(full["x"], full["y"], full["z"])

    assert span > full_ratio * 0.6, (
        f"the preview spans {span:.2%} of the box where the part spans "
        f"{full_ratio:.2%} — the tall structure was decimated away")


def test_the_preview_keeps_triangles_from_all_over_the_part(tmp_path):
    """Not just from wherever the biggest faces happen to be."""
    triangles = []
    for i in range(900):
        x = (i % 30) * 2.0
        y = (i // 30) * 2.0
        triangles.append(((x, y, 0.0), (x + 1, y, 0.0), (x, y + 1, 3.0)))
    model = _stl(tmp_path / "field.stl", triangles)

    preview = downloads.preview_mesh(model, max_triangles=100)
    values = _decode(preview)
    xs, ys = values[0::3], values[1::3]

    assert (max(xs) - min(xs)) > 50000, "the preview clusters on one side in x"
    assert (max(ys) - min(ys)) > 50000, "the preview clusters on one side in y"
