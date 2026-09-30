"""Which way up to print a part.

Orientation decides whether a model needs support, how well it grips the plate,
and therefore whether the print survives the night. The arithmetic is
per-triangle and entirely local: for a candidate rotation, add up the area
resting on the bed and the area steep enough to need holding up.

    score = bed adhesion area − 1.5 × overhang area

The 1.5 is a judgement — support costs material, time, and a scarred surface —
but everything it multiplies is measured. Six candidate orientations are tried,
the six ways a box can be set down: as modelled, and rotated a quarter, a half
and three quarters of a turn about X, plus a quarter each way about Y.

The overhang rule is the standard one. An overhang's angle is measured from
vertical, so a wall is 0° and a ceiling is 90°; past 45° the extrusion has too
little of the previous layer beneath it and needs support. For a face with
downward normal `n`, that angle is `asin(-n.z)`, which makes the threshold a
single comparison against `sin(45°)`.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Sequence

Point = Sequence[float]
Triangle = Sequence[Point]

#: Past this angle from vertical, a downward face needs support.
OVERHANG_LIMIT_DEGREES = 45.0
_OVERHANG_SIN = math.sin(math.radians(OVERHANG_LIMIT_DEGREES))

#: A face is resting on the plate if it points down and sits at the model's
#: lowest point. The tolerance is a printed layer's worth of slack, so a mesh
#: whose bottom is not perfectly planar still counts as flat.
_BED_TOLERANCE_MM = 0.2
_DOWNWARD = -0.99

#: Overhang weight. Support is material, time, and a marked surface.
SUPPORT_PENALTY = 1.5


def _subtract(a: Point, b: Point) -> tuple[float, float, float]:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _cross(a, b) -> tuple[float, float, float]:
    return (a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def triangle_area(v0: Point, v1: Point, v2: Point) -> float:
    """Half the magnitude of the cross product of two edges."""
    cx, cy, cz = _cross(_subtract(v1, v0), _subtract(v2, v0))
    return 0.5 * math.sqrt(cx * cx + cy * cy + cz * cz)


def _normal(v0: Point, v1: Point, v2: Point) -> tuple[float, float, float]:
    cx, cy, cz = _cross(_subtract(v1, v0), _subtract(v2, v0))
    length = math.sqrt(cx * cx + cy * cy + cz * cz)
    if length == 0.0:
        return (0.0, 0.0, 0.0)
    return (cx / length, cy / length, cz / length)


def bed_adhesion_area(mesh: Iterable[Triangle]) -> float:
    """Area of the faces resting on the plate.

    A face parallel to the bed but held five millimetres above it is a ceiling,
    not adhesion, so height is checked as well as direction.
    """
    triangles = [tuple(t) for t in mesh]
    if not triangles:
        return 0.0

    lowest = min(vertex[2] for triangle in triangles for vertex in triangle)
    total = 0.0
    for v0, v1, v2 in triangles:
        if _normal(v0, v1, v2)[2] > _DOWNWARD:
            continue
        if max(v0[2], v1[2], v2[2]) > lowest + _BED_TOLERANCE_MM:
            continue
        total += triangle_area(v0, v1, v2)
    return total


def overhang_area(mesh: Iterable[Triangle]) -> float:
    """Area of the downward faces steeper than the 45 degree rule allows.

    Faces resting on the plate are excluded. They point straight down, so the
    angle test alone calls them overhangs — but the first layer is extruded
    onto the bed, which is the one thing always underneath it. Counting them
    made a flat plate score worse than the same plate stood on its edge, which
    is the opposite of the advice anyone wants.
    """
    triangles = [tuple(t) for t in mesh]
    if not triangles:
        return 0.0
    lowest = min(vertex[2] for triangle in triangles for vertex in triangle)

    total = 0.0
    for v0, v1, v2 in triangles:
        nz = _normal(v0, v1, v2)[2]
        if nz >= 0.0:
            continue                       # points up or sideways: supported
        if -nz <= _OVERHANG_SIN:
            continue                       # shallower than the rule allows for
        if max(v0[2], v1[2], v2[2]) <= lowest + _BED_TOLERANCE_MM:
            continue                       # sitting on the plate
        total += triangle_area(v0, v1, v2)
    return total


def score_orientation(mesh: Iterable[Triangle]) -> float:
    """Adhesion minus the weighted cost of what has to be held up."""
    triangles = [tuple(t) for t in mesh]
    return bed_adhesion_area(triangles) - overhang_area(triangles) * SUPPORT_PENALTY


# ── the sweep ───────────────────────────────────────────────────────────────

def _rotate_x(point: Point, turns: int) -> tuple[float, float, float]:
    x, y, z = point
    for _ in range(turns % 4):
        y, z = -z, y
    return (x, y, z)


def _rotate_y(point: Point, turns: int) -> tuple[float, float, float]:
    x, y, z = point
    for _ in range(turns % 4):
        x, z = z, -x
    return (x, y, z)


def _make(axis: str, turns: int):
    if axis == "x":
        return lambda p: _rotate_x(p, turns)
    return lambda p: _rotate_y(p, turns)


#: The six ways a box can be set down. Quarter turns only: an arbitrary angle
#: would need a real optimiser, and every extra candidate is a slice the user
#: waits through for a result nobody asked to be that precise.
ORIENTATIONS: tuple[tuple[str, object], ...] = (
    ("flat", _make("x", 0)),
    ("x90", _make("x", 1)),
    ("x180", _make("x", 2)),
    ("x270", _make("x", 3)),
    ("y90", _make("y", 1)),
    ("y270", _make("y", 3)),
)


@dataclass(frozen=True)
class Orientation:
    """The orientation chosen, and what the others scored."""
    name: str
    score: float
    bed_adhesion_mm2: float
    overhang_mm2: float
    mesh: tuple = field(default=(), repr=False)
    considered: tuple = ()


def apply_orientation(mesh: Iterable[Triangle], name: str) -> list[tuple]:
    """`mesh` rotated into the named orientation."""
    rotate = dict(ORIENTATIONS).get(name)
    if rotate is None:
        raise ValueError(f"unknown orientation {name!r}")
    return [tuple(rotate(vertex) for vertex in triangle) for triangle in mesh]


def best_orientation(mesh: Iterable[Triangle]) -> Orientation:
    """Try the six, keep the highest scoring one.

    Ties go to the orientation the model arrived in: turning a part for no
    measured gain is a change the user did not ask for.
    """
    triangles = [tuple(tuple(v) for v in t) for t in mesh]
    considered: list[dict] = []
    best: Orientation | None = None

    for name, rotate in ORIENTATIONS:
        turned = [tuple(rotate(v) for v in t) for t in triangles]
        adhesion = bed_adhesion_area(turned)
        overhang = overhang_area(turned)
        score = adhesion - overhang * SUPPORT_PENALTY
        considered.append({"name": name, "score": round(score, 4),
                           "bed_adhesion_mm2": round(adhesion, 4),
                           "overhang_mm2": round(overhang, 4)})
        if best is None or score > best.score:
            best = Orientation(name=name, score=score,
                               bed_adhesion_mm2=adhesion,
                               overhang_mm2=overhang,
                               mesh=tuple(turned))

    assert best is not None
    return Orientation(name=best.name, score=best.score,
                       bed_adhesion_mm2=best.bed_adhesion_mm2,
                       overhang_mm2=best.overhang_mm2,
                       mesh=best.mesh, considered=tuple(considered))
