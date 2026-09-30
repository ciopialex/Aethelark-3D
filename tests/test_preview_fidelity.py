"""The drawn preview must be the shape of the actual part.

Two defects lived here, and both passed the existing suite because the suite
asked whether a mesh came back, never whether it was the right shape.

`_cluster` buckets triangles into a spatial grid and keeps the largest from
each occupied cell, which preserves the silhouette. Its overflow path then
sorted by area and truncated — the exact "keep the biggest faces" failure its
own docstring says it exists to prevent. Measured on a 31,576-triangle
sculpture: the grid gave 2,070 cells against a 500 budget, so the area cut
fired on every real part and squashed a 103.75mm model to 17.22mm of height.

`_3mf_points` read only `3dmodel.model`. In the production extension — what
Bambu Studio, Orca and therefore MakerWorld write — that file holds component
references and zero vertices, and the geometry lives in `3D/Objects/*.model`.
A downloaded 3MF therefore measured as None and drew a placeholder cube.

The load-bearing assertion is aspect ratio: the proportions of the decimated
mesh against the proportions of the full model's bounding box. A mesh that
merely exists tells you nothing; a mesh whose aspect matches the measurement
is the same object.
"""
from __future__ import annotations

import struct
import zipfile
from pathlib import Path

import pytest

from aethelark3d.downloads import (
    _3mf_points, _mesh_points, _cluster, measure, preview_mesh,
)

MODELS = Path.home() / "3D_Prints"


def _spans(triangles):
    xs = [v[0] for t in triangles for v in t]
    ys = [v[1] for t in triangles for v in t]
    zs = [v[2] for t in triangles for v in t]
    return (max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs))


def _tall_tower():
    """A thick base slab and a tall spire, shaped to reproduce the real defect.

    Getting this right took several attempts, and the failures are instructive:
    the truncation sorts the GRID REPRESENTATIVES, not the raw triangles, so a
    base that is merely dense is not enough — a thin disc collapses into one
    z-layer of the 22x22x22 grid, contributes at most ~380 cells, and the
    remaining representatives still span the height. The base must occupy more
    than `budget` cells by itself.

    Measured with these constants: 1,138 occupied cells against a budget of
    500 (so the overflow path runs), and the buggy area-sort keeps 20.0mm of
    90.0mm — 22%, the same shape as the real failure, where a 31,576-triangle
    sculpture went from 103.75mm tall to 17.22mm.
    """
    import math
    disc_n, disc_r, disc_h = 2400, 16.0, 20.0
    spire_n, spire_r, height = 1500, 7.0, 90.0
    tris = []
    for i in range(disc_n):
        a0 = i * (2 * math.pi / disc_n)
        a1 = (i + 1) * (2 * math.pi / disc_n)
        z = disc_h * (i % 5) / 5.0
        for r_in, r_out in ((0.0, disc_r * 0.5), (disc_r * 0.5, disc_r)):
            tris.append(((r_in * math.cos(a0), r_in * math.sin(a0), z),
                         (r_out * math.cos(a0), r_out * math.sin(a0), z),
                         (r_out * math.cos(a1), r_out * math.sin(a1), z + disc_h / 5)))
    for i in range(spire_n):
        z0 = disc_h + i * (height - disc_h) / spire_n
        z1 = disc_h + (i + 1) * (height - disc_h) / spire_n
        a0, a1 = i * 0.31, (i + 1) * 0.31
        tris.append(((spire_r * math.cos(a0), spire_r * math.sin(a0), z0),
                     (spire_r * math.cos(a1), spire_r * math.sin(a1), z0),
                     (spire_r * math.cos(a1), spire_r * math.sin(a1), z1)))
    return tris


def test_decimation_keeps_the_tall_axis_of_a_tapered_tower():
    """The synthetic case that isolates the defect from any real file."""
    tris = _tall_tower()
    full = _spans(tris)
    kept = _cluster(tris, 500)
    got = _spans(kept)
    assert len(kept) <= 500
    assert got[2] >= full[2] * 0.9, (
        f"height collapsed from {full[2]:.1f} to {got[2]:.1f}: the decimator "
        f"is selecting by area again")


