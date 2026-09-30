"""The picture of the part a printer is working on, keyed by the printer-side
file name, so a live job can show what it is making."""
from __future__ import annotations

import base64
import re
import zipfile
from pathlib import Path
from typing import Optional

CACHE = Path.home() / ".cache" / "aethelark3d" / "thumbs"
_BLOCK = re.compile(r"; thumbnail begin (\d+)x(\d+) \d+\n(.*?); thumbnail end", re.S)


def _key(name: str) -> str:
    return re.sub(r"[^\w.-]+", "_", Path(str(name)).name)[:120]


def extract(local: Path) -> Optional[bytes]:
    local = Path(local)
    try:
        if zipfile.is_zipfile(local):
            with zipfile.ZipFile(local) as z:
                names = [n for n in z.namelist()
                         if n.startswith("Metadata/") and n.endswith(".png")]
                pick = next((n for n in names if "plate_1" in n), names[0] if names else None)
                if pick:
                    return z.read(pick)
                gcode = next((n for n in z.namelist() if n.endswith(".gcode")), None)
                text = z.read(gcode).decode("utf-8", "ignore")[:2_000_000] if gcode else ""
        else:
            with open(local, "r", encoding="utf-8", errors="ignore") as f:
                text = f.read(2_000_000)
    except Exception:
        return None
    best = None
    for m in _BLOCK.finditer(text):
        area = int(m.group(1)) * int(m.group(2))
        if best is None or area > best[0]:
            best = (area, m.group(3))
    if best is None:
        return None
    raw = "".join(line[2:] if line.startswith("; ") else line
                  for line in best[1].splitlines())
    try:
        return base64.b64decode(raw)
    except Exception:
        return None


def remember(local: Path, remote_name: str) -> Optional[Path]:
    data = extract(local)
    if not data:
        return None
    try:
        CACHE.mkdir(parents=True, exist_ok=True)
        out = CACHE / (_key(remote_name) + ".png")
        out.write_bytes(data)
        return out
    except OSError:
        return None


def lookup(remote_name: Optional[str]) -> Optional[Path]:
    if not remote_name:
        return None
    path = CACHE / (_key(remote_name) + ".png")
    return path if path.is_file() else None
