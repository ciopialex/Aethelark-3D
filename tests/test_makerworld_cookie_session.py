import threading
import time
import pytest
from aethelark3d.providers.makerworld import MakerWorldProvider


class FakeCfg:
    makerworld_cookie = "stale=1"
    makerworld_token = ""
    download_dir = "/tmp"


def test_harvested_jar_replaces_configured_cookie():
    """Concatenating them measured HTTP 418; the jar must win outright."""
    p = MakerWorldProvider(FakeCfg())
    p._cookie_jar = "good=2"
    headers = p._get_headers()
    assert headers["Cookie"] == "good=2"
    assert "stale" not in headers["Cookie"]


def test_configured_cookie_used_only_without_a_jar():
    p = MakerWorldProvider(FakeCfg())
    p._cookie_jar = None
    assert p._get_headers()["Cookie"] == "stale=1"


def test_jar_is_seeded_once_across_calls():
    p = MakerWorldProvider(FakeCfg())
    calls = []

    def fake_harvest():
        calls.append(1)
        return "seeded=3"

    p._harvest_cookies = fake_harvest
    assert p._seed_cookie_jar() == "seeded=3"
    assert p._seed_cookie_jar() == "seeded=3"
    assert len(calls) == 1


class FakeResp:
    def __init__(self, code, payload=None):
        self.status_code = code
        self._payload = payload or {}
    def json(self):
        return self._payload


def test_a_418_reseeds_the_jar_exactly_once():
    p = MakerWorldProvider(FakeCfg())
    seeds, codes = [], [418, 200]

    def fake_harvest():
        seeds.append(1)
        return f"jar{len(seeds)}"

    p._harvest_cookies = fake_harvest
    p.session = type("S", (), {"get": lambda self, *a, **k: FakeResp(
        codes.pop(0), {"url": "https://cdn/x.3mf", "name": "x.3mf"})})()
    url, name = p.get_download_url(124686, profile_id=138698)
    assert url == "https://cdn/x.3mf"
    assert len(seeds) == 2      # initial seed, then one re-seed after the 418


def test_five_concurrent_downloads_seed_the_jar_exactly_once():
    """I3 (final review): `a3d browse` fans five downloads across a
    ThreadPoolExecutor onto ONE shared MakerWorldProvider instance
    (providers/__init__.py holds a single module-level provider). Without a
    lock, all five threads observe an empty jar at once and each launches its
    own browser -- the review measured 5 launches for a 5-candidate deck
    against the 1 promised by commit 73ea11e.

    A barrier releases all five threads at the same instant, and the harvest
    stub sleeps long enough (0.1s, vs. microseconds to check an attribute)
    that -- absent a lock -- every thread reliably observes the jar as empty
    before any of them finishes seeding it. This is not a timing-flaky test:
    the sleep duration is what makes the race deterministic, not luck.
    """
    p = MakerWorldProvider(FakeCfg())
    launches = []
    start_barrier = threading.Barrier(5)

    def slow_harvest():
        launches.append(1)
        time.sleep(0.1)
        return f"jar{len(launches)}"

    p._harvest_cookies = slow_harvest

    def worker():
        start_barrier.wait()
        p._seed_cookie_jar()

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(launches) == 1, f"expected 1 browser launch, got {len(launches)}"
    assert p._cookie_jar == "jar1"
