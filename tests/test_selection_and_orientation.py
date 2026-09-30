"""Choosing which model to print, and which way up to print it.

Two problems that both come down to not handing someone a job they cannot
finish.

*Selection.* A search result that needs four M3 screws and a set of magnets is
not a thing the user can print tonight; it is a shopping list with a print
attached. The default tier keeps that out. But the same rule applied to a
gearbox would reject every gearbox ever designed, because a gearbox has
bearings — so a mechanical query moves to a tier that allows fasteners and says
plainly what has to be bought.

*Orientation.* Which way a part sits on the plate decides whether it needs
support, and the arithmetic is per-triangle: area of the faces that end up on
the bed, minus the area of the faces steep enough to need holding up. The
weighting is the caller's; the geometry is not negotiable, and the tests below
build meshes whose right answer is known before the code runs.
"""
from __future__ import annotations

import math

import pytest

from aethelark3d import selection
from aethelark3d.orientation import (
    ORIENTATIONS, bed_adhesion_area, best_orientation, overhang_area,
    score_orientation, triangle_area,
)


def _design(design_id: int, title: str, tags: list[str], rating: float,
            downloads: int) -> dict:
    return {"id": design_id, "title": title, "tags": tags,
            "rating_score": rating, "download_count": downloads}


# ── the geometry is arithmetic, so check the arithmetic ─────────────────────

def test_a_right_triangle_reports_the_area_a_right_triangle_has():
    """3-4-5: area is 6. If this is wrong, everything built on it is."""
    area = triangle_area((0.0, 0.0, 0.0), (3.0, 0.0, 0.0), (0.0, 4.0, 0.0))
    assert area == pytest.approx(6.0)


def test_a_degenerate_triangle_has_no_area():
    assert triangle_area((0.0, 0.0, 0.0), (1.0, 1.0, 1.0),
                         (2.0, 2.0, 2.0)) == pytest.approx(0.0)


# ── what sits on the bed ────────────────────────────────────────────────────

# Wound so the normals point outward, as an STL's do: the underside of a solid
# faces down. Getting this backwards makes a bed face look like a ceiling.
FLAT_SQUARE = [
    ((0.0, 0.0, 0.0), (10.0, 10.0, 0.0), (10.0, 0.0, 0.0)),
    ((0.0, 0.0, 0.0), (0.0, 10.0, 0.0), (10.0, 10.0, 0.0)),
]


def test_a_flat_square_lying_down_is_all_bed_contact():
    assert bed_adhesion_area(FLAT_SQUARE) == pytest.approx(100.0)


def test_a_face_held_off_the_plate_is_not_bed_contact():
    """Only the lowest surface touches. A face parallel to the bed but 5mm up
    is a ceiling, not adhesion."""
    raised = [((0.0, 0.0, 5.0), (10.0, 10.0, 5.0), (10.0, 0.0, 5.0))]
    mesh = FLAT_SQUARE + raised

    assert bed_adhesion_area(mesh) == pytest.approx(100.0)


# ── what needs holding up ───────────────────────────────────────────────────

def test_a_vertical_wall_is_not_an_overhang():
    """Zero degrees from vertical. Every printer manages this."""
    wall = [((0.0, 0.0, 0.0), (0.0, 10.0, 0.0), (0.0, 10.0, 10.0))]
    assert overhang_area(wall) == pytest.approx(0.0)


def test_a_horizontal_ceiling_is_entirely_overhang():
    """Ninety degrees, with the part's own base ten millimetres below it.

    Modelled with that base on purpose: a lone downward triangle floating in
    space IS the lowest surface in its own mesh, so it is resting on the plate
    and not an overhang at all. The interesting case is a roof over something.
    """
    mesh = FLAT_SQUARE + [
        ((0.0, 0.0, 10.0), (10.0, 10.0, 10.0), (10.0, 0.0, 10.0)),
    ]
    assert overhang_area(mesh) == pytest.approx(50.0)


