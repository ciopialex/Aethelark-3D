"""Notice a 3D model the moment the browser finishes writing it.

A browser downloads to a temporary name — `.crdownload` on Chrome, `.part` on
Firefox, `.download` on Safari — and renames it into place when the transfer
completes. That rename is the completion signal and it is atomic within a
filesystem, so a file sitting under its final name with a size that has stopped
moving is a finished file.

Detection is a poll rather than a kernel subscription. inotify, FSEvents and
ReadDirectoryChangesW would each shave the remaining quarter-second, at the
cost of three OS-specific code paths or a new dependency, to speed up an event
a human already perceives as instantaneous. If that quarter second ever
matters, `settled_models` is the seam to put a native watcher behind.

Two events, because the facts arrive at different times. A bounding box is
arithmetic over vertices already on disk. An ETA requires actually slicing.
Holding the first until the second is ready means showing nothing while the
answer is already known.
"""
from __future__ import annotations

import base64
import math
import struct
import time
import zipfile
from pathlib import Path
import threading
from typing import Any, Callable, Iterable

#: What counts as a model worth reacting to.
MODEL_SUFFIXES = {".stl", ".3mf", ".step", ".stp", ".gcode"}

#: What a browser calls a file it has not finished writing.
PARTIAL_SUFFIXES = {".crdownload", ".part", ".download", ".tmp", ".partial"}

#: Binary STL: 80-byte header, uint32 facet count, 50 bytes per facet.
_STL_HEADER = 80
_STL_FACET = 50


def _bbox(points: Iterable[tuple[float, float, float]]) -> dict[str, float] | None:
    """The span of a point cloud on each axis.

    Span, not maximum: a part modelled around the origin lives on both sides of
    it, and max alone reports half of it.
    """
    lo = [float("inf")] * 3
    hi = [float("-inf")] * 3
    seen = False
    for point in points:
        seen = True
        for axis in range(3):
            value = point[axis]
            if value < lo[axis]:
                lo[axis] = value
            if value > hi[axis]:
                hi[axis] = value
    if not seen:
        return None
    return {"x": round(hi[0] - lo[0], 3),
            "y": round(hi[1] - lo[1], 3),
            "z": round(hi[2] - lo[2], 3)}


def _binary_stl_points(data: bytes):
    count = struct.unpack_from("<I", data, _STL_HEADER)[0]
    expected = _STL_HEADER + 4 + count * _STL_FACET
    if count == 0 or len(data) < expected:
        raise ValueError("truncated binary STL")
    offset = _STL_HEADER + 4
    for _ in range(count):
        for vertex in range(3):
            at = offset + 12 + vertex * 12
            yield struct.unpack_from("<3f", data, at)
        offset += _STL_FACET


def _ascii_stl_points(text: str):
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("vertex"):
            continue
        parts = line.split()
        if len(parts) >= 4:
            yield (float(parts[1]), float(parts[2]), float(parts[3]))


#: 3MF transforms are twelve numbers: a 3x3 basis followed by a translation,
#: applied to a ROW vector. p' = (x, y, z, 1) x M, with M four rows of three.
_IDENTITY = (1.0, 0.0, 0.0,  0.0, 1.0, 0.0,  0.0, 0.0, 1.0,  0.0, 0.0, 0.0)


def _parse_transform(raw: str | None) -> tuple[float, ...]:
    """A 3MF `transform` attribute, or identity if it is absent or malformed."""
    if not raw:
        return _IDENTITY
    try:
        nums = tuple(float(v) for v in raw.split())
    except ValueError:
        return _IDENTITY
    return nums if len(nums) == 12 else _IDENTITY


def _compose(child: tuple[float, ...],
             parent: tuple[float, ...]) -> tuple[float, ...]:
    """The child's transform followed by the parent's.

    Row-vector convention, so p x child x parent -- the order matters and
    reversing it puts every part in the wrong place while still looking like a
    plausible arrangement.
    """
    c, q = child, parent
    out = []
    for row in range(4):
        cx = c[row * 3] if row < 3 else c[9]
        cy = c[row * 3 + 1] if row < 3 else c[10]
        cz = c[row * 3 + 2] if row < 3 else c[11]
        for col in range(3):
            v = cx * q[col] + cy * q[3 + col] + cz * q[6 + col]
            if row == 3:
                v += q[9 + col]
            out.append(v)
    return tuple(out)


def _apply(m: tuple[float, ...], x: float, y: float, z: float):
    return (x * m[0] + y * m[3] + z * m[6] + m[9],
            x * m[1] + y * m[4] + z * m[7] + m[10],
            x * m[2] + y * m[5] + z * m[8] + m[11])


