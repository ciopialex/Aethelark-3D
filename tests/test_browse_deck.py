import pytest
from aethelark3d import api


# ── the metadata-first browse contract ──────────────────────────────────────
#
# browse_models used to download every candidate in parallel and read its
# geometry, so the island could turn the real part. Measured 2026-09-21 that
# cost 108s for five benchies — and the cost was NOT the mesh (a 970k-triangle
# model parses in ~2s). It was the download: MakerWorld answers an automated
# fetch with HTTP 418 "confirm you are not a robot", and the resolver falls
# back to a headless browser (~30s each) to get past it. Browsing five to keep
# one meant fighting that captcha five times.
#
# So browse now draws from the SEARCH alone — title, creator, stats, estimated
# weight/time, and the repository's cover image, all free in ~1.5s with no
# captcha. Nothing is downloaded until the user PICKS a model (a3d_print and
# a3d_download still download in full). These tests pin that: a browse must not
# download, and its candidates carry metadata + a cover, not geometry.
#
# The `local_models` tests below are unchanged: a file already on disk has no
# captcha and no download, so it is still measured for the real turntable.


def _spy_download():
    """A download_model that records whether it was called (it must not be)."""
    calls = {"n": 0}
    def fake(query_or_id, **kw):
        calls["n"] += 1
        return {"success": True, "file_path": "/x", "_preview": {"triangles": 1}}
    fake.calls = calls
    return fake


_SEARCH = [
    {"id": 1, "title": "One", "creator": "a", "downloads": 10, "likes": 2,
     "weight": "12.0g", "print_time": "31m", "url": "u1", "cover_url": "c1"},
    {"id": 2, "title": "Two", "creator": "b", "downloads": 9, "likes": 1,
     "weight": "20.0g", "print_time": "50m", "url": "u2", "cover_url": "c2"},
    {"id": 3, "title": "Three", "creator": "c", "downloads": 8, "likes": 0,
     "weight": "N/A", "print_time": "N/A", "url": "u3", "cover_url": None},
]


@pytest.fixture(autouse=True)
def _no_design_pages(monkeypatch):
    """browse reads each design's page for its weight and time. Not here: the
    search is faked, and the ids in it would be looked up on the real site."""
    monkeypatch.setattr(api, "_with_profiles", lambda entries, *a, **k: entries)


def test_browse_downloads_nothing(monkeypatch):
    """The whole point: browsing pays no download cost, so it trips no captcha
    and comes back in the time of one search."""
    spy = _spy_download()
    monkeypatch.setattr(api, "search_models", lambda *a, **k: list(_SEARCH))
    monkeypatch.setattr(api, "download_model", spy)
    api.browse_models("watch stand", limit=3, printer="CC1")
    assert spy.calls["n"] == 0, "browse downloaded a model — it must not"


def test_deck_carries_metadata_and_a_cover_not_geometry(monkeypatch):
    monkeypatch.setattr(api, "search_models", lambda *a, **k: list(_SEARCH))
    monkeypatch.setattr(api, "download_model", _spy_download())
    deck = api.browse_models("x", limit=3, printer="CC1")
    c = deck["candidates"][0]
    assert c["title"] == "One" and c["cover_url"] == "c1"
    assert c["printer"] == "CC1"
    assert "_preview" not in c, "browse carries no geometry — nothing is downloaded"


def test_a_metadata_only_candidate_still_earns_a_slot(monkeypatch):
    """The opposite of the old rule: a candidate is NOT dropped for lacking
    geometry, because browse never fetches geometry. Even the third result,
    whose cover is missing and whose weight is N/A, is a real option to pick."""
    monkeypatch.setattr(api, "search_models", lambda *a, **k: list(_SEARCH))
    monkeypatch.setattr(api, "download_model", _spy_download())
    deck = api.browse_models("x", limit=3)
    assert [c["id"] for c in deck["candidates"]] == [1, 2, 3]


def test_the_deck_preserves_search_rank(monkeypatch):
    monkeypatch.setattr(api, "search_models", lambda *a, **k: list(_SEARCH))
    monkeypatch.setattr(api, "download_model", _spy_download())
    deck = api.browse_models("x", limit=3)
    assert [c["id"] for c in deck["candidates"]] == [1, 2, 3]
    assert deck["printer"] is None or "candidates" in deck