def test_decimation_never_returns_more_than_the_budget():
    assert len(_cluster(_tall_tower(), 50)) <= 50
    assert len(_cluster(_tall_tower(), 3)) <= 3


def test_the_synthetic_case_actually_overflows_the_grid():
    """Guards the guard: if this stops overflowing, the tests above go blind.

    The area-sort truncation only runs when occupied cells exceed the budget.
    A helper that quietly stops meeting that condition would make every
    decimation test below pass against broken code.
    """
    tris = _tall_tower()
    xs = [v[0] for t in tris for v in t]
    ys = [v[1] for t in tris for v in t]
    zs = [v[2] for t in tris for v in t]
    lo = (min(xs), min(ys), min(zs))
    hi = (max(xs), max(ys), max(zs))
    ext = [(hi[i] - lo[i]) or 1.0 for i in range(3)]
    side = 22                       # int(round(500 ** 0.5))
    cells = set()
    for t in tris:
        c = [sum(v[a] for v in t) / 3.0 for a in range(3)]
        cells.add(tuple(min(side - 1, int((c[a] - lo[a]) / ext[a] * side))
                        for a in range(3)))
    assert len(cells) > 500, (
        f"only {len(cells)} cells occupied at side {side}; the overflow path "
        f"the decimation tests exercise is no longer reached")


@pytest.mark.parametrize("budget,floor", [(500, 0.95), (200, 0.90), (60, 0.70)])
def test_decimation_degrades_gracefully_as_the_budget_shrinks(budget, floor):
    """Shape survival should track the budget, not fall off a cliff.

    Measured on the tapered tower: 99.5% of the height at the production
    budget of 500, and 88% at 60, where the grid is coarse enough that the
    topmost cell merges with its neighbour. The thresholds are what each
    budget can actually deliver — the defect being guarded against collapsed
    this to 17% regardless of budget.
    """
    tris = _tall_tower()
    full_z = _spans(tris)[2]
    got = _spans(_cluster(tris, budget))[2]
    assert got >= full_z * floor, (
        f"budget {budget}: height {got:.1f} of {full_z:.1f} "
        f"({got / full_z:.0%}, floor {floor:.0%})")


@pytest.mark.parametrize("name", [
    "Modern_Eagle_Sculpture.stl",
    "Oakley_Glasses_Mount_v1.stl",
    "Pan_Tilt_Gimbal_03_nema17_outer_cradle.stl",
])
def test_drawn_aspect_matches_measured_aspect_on_real_models(name):
    """The regression guard for the whole preview path.

    preview_mesh normalises to a cube of side 2 using ONE scale for all axes,
    so relative proportions must survive. If they do not, the decimator threw
    away a dimension.
    """
    path = MODELS / name
    if not path.is_file():
        pytest.skip(f"{name} not present on this machine")

    dims = measure(path)
    preview = preview_mesh(path)
    assert dims and preview, "a readable model produced no measurement or mesh"

    decoded = []
    blob = __import__("base64").b64decode(preview["vertices"])
    vals = struct.unpack(f"<{len(blob) // 2}h", blob)
    for i in range(0, len(vals) - 8, 9):
        decoded.append(tuple(
            tuple(vals[i + k * 3 + a] / 32767 for a in range(3)) for k in range(3)))

    mesh = _spans(decoded)
    real = (dims["x"], dims["y"], dims["z"])
    longest = max(range(3), key=lambda i: real[i])
    shortest = min(range(3), key=lambda i: real[i])
    assert longest == max(range(3), key=lambda i: mesh[i]), (
        f"the drawn mesh's longest axis is not the model's longest axis: "
        f"mesh {tuple(round(m, 3) for m in mesh)} vs real {real}")
    ratio_real = real[longest] / real[shortest]
    ratio_mesh = mesh[longest] / mesh[shortest]
    assert ratio_mesh == pytest.approx(ratio_real, rel=0.15), (
        f"aspect {ratio_mesh:.3f} does not match measured {ratio_real:.3f}")