def _3mf_plates(blobs: dict[str, bytes]) -> list[list[str]]:
    """Which build objects belong to which plate, in plate order.

    A slicer 3MF is a print JOB, not a model. Bambu and Orca lay every plate
    out side by side in ONE coordinate space, so reading all of them gives a
    mesh as wide as the whole job -- measured on a Kaws Flayed Grey, six plates
    and 799mm, with each part rendered too small to recognise. The creator's
    own thumbnail in that same file (Metadata/plate_1.png) shows one plate.

    `Metadata/slice_info.config` lists each plate's objects by name and
    `Metadata/model_settings.config` maps an object id to its name, so the two
    together give plate -> ids. Matched on name because `identify_id` in the
    first and `id` in the second are different numberings; a name repeated
    across plates would put that part on both, which over-includes rather than
    dropping geometry.

    Returns [] when the file declares no plates, which is every plain 3MF and
    is the case the caller treats as "one plate, everything on it".
    """
    import xml.etree.ElementTree as ET

    def _part(name: str) -> bytes | None:
        for key, blob in blobs.items():
            if key.lower().endswith(name):
                return blob
        return None

    slice_info = _part("metadata/slice_info.config")
    settings = _part("metadata/model_settings.config")
    if not slice_info or not settings:
        return []

    try:
        names_by_id: dict[str, str] = {}
        for obj in ET.fromstring(settings).iter("object"):
            oid = obj.get("id")
            if not oid:
                continue
            for meta in obj.iter("metadata"):
                if meta.get("key") == "name" and meta.get("value"):
                    names_by_id[oid] = meta.get("value")
                    break

        ids_by_name: dict[str, list[str]] = {}
        for oid, name in names_by_id.items():
            ids_by_name.setdefault(name, []).append(oid)

        plates: list[list[str]] = []
        for plate in ET.fromstring(slice_info).iter("plate"):
            ids: list[str] = []
            for obj in plate.iter("object"):
                ids.extend(ids_by_name.get(obj.get("name") or "", []))
            if ids:
                plates.append(ids)
        return plates
    except ET.ParseError:
        return []


def _3mf_points(path: Path):
    import posixpath
    import xml.etree.ElementTree as ET

    with zipfile.ZipFile(path) as bundle:
        names = [n for n in bundle.namelist() if n.lower().endswith(".model")]
        if not names:
            raise ValueError("no .model part in bundle")
        blobs = {"/" + n.lstrip("/"): bundle.read(n) for n in names}
        meta = {"/" + n.lstrip("/"): bundle.read(n) for n in bundle.namelist()
                if n.lower().endswith(".config")}

    # One plate, not the whole job. See `_3mf_plates`.
    plates = _3mf_plates(meta)
    wanted: set[str] | None = set(plates[0]) if plates else None

    # The graph is: <build><item objectid> -> <object> -> either a <mesh> or
    # <components>, each <component> naming an objectid and, in the production
    # extension, a p:path to the part holding it. Ids are namespaced per part
    # file, so an object is identified by (path, id) and not by id alone.

    objects: dict[tuple[str, str], tuple[str, "ET.Element"]] = {}
    roots: dict[str, "ET.Element"] = {}
    for part, blob in blobs.items():
        try:
            root = ET.fromstring(blob)
        except ET.ParseError:
            continue
        roots[part] = root
        for obj in root.iter():
            if obj.tag.endswith("object") and obj.get("id"):
                objects[(part, obj.get("id"))] = (part, obj)

    def _mesh_of(element):
        for child in element:
            if child.tag.endswith("mesh"):
                return child
        return None

    def _emit(part: str, oid: str, matrix, depth: int = 0):
        """Every triangle under this object, in build coordinates."""
        if depth > 8:                       # a cycle in someone's exporter
            return
        found = objects.get((part, oid))
        if not found:
            return
        _, obj = found

        mesh = _mesh_of(obj)
        if mesh is not None:
            verts: list[tuple[float, float, float]] = []
            for group in mesh:
                if not group.tag.endswith("vertices"):
                    continue
                for vertex in group:
                    if not vertex.tag.endswith("vertex"):
                        continue
                    try:
                        verts.append(_apply(matrix,
                                            float(vertex.get("x", 0.0)),
                                            float(vertex.get("y", 0.0)),
                                            float(vertex.get("z", 0.0))))
                    except (TypeError, ValueError):
                        # Position is an INDEX, so a skipped vertex renumbers
                        # every one after it and silently re-points every
                        # triangle that referenced them. A placeholder keeps
                        # the indices meaning what the file says they mean; the
                        # damage stays where the bad coordinate was instead of
                        # spreading through the whole mesh.
                        verts.append(_apply(matrix, 0.0, 0.0, 0.0))
            saw_face = False
            for group in mesh:
                if not group.tag.endswith("triangles"):
                    continue
                for tri in group:
                    if not tri.tag.endswith("triangle"):
                        continue
                    try:
                        a, b, c = (int(tri.get("v1")), int(tri.get("v2")),
                                   int(tri.get("v3")))
                    except (TypeError, ValueError):
                        continue
                    if not (0 <= a < len(verts) and 0 <= b < len(verts)
                            and 0 <= c < len(verts)):
                        continue
                    saw_face = True
                    yield verts[a]
                    yield verts[b]
                    yield verts[c]
            # A mesh we cannot read faces from still has a bounding box, and
            # `measure` is the caller that can work from loose vertices -- the
            # size on the card stays right while the picture is honestly wrong.
            if not saw_face:
                yield from verts
            return

        for group in obj:
            if not group.tag.endswith("components"):
                continue
            for comp in group:
                if not comp.tag.endswith("component"):
                    continue
                target = comp.get("objectid")
                if not target:
                    continue
                # `p:path` is the production extension; namespaced, so it is
                # matched by local name rather than by a fixed prefix.
                cpath = part
                for key, value in comp.attrib.items():
                    if key.split("}")[-1] == "path" and value:
                        cpath = posixpath.normpath("/" + value.lstrip("/"))
                        break
                yield from _emit(cpath, target,
                                 _compose(_parse_transform(comp.get("transform")),
                                          matrix),
                                 depth + 1)

    placed = False
    for part, root in roots.items():
        for build in root.iter():
            if not build.tag.endswith("build"):
                continue
            for item in build:
                if not item.tag.endswith("item") or not item.get("objectid"):
                    continue
                if wanted is not None and item.get("objectid") not in wanted:
                    continue
                placed = True
                yield from _emit(part, item.get("objectid"),
                                 _parse_transform(item.get("transform")))
    if placed:
        return

    # No <build> anywhere: emit every mesh where it stands. A file that never
    # places its objects has nothing to place them by, and one unplaced mesh is
    # the common shape of that.
    for (part, oid) in list(objects):
        yield from _emit(part, oid, _IDENTITY)