def test_the_deck_routes_to_the_named_printer_then_the_fleet(monkeypatch):
    monkeypatch.setattr(api, "search_models", lambda *a, **k: list(_SEARCH))
    monkeypatch.setattr(api, "download_model", _spy_download())
    monkeypatch.setattr(api, "_configured_fleet",
                        lambda: [{"key": "ELEGOO_1", "name": "Elegoo"}])
    named = api.browse_models("x", limit=1, printer="CC2")
    assert named["candidates"][0]["printer"] == "CC2"        # named wins
    auto = api.browse_models("x", limit=1)
    assert auto["candidates"][0]["printer"] == "ELEGOO_1"    # else fleet's first


# ---- the fleet, and telling "nothing matched" from "everything broke" ----

def test_the_deck_carries_only_printers_that_have_an_address(monkeypatch):
    """A key declared and never filled in is a slot, not a printer.

    Offering it as a destination produces a job that fails later and less
    clearly than declining it now — the rule choose_printer already applies.
    """
    monkeypatch.setattr(api.config, "get_fleet", lambda: {
        "CC1": {"host": "172.20.10.4", "name": "Centauri 1"},
        "CC2": {"host": "", "name": "Centauri 2"},
        "C2":  {"name": "no host key at all"},
    })
    monkeypatch.setattr(api, "search_models", lambda *a, **k: [])
    deck = api.browse_models("x", limit=3)
    assert deck["fleet"] == [{"key": "CC1", "name": "Centauri 1"}]


def test_a_search_that_matches_nothing_is_not_a_failure(monkeypatch):
    monkeypatch.setattr(api, "search_models", lambda *a, **k: [])
    deck = api.browse_models("nonexistent thing", limit=5)
    assert deck["candidates"] == [] and deck["failed"] == []


# ---- models already on this machine ----

def _make(tmp_path, name, body=b""):
    f = tmp_path / name
    f.write_bytes(body or b"solid x\nendsolid x\n")
    return f


def test_a_name_finds_a_file_without_needing_its_full_path(tmp_path):
    """'Print my keychain file' — an absolute path is a lot to say out loud."""
    _make(tmp_path, "Keychain_Draft_v1.stl")
    _make(tmp_path, "unrelated.stl")
    hits = api.find_local_models("keychain", directory=str(tmp_path))
    assert [h.name for h in hits] == ["Keychain_Draft_v1.stl"]


def test_the_match_is_case_insensitive(tmp_path):
    _make(tmp_path, "KEYCHAIN.stl")
    assert api.find_local_models("keychain", directory=str(tmp_path))


def test_an_exact_name_outranks_a_longer_one(tmp_path):
    """Asked for 'benchy', benchy.stl should beat Baby_Seal_Benchy.stl."""
    _make(tmp_path, "Baby_Seal_Benchy.stl")
    _make(tmp_path, "benchy.stl")
    assert api.find_local_models("benchy", directory=str(tmp_path))[0].name == "benchy.stl"


def test_a_full_path_is_taken_as_given(tmp_path):
    f = _make(tmp_path, "thing.stl")
    assert api.find_local_models(str(f)) == [f.resolve()]


def test_files_that_are_not_models_are_ignored(tmp_path):
    _make(tmp_path, "keychain.txt")
    _make(tmp_path, "keychain.png")
    assert api.find_local_models("keychain", directory=str(tmp_path)) == []


def test_it_searches_subfolders(tmp_path):
    (tmp_path / "nested").mkdir()
    _make(tmp_path / "nested", "keychain.stl")
    assert api.find_local_models("keychain", directory=str(tmp_path))


def test_nothing_matching_is_an_empty_deck_not_an_error(tmp_path):
    deck = api.local_models("nothing like this", directory=str(tmp_path))
    assert deck["candidates"] == [] and deck["failed"] == []


def test_a_local_deck_has_the_same_shape_as_a_browse(tmp_path, monkeypatch):
    """One match renders as a card, several as a carousel — the island already
    knows how to do both and must not need a third thing."""
    monkeypatch.setattr(api, "_describe", lambda r, printer=None: {
        **r, "dimensions": {"x": 1, "y": 1, "z": 1},
        "_preview": {"triangles": 4, "vertices": "AA"}})
    _make(tmp_path, "keychain_a.stl")
    _make(tmp_path, "keychain_b.stl")
    deck = api.local_models("keychain", directory=str(tmp_path), printer="CC1")
    assert set(deck) == {"query", "printer", "fleet", "candidates", "failed"}
    assert len(deck["candidates"]) == 2
    assert all(c["_preview"] and c["id"] for c in deck["candidates"])


def test_an_unreadable_model_is_reported_not_dropped_silently(tmp_path):
    _make(tmp_path, "broken.stl", b"not really an stl")
    deck = api.local_models("broken", directory=str(tmp_path))
    assert deck["candidates"] == []
    assert "geometry" in deck["failed"][0]["error"]