def _write_production_3mf(path: Path):
    """A 3MF in the layout MakerWorld actually serves."""
    root = ('<?xml version="1.0"?><model unit="millimeter" '
            'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
            '<resources><object id="2" type="model"><components>'
            '<component p:path="/3D/Objects/object_1.model" objectid="1" '
            'xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06"/>'
            '</components></object></resources>'
            '<build><item objectid="2" transform="1 0 0 0 1 0 0 0 1 128 128 5"/>'
            '</build></model>')
    obj = ('<?xml version="1.0"?><model unit="millimeter" '
           'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
           '<resources><object id="1" type="model"><mesh><vertices>'
           '<vertex x="0" y="0" z="0"/><vertex x="30" y="0" z="0"/>'
           '<vertex x="0" y="20" z="0"/><vertex x="0" y="0" z="10"/>'
           '</vertices><triangles>'
           '<triangle v1="0" v2="1" v3="2"/><triangle v1="0" v2="1" v3="3"/>'
           '</triangles></mesh></object></resources><build/></model>')
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("3D/3dmodel.model", root)
        z.writestr("3D/Objects/object_1.model", obj)


def test_a_production_extension_3mf_is_measurable(tmp_path):
    """Reading only 3dmodel.model finds zero vertices in this layout."""
    f = tmp_path / "part.3mf"
    _write_production_3mf(f)
    # Two triangles, three corners each. This asserted 4 — the vertex count —
    # back when the reader yielded vertices and let the caller chunk them into
    # triples, which is the defect below.
    assert len(list(_3mf_points(f))) == 6
    assert measure(f) == {"x": 30.0, "y": 20.0, "z": 10.0}


def test_the_triangles_are_read_and_not_guessed(tmp_path):
    """3MF is INDEXED, and the reader used to ignore the index.

    `<vertices>` holds positions; `<triangles>` holds v1/v2/v3 into that list.
    Consecutive vertices have no relationship to one another, so yielding the
    vertex stream and chunking it into triples fabricates a triangle out of
    every three unrelated points.

    Measured 2026-09-08 on a downloaded Urban Spiderman:

        the file        18,449 vertices   36,952 triangles
        what was read    6,149 triangles  0.0% sharing a corner

    6,149 x 3 = 18,447: the reader was spending the vertex list and calling it
    a mesh. `measure` never noticed, because a bounding box does not care how
    points are joined — which is exactly why this lived so long. The card's
    numbers were right and only the picture was wrong, so it was reported as
    the renderer being broken.

    The fixture's two triangles share the edge 0-1 on purpose: a reader that
    chunks cannot produce a shared corner, and a reader that resolves indices
    cannot avoid one.
    """
    f = tmp_path / "part.3mf"
    _write_production_3mf(f)

    pts = list(_3mf_points(f))
    tris = [tuple(pts[i:i + 3]) for i in range(0, len(pts), 3)]

    assert len(tris) == 2, f"expected the file's 2 triangles, got {len(tris)}"

    # The fixture's <build><item> places it at (128, 128, 5), so the vertices
    # come back moved by exactly that. Asserting the raw coordinates was
    # asserting that placement is ignored, which is the other defect this file
    # now guards against.
    dx, dy, dz = 128.0, 128.0, 5.0
    assert tris[0] == ((dx, dy, dz), (30.0 + dx, dy, dz), (dx, 20.0 + dy, dz))
    assert tris[1] == ((dx, dy, dz), (30.0 + dx, dy, dz), (dx, dy, 10.0 + dz))

    corners = [v for t in tris for v in t]
    assert len(set(corners)) == 4, (
        "the two triangles no longer share the edge they are declared to "
        "share; the index is being ignored again")