def measure(path: Path) -> dict[str, float] | None:
    """The model's bounding box in millimetres, or None.

    None means "not measurable from this file", which is the honest answer for
    a STEP body that needs a kernel to tessellate or a G-code file that is
    already toolpaths. A guess here would be a number on a card that nothing
    produced.
    """
    path = Path(path)
    suffix = path.suffix.lower()
    try:
        if suffix == ".stl":
            data = path.read_bytes()
            if len(data) < _STL_HEADER + 4:
                return None
            try:
                return _bbox(_binary_stl_points(data))
            except (ValueError, struct.error):
                return _bbox(_ascii_stl_points(data.decode("utf-8", "replace")))
        if suffix == ".3mf":
            return _bbox(_3mf_points(path))
    except Exception:
        return None
    return None



# ── preview geometry ────────────────────────────────────────────────────────

#: Triangles kept for the turntable preview.
#:
#: 500 was a CONTRACT, and the contract is now gone. `detected_event` wrote
#: this under the key "preview" with no underscore, so `model_view` did not
#: strip it and the mesh counted against the module bus's 16000-character
#: ceiling. Measured 2026-09-07 against a 4000-triangle part:
#:
#:      500 ->   497 tris, 11,993 chars   OK
#:      700 ->   700 tris, 16,865 chars   OVER
#:
#: That key is `_preview` now, matching api.py, and on that path the ceiling
#: does not apply at all: the bus clips model_view(parsed), which has already
#: dropped every `_`-prefixed key, while the island receives `result=parsed`
#: whole and uncapped (Space-Eagle core/module_bus/bus.py:374-379).
#:
#: So the budget is set by what the card can DRAW, which is the number that
#: was never the constraint. Measured 2026-09-08 in the real page, timing
#: requestAnimationFrame intervals through the canvas-2D renderer at the
#: card's own 440x472:
#:
#:       500 tris -> 60 fps   (p95 16.9ms)
#:      2500 tris -> 60 fps   (p95 17.1ms)
#:      5000 tris -> 60 fps   (p95 17.2ms)
#:      8000 tris -> 60 fps   (p95 17.3ms)
#:
#: Nowhere near the 16.6ms frame budget even at sixteen times the old number,
#: so the turntable was never what 500 was protecting. 4000 is half the
#: highest measured point, which leaves the margin on a slower machine and
#: still costs about 96 KB of base64 per model on a path with no cap.
PREVIEW_TRIANGLES = 4000

