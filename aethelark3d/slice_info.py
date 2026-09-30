"""What a 3MF already knows about itself.

A `.3mf` is a zip. Bambu Studio, OrcaSlicer and ElegooSlicer all write
`Metadata/slice_info.config` into it when they save, and that file holds, per
plate, the numbers everyone assumes you can only get by slicing:

    <plate>
      <metadata key="index" value="1"/>
      <metadata key="prediction" value="9649"/>      seconds
      <metadata key="weight" value="85.05"/>         grams
      <metadata key="support_used" value="true"/>
      <filament id="1" type="PLA" color="#000000" used_m="26.79" used_g="85.05"/>
    </plate>

Measured across 120 files on this machine, 2026-09-05: every one carried it.

So the print time and filament weight the card draws as em dashes are already on
disk, computed by the slicer that produced the file. Reading them costs
milliseconds and returns the creator's ground truth rather than our estimate of
it. Running a slicer to rediscover a number the file is already carrying is the
expensive way to be less accurate.

A raw `.stl` has none of this and returns None, so the card shows a dash rather
than a number nobody measured.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

#: Where every slicer writes it.
SLICE_INFO = "Metadata/slice_info.config"


def _number(raw: Optional[str]) -> Optional[float]:
    """A figure the file carried, or None. Zero is not a print time."""
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _plate(node: ET.Element) -> Dict[str, Any]:
    meta = {m.get("key"): m.get("value")
            for m in node.findall("metadata") if m.get("key")}

    seconds = _number(meta.get("prediction"))
    grams = _number(meta.get("weight"))

    # The filament element is the only place the material and colour appear.
    # A plate can list several; the first is the one the plate is printed in.
    spool = node.find("filament")
    filament_type = colour = None
    if spool is not None:
        filament_type = (spool.get("type") or "").strip() or None
        colour = (spool.get("color") or "").strip() or None
        grams = grams or _number(spool.get("used_g"))

    index = meta.get("index")
    try:
        index = int(index)
    except (TypeError, ValueError):
        index = 0

    # Objects carry the part names, which is what a person recognises a plate
    # by -- "Torso", not "plate 2".
    names = [o.get("name") for o in node.findall("object") if o.get("name")]

    return {
        "index": index,
        "eta_seconds": int(seconds) if seconds else None,
        "grams": round(grams, 2) if grams else None,
        "filament_type": filament_type,
        "filament_color": colour,
        "supports": str(meta.get("support_used") or "").lower() == "true",
        # De-duplicated: a plate with five copies of one part lists it five
        # times, and "Part 5, Part 5, Part 5" tells a person nothing.
        "objects": sorted(set(names)),
    }


def read_slice_info(path: str | Path) -> Optional[Dict[str, Any]]:
    """What the slicer recorded, or None for a file that was never sliced.

    Never raises. A corrupt zip, a 3MF written by something that does not
    record this, or a plain STL all return None -- the caller shows a dash,
    which is the truth about what is known.
    """
    p = Path(path)
    if not p.is_file():
        return None

    try:
        with zipfile.ZipFile(p) as archive:
            if SLICE_INFO not in archive.namelist():
                return None
            root = ET.fromstring(archive.read(SLICE_INFO).decode("utf-8", "replace"))
    except (zipfile.BadZipFile, OSError, ET.ParseError, KeyError):
        return None

    plates: List[Dict[str, Any]] = [_plate(node) for node in root.findall("plate")]
    if not plates:
        return None
    plates.sort(key=lambda pl: pl["index"])

    timed = [pl["eta_seconds"] for pl in plates if pl["eta_seconds"]]
    weighed = [pl["grams"] for pl in plates if pl["grams"]]

    return {
        "plates": plates,
        "plate_count": len(plates),
        # Totals are sums of what was actually recorded, so a project where one
        # plate reports no time still totals the plates that did rather than
        # claiming the whole project is unknown.
        "eta_seconds": sum(timed) if timed else None,
        "grams": round(sum(weighed), 2) if weighed else None,
    }


def humanise(seconds: Optional[int]) -> Optional[str]:
    """`9649` -> `2h 40m`. None stays None; a card draws a dash for it."""
    if not seconds or seconds <= 0:
        return None
    hours, rest = divmod(int(seconds), 3600)
    minutes = rest // 60
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"