def test_a_mesh_with_no_readable_faces_is_still_measurable(tmp_path):
    """A shape we cannot draw must not become a size we cannot report.

    `measure` is the only caller that can work from loose vertices, and it is
    the one the user actually reads — the bounding box goes on the card. So a
    mesh whose <triangles> are missing or unusable falls back to yielding its
    vertices: the picture will be wrong, which is honest, and the number stays
    right, which is what matters.
    """
    obj = ('<?xml version="1.0"?><model unit="millimeter" '
           'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
           '<resources><object id="1" type="model"><mesh><vertices>'
           '<vertex x="0" y="0" z="0"/><vertex x="30" y="0" z="0"/>'
           '<vertex x="0" y="20" z="0"/><vertex x="0" y="0" z="10"/>'
           '</vertices><triangles/></mesh></object></resources>'
           '<build/></model>')
    f = tmp_path / "faceless.3mf"
    with zipfile.ZipFile(f, "w") as z:
        z.writestr("3D/3dmodel.model", obj)

    assert len(list(_3mf_points(f))) == 4
    assert measure(f) == {"x": 30.0, "y": 20.0, "z": 10.0}


def test_a_core_spec_3mf_still_works(tmp_path):
    """Geometry directly in 3dmodel.model must not regress."""
    f = tmp_path / "core.3mf"
    body = ('<?xml version="1.0"?><model unit="millimeter" '
            'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
            '<resources><object id="1" type="model"><mesh><vertices>'
            '<vertex x="0" y="0" z="0"/><vertex x="5" y="0" z="0"/>'
            '<vertex x="0" y="7" z="0"/></vertices><triangles>'
            '<triangle v1="0" v2="1" v3="2"/></triangles></mesh></object>'
            '</resources><build/></model>')
    with zipfile.ZipFile(f, "w") as z:
        z.writestr("3D/3dmodel.model", body)
    assert measure(f) == {"x": 5.0, "y": 7.0, "z": 0.0}


def test_an_unreadable_bundle_measures_as_none_not_zero(tmp_path):
    """None is 'not measurable'. Zeros would be a number nothing produced."""
    f = tmp_path / "junk.3mf"
    with zipfile.ZipFile(f, "w") as z:
        z.writestr("Metadata/notes.txt", "no geometry here")
    assert measure(f) is None
    assert preview_mesh(f) is None


# ── the parts go where the file says they go ────────────────────────────────

def _write_two_object_3mf(path: Path):
    """Two identical cubes, placed a metre apart by their build items.

    The distance is the whole point: unplaced, both land on the origin and the
    bounding box is one cube. Placed, it is a metre wide.
    """
    cube = ('<mesh><vertices>'
            '<vertex x="0" y="0" z="0"/><vertex x="10" y="0" z="0"/>'
            '<vertex x="0" y="10" z="0"/><vertex x="0" y="0" z="10"/>'
            '</vertices><triangles>'
            '<triangle v1="0" v2="1" v3="2"/><triangle v1="0" v2="1" v3="3"/>'
            '<triangle v1="0" v2="2" v3="3"/><triangle v1="1" v2="2" v3="3"/>'
            '</triangles></mesh>')
    leaf = ('<?xml version="1.0"?><model unit="millimeter" '
            'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
            f'<resources><object id="1" type="model">{cube}</object></resources>'
            '<build/></model>')
    root = ('<?xml version="1.0"?><model unit="millimeter" '
            'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" '
            'xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06">'
            '<resources>'
            '<object id="2" type="model"><components>'
            '<component objectid="1" p:path="/3D/Objects/a.model"/>'
            '</components></object>'
            '<object id="3" type="model"><components>'
            '<component objectid="1" p:path="/3D/Objects/b.model"/>'
            '</components></object>'
            '</resources><build>'
            '<item objectid="2" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>'
            '<item objectid="3" transform="1 0 0 0 1 0 0 0 1 1000 0 0"/>'
            '</build></model>')
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("3D/3dmodel.model", root)
        z.writestr("3D/Objects/a.model", leaf)
        z.writestr("3D/Objects/b.model", leaf)
    return path