def test_a_face_on_the_plate_is_not_called_an_overhang():
    """It points straight down, so the angle rule alone condemns it — but the
    bed is underneath it, which is the one support that always exists."""
    assert overhang_area(FLAT_SQUARE) == pytest.approx(0.0)


def _sloped_face(degrees_from_vertical: float):
    """One downward triangle whose overhang angle is exactly what is asked for.

    With v0 at the origin, v1 along +Y and v2 at (a, 0, b), the normal works
    out proportional to (b, 0, -a) — so -n.z is a/hypot(a, b), and choosing
    a = sin(theta), b = cos(theta) makes the angle exact by construction
    rather than by a diagram nobody can check.
    """
    theta = math.radians(degrees_from_vertical)
    a, b = 10.0 * math.sin(theta), 10.0 * math.cos(theta)
    return [((0.0, 0.0, 0.0), (0.0, 10.0, 0.0), (a, 0.0, b))]


def test_a_surface_at_forty_degrees_still_prints():
    """The 45 degree rule is the whole point: steeper than that needs support,
    shallower does not. 40 is under the line."""
    assert overhang_area(_sloped_face(40.0)) == pytest.approx(0.0)


def test_a_surface_past_forty_five_degrees_needs_support():
    assert overhang_area(_sloped_face(70.0)) > 0.0


def test_the_threshold_sits_where_the_rule_says_it_does():
    assert overhang_area(_sloped_face(44.0)) == pytest.approx(0.0)
    assert overhang_area(_sloped_face(46.0)) > 0.0


# ── the score the spec names ────────────────────────────────────────────────

def test_the_score_is_adhesion_minus_one_and_a_half_times_overhang():
    """Stated exactly, so a later tweak to the weighting is a visible change
    rather than a quiet one."""
    mesh = FLAT_SQUARE + [((0.0, 0.0, 9.0), (10.0, 10.0, 9.0), (10.0, 0.0, 9.0))]
    expected = bed_adhesion_area(mesh) - overhang_area(mesh) * 1.5

    assert score_orientation(mesh) == pytest.approx(expected)


# ── picking one ─────────────────────────────────────────────────────────────

def test_the_sweep_considers_six_orientations():
    assert len(ORIENTATIONS) == 6


def test_a_plate_is_laid_flat_rather_than_stood_on_edge():
    """The obvious case, and the one a user would notice getting wrong: a thin
    plate printed on its edge is a tower with almost no grip on the bed."""
    plate = [
        # underside, facing down
        ((0.0, 0.0, 0.0), (50.0, 50.0, 0.0), (50.0, 0.0, 0.0)),
        ((0.0, 0.0, 0.0), (0.0, 50.0, 0.0), (50.0, 50.0, 0.0)),
        # top, facing up
        ((0.0, 0.0, 2.0), (50.0, 0.0, 2.0), (50.0, 50.0, 2.0)),
        ((0.0, 0.0, 2.0), (50.0, 50.0, 2.0), (0.0, 50.0, 2.0)),
    ]
    chosen = best_orientation(plate)

    assert chosen.name == "flat", f"stood the plate up: {chosen.name}"
    assert chosen.score > 0


def test_a_plate_arriving_on_its_edge_is_turned_flat():
    """Same plate, modelled standing up. The sweep has to rotate it down."""
    on_edge = [
        # the two big faces, in the YZ plane, normals along -X and +X
        ((0.0, 0.0, 0.0), (0.0, 50.0, 50.0), (0.0, 50.0, 0.0)),
        ((0.0, 0.0, 0.0), (0.0, 0.0, 50.0), (0.0, 50.0, 50.0)),
        ((2.0, 0.0, 0.0), (2.0, 50.0, 0.0), (2.0, 50.0, 50.0)),
        ((2.0, 0.0, 0.0), (2.0, 50.0, 50.0), (2.0, 0.0, 50.0)),
    ]
    chosen = best_orientation(on_edge)

    assert chosen.name != "flat", "left a plate standing on its edge"
    assert chosen.score > score_orientation(on_edge)