#: int16 rather than floats. The card draws this at roughly 220 pixels across,
#: turning, so a 1/32768 step is far below anything a viewer could resolve, and
#: it costs a quarter of what the same numbers cost as JSON text.
_QUANT = 32767

#: Full passes over the mesh the resolution search may spend. Each one walks
#: every triangle, and a 152,013-triangle model makes that real work.
_CLUSTER_PROBES = 8

#: How much of the budget a resolution has to spend before the search stops
#: looking. Also the line below which an under-budget probe is treated as a
#: collapse rather than an answer.
_CLUSTER_GOOD_ENOUGH = 0.6

#: Ceiling on grid resolution. Only occupied cells are ever stored, so this
#: bounds the key arithmetic rather than any allocation — but a runaway
#: estimate on a degenerate mesh should stop somewhere.
_CLUSTER_MAX_SIDE = 512


def _mesh_points(path: Path) -> list[tuple] | None:
    """Every triangle in the file, or None if we cannot read this format."""
    path = Path(path)
    suffix = path.suffix.lower()
    try:
        if suffix == ".stl":
            data = path.read_bytes()
            if len(data) < _STL_HEADER + 4:
                return None
            try:
                flat = list(_binary_stl_points(data))
            except (ValueError, struct.error):
                flat = list(_ascii_stl_points(data.decode("utf-8", "replace")))
            return [tuple(flat[i:i + 3]) for i in range(0, len(flat) - 2, 3)]
        if suffix == ".3mf":
            flat = list(_3mf_points(path))
            return [tuple(flat[i:i + 3]) for i in range(0, len(flat) - 2, 3)]
    except Exception:
        return None
    return None


def preview_mesh(path: Path,
                 max_triangles: int = PREVIEW_TRIANGLES) -> dict[str, Any] | None:
    """A small, normalised version of the model, for drawing.

    Reduced by vertex clustering, so what comes back is a coarser skin rather
    than a sample of the original one — see `_cluster`. Deliberately a picture
    and not a measurement: the dimensions the user reads come from `measure()`
    over the whole model, so decimating here can never quietly shrink the part
    that is about to be printed.
    """
    triangles = _mesh_points(path)
    if not triangles:
        return None

    if len(triangles) > max_triangles:
        triangles = _cluster(triangles, max_triangles)

    xs = [v[0] for t in triangles for v in t]
    ys = [v[1] for t in triangles for v in t]
    zs = [v[2] for t in triangles for v in t]
    lo = (min(xs), min(ys), min(zs))
    hi = (max(xs), max(ys), max(zs))
    centre = tuple((lo[i] + hi[i]) / 2.0 for i in range(3))
    # One scale for all three axes so the shape is not stretched into its own
    # bounding box; a sheet has zero extent on one axis and must not divide by
    # it either.
    span = max(hi[i] - lo[i] for i in range(3)) or 1.0

    packed: list[int] = []
    for triangle in triangles:
        for vertex in triangle:
            for axis in range(3):
                unit = (vertex[axis] - centre[axis]) / (span / 2.0)
                value = int(round(max(-1.0, min(1.0, unit)) * _QUANT))
                packed.append(max(-32768, min(32767, value)))

    blob = struct.pack(f"<{len(packed)}h", *packed)
    return {
        "triangles": len(triangles),
        "vertices": base64.b64encode(blob).decode("ascii"),
    }



