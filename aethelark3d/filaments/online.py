"""A brand's own filament profile, from OrcaSlicer's published library.

The file index is kept for two weeks; a profile, once fetched, is kept under
the name the resolver reads (`<brand material> @Online.json`).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import List, Optional

INDEX_URL = "https://api.github.com/repos/OrcaSlicer/OrcaSlicer/git/trees/main?recursive=1"
RAW_URL = "https://raw.githubusercontent.com/OrcaSlicer/OrcaSlicer/main/"
INDEX_TTL = 14 * 24 * 3600
ONLINE_TAG = "Online"


def cache_dir() -> Path:
    from ..config import DEFAULT_CONFIG_DIR
    return DEFAULT_CONFIG_DIR / "filaments"


def _offline() -> bool:
    return bool(os.environ.get("AETHELARK3D_OFFLINE"))


def _index() -> List[str]:
    path = cache_dir() / "orca_index.json"
    stale: List[str] = []
    if path.exists():
        try:
            stale = json.loads(path.read_text(encoding="utf-8"))
            if time.time() - path.stat().st_mtime < INDEX_TTL:
                return stale
        except Exception:
            stale = []
    if _offline():
        return stale
    try:
        import requests
        tree = requests.get(INDEX_URL, timeout=30).json().get("tree") or []
        names = [t["path"] for t in tree
                 if t.get("path", "").startswith("resources/profiles/")
                 and "/filament/" in t["path"] and t["path"].endswith(".json")
                 and "@" in t["path"].rsplit("/", 1)[-1]]
        if names:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(names), encoding="utf-8")
            return names
    except Exception:
        pass
    return stale


def fetch(query: str) -> Optional[Path]:
    """The best brand profile for `query` ("eSUN ASA+"), cached locally, or None."""
    from .resolver import _score, parse

    want = parse(query)
    if not (want.brand and want.material):
        return None
    best = None
    for rel in _index():
        stem = rel.rsplit("/", 1)[-1][:-5]
        name = stem.rsplit("@", 1)[0].strip()
        have = parse(name)
        if (have.brand, have.material, have.composite) != (want.brand, want.material, want.composite):
            continue
        rank = (_score(want, have), "OrcaFilamentLibrary" in rel, -len(stem))
        if best is None or rank > best[0]:
            best = (rank, rel, name)
    if best is None or _offline():
        return None
    _, rel, name = best
    out = cache_dir() / f"{name} @{ONLINE_TAG}.json"
    if out.exists():
        return out
    try:
        import requests
        from urllib.parse import quote
        body = requests.get(RAW_URL + quote(rel), timeout=20).json()
    except Exception:
        return None
    if not isinstance(body, dict) or body.get("type") != "filament":
        return None
    body["name"] = out.stem
    body["aethelark_source"] = RAW_URL + rel
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(body, indent=2), encoding="utf-8")
    return out