def test_every_orientation_is_scored_and_reported():
    """The caller should be able to see why, not just what."""
    chosen = best_orientation(FLAT_SQUARE)
    assert len(chosen.considered) == 6
    assert all("score" in c and "name" in c for c in chosen.considered)


def test_an_empty_mesh_does_not_explode():
    chosen = best_orientation([])
    assert chosen.score == pytest.approx(0.0)


# ── tier 1: what a person can print tonight ─────────────────────────────────

def test_a_design_needing_screws_is_not_the_default_answer():
    rejected = _design(1, "Wall Bracket", ["M3 screws", "functional"], 4.9, 9000)
    assert selection.is_zero_bom(rejected) is False


@pytest.mark.parametrize("tag", ["magnets required", "M3 screws", "glue required",
                                 "requires bearings", "M5 bolt", "heat set inserts"])
def test_the_shopping_list_tags_are_all_caught(tag):
    assert selection.is_zero_bom(_design(1, "Thing", [tag], 5.0, 1)) is False


@pytest.mark.parametrize("tag", ["print-in-place", "snap-fit", "no supports",
                                 "single plate"])
def test_the_tags_worth_having_are_not_mistaken_for_the_others(tag):
    assert selection.is_zero_bom(_design(1, "Thing", [tag], 5.0, 1)) is True


def test_a_print_in_place_design_outranks_an_equal_one_without_it():
    plain = _design(1, "Box", ["functional"], 4.8, 5000)
    in_place = _design(2, "Box", ["print-in-place"], 4.8, 5000)

    assert selection.rank_score(in_place) > selection.rank_score(plain)


def test_a_thinly_rated_design_is_filtered_out():
    picks = selection.select([
        _design(1, "Good", ["print-in-place"], 4.8, 100),
        _design(2, "Shaky", ["print-in-place"], 4.2, 999999),
    ], query="phone stand")

    assert [p["id"] for p in picks] == [1]


def test_at_most_five_come_back():
    many = [_design(i, f"Thing {i}", ["snap-fit"], 4.9, 1000 + i)
            for i in range(20)]
    assert len(selection.select(many, query="hook")) == 5


def test_the_five_are_the_most_downloaded_ones():
    many = [_design(i, f"Thing {i}", ["snap-fit"], 4.8, i * 100)
            for i in range(1, 11)]
    picks = selection.select(many, query="hook")

    assert [p["id"] for p in picks] == [10, 9, 8, 7, 6]


# ── tier 2: the exception that keeps gearboxes findable ─────────────────────

@pytest.mark.parametrize("query", ["planetary gearbox", "bench vise",
                                   "pipe clamp", "worm drive gearbox"])
def test_a_mechanical_query_moves_to_the_engineering_tier(query):
    assert selection.tier_for(query) == 2


@pytest.mark.parametrize("query", ["phone stand", "cable clip", "vase",
                                   "keychain"])
def test_an_everyday_query_stays_on_the_default_tier(query):
    assert selection.tier_for(query) == 1


def test_the_engineering_tier_keeps_a_design_that_needs_hardware():
    picks = selection.select(
        [_design(1, "Planetary Gearbox", ["M5 bolt", "requires bearings"],
                 4.8, 5000)],
        query="planetary gearbox")

    assert [p["id"] for p in picks] == [1]


def test_a_design_that_needs_hardware_says_what_to_buy():
    picks = selection.select(
        [_design(1, "Bench Vise", ["1x M5 bolt", "M3 screws"], 4.9, 4000)],
        query="bench vise")

    warning = picks[0]["hardware_required"]
    assert warning, "no hardware warning on a design that needs hardware"
    assert "M5" in " ".join(warning)


def test_a_zero_bom_design_carries_no_warning():
    picks = selection.select(
        [_design(1, "Clip", ["print-in-place"], 4.9, 4000)],
        query="cable clip")

    assert picks[0]["hardware_required"] == []