def _cluster(triangles: list[tuple], budget: int) -> list[tuple]:
    """Thin a mesh toward `budget` triangles and keep it a SURFACE.

    What this replaces kept the largest triangle from each occupied cell of a
    grid. That preserved where the model IS -- the silhouette tests below still
    pass -- but the survivors were never neighbours. Keeping one facet in six
    with no edge shared between them is a scatter, not a skin, and the card
    draws it with a painter's algorithm: nothing occludes anything, so you see
    straight through the model. Measured 2026-09-08 on a real download, an
    "Urban Spiderman" figure previewed as 429 disconnected facets and read on
    screen as noise rather than as a person.

    Vertex clustering (Rossignac-Borrel) is how a low-poly model is actually
    made. Grid the bounding box; every vertex in a cell collapses to one
    representative, the average of the vertices that landed there. Rewrite each
    original triangle in terms of representatives and drop the ones whose
    corners collapsed together, because those stopped being triangles. What
    survives SHARES CORNERS with its neighbours, so it is the same closed skin
    at a coarser resolution rather than a sample of it.

    The grid search is unchanged in spirit: find the finest grid whose output
    still fits the budget. Counting is one cheap pass per probe and the number
    of probes is capped, because a 152,013-triangle model makes every pass
    real work.
    """
    xs = [v[0] for t in triangles for v in t]
    ys = [v[1] for t in triangles for v in t]
    zs = [v[2] for t in triangles for v in t]
    lo = (min(xs), min(ys), min(zs))
    hi = (max(xs), max(ys), max(zs))
    # A flat part has no thickness to divide by, and a preview of one is still
    # a preview. `or 1.0` keeps the axis collapsed instead of raising.
    ext = [(hi[i] - lo[i]) or 1.0 for i in range(3)]

    def keys_at(side: int):
        """Every triangle as a triple of cell ids, at this resolution.

        Cell ids are packed ints rather than tuples: this is the inner loop of
        every probe, and a tuple key costs several times what an int does in a
        set of hundreds of thousands.
        """
        sx, sy, sz = side / ext[0], side / ext[1], side / ext[2]
        lx, ly, lz = lo
        last = side - 1
        for t in triangles:
            packed = []
            for v in t:
                ix = int((v[0] - lx) * sx)
                iy = int((v[1] - ly) * sy)
                iz = int((v[2] - lz) * sz)
                if ix > last: ix = last
                elif ix < 0: ix = 0
                if iy > last: iy = last
                elif iy < 0: iy = 0
                if iz > last: iz = last
                elif iz < 0: iz = 0
                packed.append((ix * side + iy) * side + iz)
            yield t, packed

    def count_at(side: int) -> int:
        seen = set()
        for _t, (a, b, c) in ((t, k) for t, k in keys_at(side)):
            if a == b or b == c or a == c:
                continue            # collapsed to an edge or a point
            seen.add((a, b, c) if a < b < c else tuple(sorted((a, b, c))))
        return len(seen)

    # Finding the resolution.
    #
    # Stepping the grid one notch at a time does not work here, and the way it
    # fails is silent. Clustering DELETES any feature smaller than a cell --
    # all three corners land in the same cell and the triangle stops being one
    # -- so a tall thin part inside a wide bounding box can lose its entire
    # structure while the coarse faces that set the box survive. Measured on
    # the tapered-tower fixture: a 1x1 column in a 120x120x150 box came back as
    # the two base plates and nothing else, 2 triangles out of a 200 budget,
    # and z-extent zero. A stepping search never got near the resolution that
    # would have kept it.
    #
    # So the resolution is estimated, not stepped. Output faces track occupied
    # SURFACE cells, which grow about as side**2, so one count gives the scale
    # factor straight to the next guess. `best` keeps the finest grid measured
    # that still fits, because the estimate overshoots as readily as it
    # undershoots and the answer has to be one that actually fitted.
    # What is being maximised is the FACE COUNT under the budget, not the grid
    # resolution. Those come apart, and the difference is the whole bug: a grid
    # too coarse for a feature deletes it, so a hopeless resolution reports a
    # tiny face count, and a tiny count "fits" any budget. Choosing the finest
    # grid that fit therefore chose a grid on which the model had vanished --
    # the tower came back as its two base plates at budgets of 60 and 200,
    # while budget 500 happened to land on a good probe and kept 100% of the
    # height. Choosing the probe that spends the most of the allowance cannot
    # pick a collapse, because a collapse is what spends the least.
    #
    # Count is also discontinuous in `side`: it stays flat while cells are
    # bigger than the feature and jumps by orders of magnitude the moment they
    # are not. The estimate oscillates across that step, which is fine — every
    # probe is scored and the best is kept.
    side = max(2, int((budget / 2.0) ** 0.5))
    under_side, under_found = 2, -1         # most faces while still fitting
    over_side, over_found = None, None      # fewest faces while overflowing
    tried: set[int] = set()
    for _ in range(_CLUSTER_PROBES):
        if side in tried:
            break
        tried.add(side)
        found = count_at(side)
        if found <= budget:
            if found > under_found:
                under_side, under_found = side, found
                # Most of the allowance spent. Another full pass to gain a few
                # percent is not worth what it costs on a large mesh.
                if found >= budget * _CLUSTER_GOOD_ENOUGH:
                    break
        elif over_found is None or found < over_found:
            over_side, over_found = side, found
        scale = (budget / float(found)) ** 0.5 if found else 4.0
        nxt = int(side * scale)
        if found <= budget and nxt <= side:
            nxt = side + 1                  # room left; must go finer
        nxt = min(_CLUSTER_MAX_SIDE, max(2, nxt))
        if nxt == side:
            break
        side = nxt

    # Overflowing on purpose, when fitting would mean deleting the model.
    #
    # Face count is not continuous in `side`. It stays flat while cells are
    # bigger than the model's features and steps by orders of magnitude the
    # moment they are not, so a budget can land inside the step and have no
    # grid that fits it honestly. Measured on the tapered-tower fixture, every
    # resolution up to 118 yields 2 faces and 120 yields 241: at a budget of
    # 200 the only options are "the two base plates" and "over budget".
    #
    # The tail of this function thins by stride to the budget regardless, so
    # DRAWING cost is capped either way and the only thing being chosen here
    # is shape. A mesh that is the right shape and gets thinned beats one that
    # fits by having deleted the part.
    if over_side is not None and under_found < budget * _CLUSTER_GOOD_ENOUGH:
        side = over_side
    else:
        side = under_side

    # One full pass now that the resolution is settled: average each cell and
    # emit the faces in the same walk.
    #
    # The representative is the cell's CENTROID. Rossignac-Borrel says so and
    # it is right, but it costs the silhouette: averaging pulls the outermost
    # vertex inward by up to half a cell, so the preview's bounding box shrinks
    # against the part's, by different amounts per axis depending on where the
    # cell boundaries fell. Measured on the tapered tower, that kept 94.7% of a
    # 90mm height against a 95% fidelity floor, and the aspect ratio is what
    # says the preview is the same object.
    #
    # Taking the outermost vertex instead fixed the box and cost the surface:
    # every cell then reports its most extreme point, which is a spiky mesh,
    # and once the card renders with real lighting and averaged normals a spiky
    # mesh is exactly what it looks like.
    #
    # So: centroid for the shape, and the box corrected afterwards.
    acc: dict[int, list] = {}
    faces: list[tuple[int, int, int]] = []
    seen: set = set()
    for triangle, (a, b, c) in keys_at(side):
        for key, vertex in zip((a, b, c), triangle):
            bucket = acc.get(key)
            if bucket is None:
                acc[key] = [vertex[0], vertex[1], vertex[2], 1]
            else:
                bucket[0] += vertex[0]
                bucket[1] += vertex[1]
                bucket[2] += vertex[2]
                bucket[3] += 1
        if a == b or b == c or a == c:
            continue
        canon = tuple(sorted((a, b, c)))
        if canon in seen:
            continue
        seen.add(canon)
        faces.append((a, b, c))

    rep = {k: (v[0] / v[3], v[1] / v[3], v[2] / v[3]) for k, v in acc.items()}

    # Put the box back. The clustered mesh is uniformly slightly smaller than
    # the part; stretching each axis back onto the original extent restores the
    # aspect ratio exactly while leaving the smooth centroid surface alone. It
    # is a correction of about five percent, on a thing whose own docstring
    # calls it a picture and not a measurement -- the numbers the user reads
    # come from `measure()` over the whole model and never from here.
    used = [v for v in rep.values()]
    if used:
        clo = [min(v[i] for v in used) for i in range(3)]
        chi = [max(v[i] for v in used) for i in range(3)]
        scale = []
        for i in range(3):
            span = chi[i] - clo[i]
            scale.append(((hi[i] - lo[i]) / span) if span > 1e-12 else 1.0)
        rep = {k: tuple(lo[i] + (v[i] - clo[i]) * scale[i] for i in range(3))
               for k, v in rep.items()}

    kept = [(rep[a], rep[b], rep[c]) for a, b, c in faces]

    if not kept:
        # Everything collapsed into one cell, which needs a degenerate model or
        # a budget under eight. A preview of something beats the calibration
        # cube the card falls back to when there is no mesh at all.
        stride = max(1, len(triangles) // max(1, budget))
        return triangles[::stride][:budget]

    if len(kept) > budget:
        # The search lands under budget by construction; this is the floor case
        # where even a 2x2x2 grid overflows. Walk at a stride rather than drop
        # the smallest: the large faces of a part sit on one side of it, so
        # thinning by area flattens the model.
        step = len(kept) / float(budget)
        kept = [kept[int(i * step)] for i in range(budget)]
    return kept


def _tri_area(triangle) -> float:
    (ax, ay, az), (bx, by, bz), (cx, cy, cz) = triangle
    ux, uy, uz = bx - ax, by - ay, bz - az
    vx, vy, vz = cx - ax, cy - ay, cz - az
    nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
    return 0.5 * math.sqrt(nx * nx + ny * ny + nz * nz)


def settled_models(directory: Path, sizes: dict[str, int] | None = None
                   ) -> list[Path]:
    """Models in `directory` whose size has stopped changing.

    `sizes` carries the previous sighting between calls: a file is only offered
    once it has been seen twice at the same length. A browser writing in place
    would otherwise be interrupted mid-transfer and the model measured from
    half a file.
    """
    directory = Path(directory)
    if sizes is None:
        sizes = {}
    ready: list[Path] = []
    try:
        entries = sorted(directory.iterdir())
    except OSError:
        return ready

    for entry in entries:
        if not entry.is_file():
            continue
        if entry.suffix.lower() in PARTIAL_SUFFIXES:
            continue
        if entry.suffix.lower() not in MODEL_SUFFIXES:
            continue
        try:
            size = entry.stat().st_size
        except OSError:
            continue
        key = str(entry)
        if sizes.get(key) == size and size > 0:
            ready.append(entry)
        sizes[key] = size
    return ready


def choose_printer(dimensions: dict[str, float] | None,
                   fleet: dict[str, dict]) -> str | None:
    """The machine this part is going to, or None if none of them fit.

    A printer with no address is never chosen: it is a slot the user declared
    and has not filled in, and routing a job to it fails later and less
    clearly than declining it now.
    """
    candidates = [(key, cfg) for key, cfg in fleet.items()
                  if str(cfg.get("host") or "").strip()]
    if not candidates:
        return None
    if not dimensions:
        return candidates[0][0]

    fitting = []
    for key, cfg in candidates:
        volume = cfg.get("build_volume") or {}
        if not volume:
            fitting.append((key, float("inf")))
            continue
        if all(float(dimensions.get(axis, 0)) <= float(volume.get(axis, 0))
               for axis in ("x", "y", "z")):
            fitting.append((key, float(volume.get("z", 0))))
    if not fitting:
        return None
    # Smallest machine that still fits, so a keychain does not occupy the one
    # printer large enough for the part queued behind it.
    fitting.sort(key=lambda item: item[1])
    return fitting[0][0]



# ── build volume ────────────────────────────────────────────────────────────

#: What a machine can print, when nothing more specific is known. Every Elegoo
#: in the fleet declares this in its driver capabilities; the constant is the
#: floor for a printer we have no driver for.
DEFAULT_BUILD_VOLUME = {"x": 256.0, "y": 256.0, "z": 256.0}


def build_volume_for(printer_key: str, fleet: dict[str, dict]) -> dict[str, float]:
    """The printable envelope of `printer_key`, in millimetres.

    Read from the driver's capabilities rather than from config: the size of a
    machine is a fact about the machine, and an operator should not have to
    type it in correctly for the check below to mean anything.
    """
    cfg = (fleet or {}).get(printer_key) or {}
    declared = cfg.get("build_volume")
    if isinstance(declared, dict) and declared:
        return {axis: float(declared.get(axis, 0.0)) for axis in ("x", "y", "z")}
    if isinstance(declared, (list, tuple)) and len(declared) == 3:
        return dict(zip(("x", "y", "z"), (float(v) for v in declared)))

    try:
        from aethelark3d.drivers.factory import get_driver

        driver = get_driver(printer_key)
        volume = tuple(driver.capabilities.build_volume)
        return dict(zip(("x", "y", "z"), (float(v) for v in volume)))
    except Exception:
        return dict(DEFAULT_BUILD_VOLUME)


def fits(dimensions: dict[str, float] | None,
         volume: dict[str, float]) -> bool:
    """Whether the part fits. A part exactly the size of the bed fits — an
    off-by-one here refuses real prints."""
    if not dimensions:
        return True                 # nothing measured is not a reason to refuse
    return all(float(dimensions.get(axis, 0.0)) <= float(volume.get(axis, 0.0))
               for axis in ("x", "y", "z"))


def exceeded_event(path: Path, printer: str | None,
                   dimensions: dict[str, float] | None,
                   volume: dict[str, float]) -> dict[str, Any]:
    path = Path(path)
    return {
        "event": "build_volume_exceeded",
        "name": path.name,
        "path": str(path),
        "printer": printer,
        "dimensions": dimensions,
        "build_volume": volume,
        "at": time.time(),
    }


# ── the estimate ────────────────────────────────────────────────────────────

#: Background estimate threads, so a caller (or a test) can wait for them.
_ESTIMATES: list[threading.Thread] = []


def _default_slicer(model: Path, printer: str) -> dict[str, Any]:
    """Slice for real. Imported lazily: a machine with no slicer toolchain
    should still be able to watch for downloads."""
    from aethelark3d.slicers.base import SliceConfig
    from aethelark3d.slicers.elegoo import _default_backend

    result = _default_backend.slice(Path(model),
                                    SliceConfig(printer_key=printer))
    return {
        "estimated_time_seconds": getattr(result, "estimated_time_seconds", None),
        "filament_mass_grams": getattr(result, "filament_mass_grams", None),
        "gcode_path": str(getattr(result, "gcode_path", "") or ""),
    }


def estimate(path: Path, printer: str | None, fleet: dict[str, dict],
             on_event: Callable[[dict], None], slicer=None) -> None:
    """Event 2: what the slicer says, or why there will not be one.

    The volume check runs before the slicer is started, not after it finishes.
    Slicing a part that cannot fit spends minutes producing G-code that nothing
    can print.
    """
    path = Path(path)
    dimensions = measure(path)
    volume = build_volume_for(printer or "", fleet)

    if not fits(dimensions, volume):
        on_event(exceeded_event(path, printer, dimensions, volume))
        return

    run = slicer or _default_slicer
    try:
        from .config import config
        result = run(path, printer or config.default_printer)
    except Exception as e:
        on_event({
            "event": "model_estimate_failed",
            "name": path.name,
            "path": str(path),
            "printer": printer,
            "detail": f"{type(e).__name__}: {e}",
            "at": time.time(),
        })
        return

    event = estimated_event(path, printer,
                            (result or {}).get("estimated_time_seconds"))
    event["filament_grams"] = (result or {}).get("filament_mass_grams")
    if (result or {}).get("gcode_path"):
        event["gcode_path"] = result["gcode_path"]
    on_event(event)


def estimate_in_background(path: Path, printer: str | None,
                           fleet: dict[str, dict],
                           on_event: Callable[[dict], None],
                           slicer=None) -> threading.Thread:
    """Run `estimate` off the watcher's thread.

    The watcher must keep noticing files while a slice is running, and Event 1
    must not wait behind minutes of work whose answer it does not need.
    """
    worker = threading.Thread(
        target=estimate, args=(path, printer, fleet, on_event),
        kwargs={"slicer": slicer}, daemon=True)
    _ESTIMATES.append(worker)
    worker.start()
    return worker


def wait_for_estimates(timeout: float = 30.0) -> None:
    """Block until outstanding estimates finish. For callers that need the
    answer before exiting — a CLI, or a test."""
    deadline = time.monotonic() + timeout
    for worker in list(_ESTIMATES):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        worker.join(timeout=remaining)
    _ESTIMATES[:] = [w for w in _ESTIMATES if w.is_alive()]


def detected_event(path: Path, fleet: dict[str, dict]) -> dict[str, Any]:
    """Event 1: everything knowable the instant the file lands."""
    path = Path(path)
    dimensions = measure(path)
    return {
        "event": "model_detected",
        "name": path.name,
        "path": str(path),
        "dimensions": dimensions,
        # Underscore-prefixed, like api.py. Without it `model_view` leaves the
        # mesh in what the model hears, which both wastes its context on
        # quantised triangles it cannot use and counted the preview against a
        # ceiling that never applied to the island's copy.
        "_preview": preview_mesh(path),
        "printer": choose_printer(dimensions, fleet),
        "at": time.time(),
    }


def estimated_event(path: Path, printer: str | None,
                    eta_seconds: int | None) -> dict[str, Any]:
    """Event 2: the slice result, whenever the slicer gets there."""
    path = Path(path)
    return {
        "event": "model_estimated",
        "name": path.name,
        "path": str(path),
        "printer": printer,
        "eta_seconds": eta_seconds,
        "at": time.time(),
    }


def _slicer_available() -> bool:
    """Whether a real slicing backend can be imported on this machine."""
    try:
        from aethelark3d.slicers.elegoo import _default_backend  # noqa: F401
    except Exception:
        return False
    return True


def watch(directory: Path, on_event: Callable[[dict], None],
          fleet: dict[str, dict] | None = None,
          interval: float = 0.25, iterations: int | None = None,
          slicer=None, estimate_eta: bool = True) -> None:
    """Poll `directory`, emitting one detected event per new finished model.

    `iterations` bounds the loop so a caller — or a test — can run it without
    owning the process forever.
    """
    fleet = fleet if fleet is not None else {}
    sizes: dict[str, int] = {}
    announced: set[str] = set()
    settled_models(directory, sizes)          # prime, so pre-existing files
    announced.update(sizes)                   # are not announced as new

    turn = 0
    while iterations is None or turn < iterations:
        turn += 1
        time.sleep(interval)
        for model in settled_models(directory, sizes):
            key = str(model)
            if key in announced:
                continue
            announced.add(key)
            try:
                detected = detected_event(model, fleet)
                on_event(detected)
            except Exception:
                continue
            # Event 2 runs off this thread: the watcher has to keep noticing
            # files while a slice is running, and detection must not wait
            # behind minutes of work whose answer it does not need.
            if estimate_eta and (slicer is not None or _slicer_available()):
                estimate_in_background(model, detected.get("printer"), fleet,
                                       on_event, slicer=slicer)