def test_a_multi_object_model_is_not_stacked_on_the_origin(tmp_path):
    """The defect, in the smallest file that shows it.

    `transform` was ignored on the reasoning that these are translations onto a
    plate, and a translation cannot change a shape the preview re-centres
    anyway. That holds for exactly one object. With two, ignoring it puts both
    in the same place.

    Measured 2026-09-09 on a downloaded Kaws Flayed Grey: sixteen <build><item>
    entries, 496,536 triangles, every part landing on the origin. The turntable
    drew a crumpled lump and `measure` reported the bounding box of the pile,
    which is still a bounding box and so looked like a number.
    """
    f = _write_two_object_3mf(tmp_path / "two.3mf")
    dims = measure(f)
    assert dims is not None
    assert dims["x"] == pytest.approx(1010.0), (
        f"x spans {dims['x']} — the second cube is a metre away and the file "
        f"says so; a span of 10 means both were read onto the origin")
    assert dims["y"] == pytest.approx(10.0)


def test_a_rotation_in_a_transform_is_applied(tmp_path):
    """Not just translations. The real files carry rotation matrices.

    The comment that justified ignoring transforms called them translations.
    Half the Kaws items are rotations, and a rotation changes the shape rather
    than merely the position — so a preview that skips it is a picture of a
    different object.
    """
    leaf = ('<?xml version="1.0"?><model unit="millimeter" '
            'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
            '<resources><object id="1" type="model"><mesh><vertices>'
            '<vertex x="0" y="0" z="0"/><vertex x="30" y="0" z="0"/>'
            '<vertex x="0" y="10" z="0"/><vertex x="0" y="0" z="10"/>'
            '</vertices><triangles>'
            '<triangle v1="0" v2="1" v3="2"/><triangle v1="0" v2="1" v3="3"/>'
            '</triangles></mesh></object></resources>'
            # 90 degrees about Z: the 30mm run along x becomes 30mm along y.
            '<build><item objectid="1" transform="0 1 0 -1 0 0 0 0 1 0 0 0"/>'
            '</build></model>')
    f = tmp_path / "rot.3mf"
    with zipfile.ZipFile(f, "w") as z:
        z.writestr("3D/3dmodel.model", leaf)

    dims = measure(f)
    assert dims["y"] == pytest.approx(30.0), (
        f"the 30mm axis is still x ({dims}); the rotation was dropped")
    assert dims["x"] == pytest.approx(10.0)


# ── a slicer file is a print job, not a model ───────────────────────────────

def _write_two_plate_3mf(path: Path):
    """Two objects, one per plate, laid out side by side as a slicer does."""
    leaf = ('<?xml version="1.0"?><model unit="millimeter" '
            'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
            '<resources><object id="1" type="model"><mesh><vertices>'
            '<vertex x="0" y="0" z="0"/><vertex x="10" y="0" z="0"/>'
            '<vertex x="0" y="10" z="0"/><vertex x="0" y="0" z="10"/>'
            '</vertices><triangles>'
            '<triangle v1="0" v2="1" v3="2"/><triangle v1="0" v2="1" v3="3"/>'
            '</triangles></mesh></object></resources><build/></model>')
    root = ('<?xml version="1.0"?><model unit="millimeter" '
            'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" '
            'xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06">'
            '<resources>'
            '<object id="2" type="model"><components>'
            '<component objectid="1" p:path="/3D/Objects/a.model"/>'
            '</components></object>'
            '<object id="4" type="model"><components>'
            '<component objectid="1" p:path="/3D/Objects/b.model"/>'
            '</components></object>'
            '</resources><build>'
            '<item objectid="2" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>'
            '<item objectid="4" transform="1 0 0 0 1 0 0 0 1 900 0 0"/>'
            '</build></model>')
    settings = ('<?xml version="1.0"?><config>'
                '<object id="2"><metadata key="name" value="first.stl"/></object>'
                '<object id="4"><metadata key="name" value="second.stl"/></object>'
                '</config>')
    slice_info = ('<?xml version="1.0"?><config>'
                  '<plate><metadata key="index" value="1"/>'
                  '<object identify_id="10" name="first.stl"/></plate>'
                  '<plate><metadata key="index" value="2"/>'
                  '<object identify_id="20" name="second.stl"/></plate>'
                  '</config>')
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("3D/3dmodel.model", root)
        z.writestr("3D/Objects/a.model", leaf)
        z.writestr("3D/Objects/b.model", leaf)
        z.writestr("Metadata/model_settings.config", settings)
        z.writestr("Metadata/slice_info.config", slice_info)
    return path


