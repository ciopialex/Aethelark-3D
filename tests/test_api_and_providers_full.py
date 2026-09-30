"""
Comprehensive Tests for API Engine, Providers, and Slicing Handoff.
Exercises aethelark3d/api.py, aethelark3d/providers/makerworld.py, and aethelark3d/models.py.
"""

from unittest.mock import patch, MagicMock
from pathlib import Path
import pytest

from aethelark3d.api import search_models, download_model, find_and_prepare_print
from aethelark3d.models import UniversalDesign, UniversalProfile, SearchResult
from tests.test_real_artifacts import create_synthetic_binary_stl


def test_model_item_sorting_and_filtering():
    items = [
        UniversalDesign(id=1, title="Vase", url="http://fake/1", download_count=50, like_count=10, print_count=5, profiles=[UniversalProfile(id=1, weight_g=45.0, prediction_s=3600)]),
        UniversalDesign(id=2, title="Dragon", url="http://fake/2", download_count=500, like_count=150, print_count=80, profiles=[UniversalProfile(id=2, weight_g=120.0, prediction_s=7200)]),
        UniversalDesign(id=3, title="Clip", url="http://fake/3", download_count=10, like_count=2, print_count=1, profiles=[UniversalProfile(id=3, weight_g=5.0, prediction_s=600)]),
    ]

    # Downloads
    by_dl = sorted(items, key=lambda x: x.download_count, reverse=True)
    assert by_dl[0].id == 2

    # Fastest
    by_fast = sorted(items, key=lambda x: x.min_print_time_s)
    assert by_fast[0].id == 3

    # Least Material
    by_mat = sorted(items, key=lambda x: x.min_weight_g)
    assert by_mat[0].id == 3


def test_api_search_models_mocked():
    fake_item = UniversalDesign(
        id=12345,
        title="Test Bench Model",
        url="http://fake/12345",
        download_count=120,
        like_count=45,
        print_count=30,
        profiles=[UniversalProfile(id=101, title="Default", is_default=True, weight_g=20.0, prediction_s=1800)]
    )
    fake_res = SearchResult(query="bench", total=1, platform="makerworld", designs=[fake_item])

    with patch("aethelark3d.providers.makerworld.MakerWorldProvider.search", return_value=fake_res):
        res = search_models("bench", limit=5, sort_by="downloads")
        assert len(res) == 1
        assert res[0]["id"] == 12345
        assert res[0]["title"] == "Test Bench Model"


def test_find_and_prepare_print_local_stl(tmp_path):
    stl_file = tmp_path / "widget.stl"
    create_synthetic_binary_stl(stl_file, size_mm=18.0)

    # Run find_and_prepare_print on direct local STL path against Virtual Digital Twin
    prep_res = find_and_prepare_print(
        query=str(stl_file),
        filament="Elegoo Rapid PLA+",
        printer_key="VIRTUAL_CC1",
        use_simulator=True,
        auto_start=False
    )

    assert prep_res["model_title"] == "widget.stl"
    assert prep_res["filament"] is not None
    assert Path(prep_res["gcode_file"]).exists()
    assert prep_res["success"] is True
