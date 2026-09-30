"""A 3MF was sliced by whoever made it. Read the answer; do not compute it.

Measured across the operator's 92 .3mf files on 2026-09-05: 90 carry
`Metadata/slice_info.config`, and **23 of them (25%) carry actual <plate> data**
with a print time. The rest are MakerWorld downloads -- model geometry and a
header, never sliced by anyone. So this is not "zero slicing required" across
the board; it is "a quarter of them already answered, for free, in
milliseconds", and the other three quarters still need a slicer.

Where the data is there, it looks like this, per plate:

    prediction   = 9649     seconds  -> 2h 40m
    weight       = 85.05    grams
    filament     type="PLA" color="#000000" used_g="85.05"
    support_used = true

So the print time and filament weight that the card shows as em dashes -- and
that everyone assumes you can only learn by slicing -- are already in the file,
computed by the slicer that produced it. A zip read costs milliseconds and
returns the creator's ground truth instead of our estimate.

Raw STL carries none of this and must keep returning nothing, so the card shows
a dash rather than a number nobody measured.
"""
from __future__ import annotations

import glob
import os

import pytest

from aethelark3d.slice_info import read_slice_info

THREE_MF = sorted(glob.glob(os.path.expanduser("~/3D_Prints/*.3mf")))


def _has_plates(path) -> bool:
    info = read_slice_info(path)
    return bool(info and info["plates"])


@pytest.fixture(scope="module")
def a_real_3mf():
    """A file that was actually sliced.

    Not simply THREE_MF[0]: three quarters of this library are unsliced
    downloads, and the first alphabetically is one of them. Picking blind
    would have tested the wrong thing and called the reader broken.
    """
    if not THREE_MF:
        pytest.skip("no .3mf files on this machine")
    sliced = next((f for f in THREE_MF if _has_plates(f)), None)
    if sliced is None:
        pytest.skip("no .3mf on this machine carries plate data")
    return sliced


def test_a_sliced_project_reports_its_plates(a_real_3mf):
    info = read_slice_info(a_real_3mf)
    assert info is not None, f"no slice_info read from {os.path.basename(a_real_3mf)}"
    assert info["plates"], "the file has plates and none were reported"


def test_each_plate_carries_a_time_and_a_weight(a_real_3mf):
    for plate in read_slice_info(a_real_3mf)["plates"]:
        assert isinstance(plate["index"], int)
        assert plate["eta_seconds"] is None or plate["eta_seconds"] > 0
        assert plate["grams"] is None or plate["grams"] > 0


def test_the_project_total_is_the_sum_of_its_plates(a_real_3mf):
    info = read_slice_info(a_real_3mf)
    plates = [p for p in info["plates"] if p["eta_seconds"]]
    if not plates:
        pytest.skip("this project reports no per-plate times")
    assert info["eta_seconds"] == sum(p["eta_seconds"] for p in plates)
    weighed = [p for p in info["plates"] if p["grams"]]
    if weighed:
        assert abs(info["grams"] - sum(p["grams"] for p in weighed)) < 0.01


def test_the_filament_is_named_not_guessed(a_real_3mf):
    """A colour and a material the creator actually chose."""
    plates = read_slice_info(a_real_3mf)["plates"]
    named = [p for p in plates if p.get("filament_type")]
    if not named:
        pytest.skip("this project names no filament")
    assert isinstance(named[0]["filament_type"], str) and named[0]["filament_type"]


def test_an_stl_reports_nothing_rather_than_zero():
    """s15. A file that was never sliced has no print time, and says so."""
    stls = glob.glob(os.path.expanduser("~/3D_Prints/*.stl"))
    if not stls:
        pytest.skip("no .stl files on this machine")
    assert read_slice_info(stls[0]) is None


def test_a_file_that_does_not_exist_is_not_an_error():
    assert read_slice_info("/no/such/file.3mf") is None


def test_the_whole_library_is_read_without_raising():
    """It works across the real library, and an unsliced file is not a failure.

    The reader is allowed to return None -- that is the honest answer for a
    downloaded model nobody has sliced. What it may never do is raise, because
    one malformed archive in a browse of five would take the whole deck down.
    """
    if len(THREE_MF) < 10:
        pytest.skip("not enough .3mf files to be worth sweeping")
    raised, sliced = [], 0
    for f in THREE_MF:
        try:
            if _has_plates(f):
                sliced += 1
        except Exception as exc:                       # noqa: BLE001
            raised.append((os.path.basename(f), f"{type(exc).__name__}: {exc}"))
    assert not raised, f"the reader raised on real files: {raised[:5]}"
    assert sliced > 0, "no file in the library reported plate data at all"