def test_only_the_first_plate_is_previewed(tmp_path):
    """A slicer 3MF is a print JOB and every plate shares one coordinate space.

    Bambu and Orca lay plates out side by side, so reading all of them gives a
    mesh as wide as the whole job. Measured on a Kaws Flayed Grey: six plates,
    sixteen parts, 799mm across, every part too small on a 220px card to be
    anything but debris — while the file's own Metadata/plate_1.png shows one
    plate with four recognisable pieces.

    One plate is 10mm wide here and the job is 910mm, so the two answers cannot
    be confused for one another.
    """
    f = _write_two_plate_3mf(tmp_path / "job.3mf")
    dims = measure(f)

    assert dims["x"] == pytest.approx(10.0), (
        f"x spans {dims['x']} — that is the whole job, not a plate; the second "
        f"plate is 900mm away and should not be in the preview")


def test_a_file_with_no_plate_metadata_is_read_whole(tmp_path):
    """Every plain 3MF, and the fallback that keeps them working.

    Plate data is a slicer extension. A model exported by anything else
    declares none, and dropping its geometry because it did not would be a far
    worse failure than the one being fixed.
    """
    f = _write_two_object_3mf(tmp_path / "plain.3mf")     # no .config parts
    dims = measure(f)
    assert dims["x"] == pytest.approx(1010.0), (
        "a file with no plate metadata lost geometry it declared")


# ── a malformed file must not become a wrong one ────────────────────────────

def test_an_unparseable_vertex_does_not_renumber_the_mesh():
    """A vertex position is an INDEX, and skipping one shifts all the rest.

    `<triangle v1 v2 v3>` names vertices by position, so dropping a vertex that
    failed to parse renumbers every vertex after it and silently re-points
    every triangle that referenced them. The mesh still loads, still measures,
    and is quietly the wrong shape — the failure mode this whole file exists to
    catch.

    FIVE vertices, deliberately. With three, dropping one makes the triangle
    index out of range, the no-faces fallback yields the raw vertices, and
    chunking them into triples rebuilds the same corners — so the bug and the
    fix are indistinguishable. With five, the shift produces a triangle that is
    still VALID and made of the wrong points, which is the dangerous case and
    the one worth pinning.
    """
    ns = 'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"'
    doc = (
        f'<model {ns}><resources><object id="1" type="model"><mesh><vertices>'
        '<vertex x="bad" y="bad" z="bad"/>'      # 0, unparseable
        '<vertex x="10" y="0" z="0"/>'           # 1  A
        '<vertex x="0" y="20" z="0"/>'           # 2  B
        '<vertex x="0" y="0" z="30"/>'           # 3  C
        '<vertex x="40" y="40" z="40"/>'         # 4  D
        '</vertices><triangles>'
        '<triangle v1="1" v2="2" v3="3"/>'       # means A, B, C
        '</triangles></mesh></object></resources>'
        '<build><item objectid="1"/></build></model>')
    import tempfile
    f = Path(tempfile.mkdtemp()) / "bad_vertex.3mf"
    with zipfile.ZipFile(f, "w") as z:
        z.writestr("3D/3dmodel.model", doc)

    tris = _mesh_points(f)
    assert tris, "the file has one readable triangle and produced none"
    corners = {v for t in tris for v in t}

    A, D = (10.0, 0.0, 0.0), (40.0, 40.0, 40.0)
    assert A in corners, (
        "vertex 1 is missing: the bad vertex was dropped, so indices 1/2/3 "
        "now name B, C and D — a valid triangle built from the wrong points")
    assert D not in corners, (
        "vertex 4 appears in a triangle that never referenced it; the mesh "
        "was renumbered by the unparseable vertex")
