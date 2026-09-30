"""Which printer this is, decided once, from what the printer said.

Identity now comes from the printer. A CC2 reports `MachineName: "Centauri
Carbon 2"` over ElegooLink (measured 2026-09-25, firmware 02.01.00.00); a CC1
reports it in its SDCP discovery reply. Discovery stores it as `model`, and
whether a CANVAS is attached as `canvas`.

The slicer side is not written down here at all: ElegooSlicer's own machine
profile for a model names its process preset, its filament family and its bed.
A model the installed slicer has no profile for is reported as exactly that,
and never sliced with another printer's profile.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

SLICER_DIR = Path.home() / ".config" / "ElegooSlicer"


@dataclass(frozen=True)
class Model:
    """What is true of a printer model regardless of the slicer installed."""
    #: ElegooSlicer's `printer_model` for it -- the join key into the slicer.
    printer_model: str
    #: How it is driven: "sdcp" (websocket, 3030) or "elegoolink" (MQTT, 80).
    protocol: str
    enclosed: bool
    max_nozzle_temp: float
    max_bed_temp: float
    #: Decides whether abrasive filament (CF, GF) may be sent to it.
    nozzle_material: str
    #: An active chamber heater. Without one, an enclosed printer is warmed
    #: only by its bed.
    chamber_heater: bool = False

    @property
    def name(self) -> str:
        return self.printer_model.replace("Elegoo ", "", 1)


#: Thermal limits and enclosure are the figures this module already applied
#: in drivers/factory.py, moved here so they follow the model and not the key.
MODELS: Dict[str, Model] = {m.printer_model: m for m in (
    Model("Elegoo Centauri Carbon", "sdcp", True, 320.0, 110.0, "hardened_steel"),
    Model("Elegoo Centauri Carbon 2", "elegoolink", True, 350.0, 110.0, "hardened_steel"),
    Model("Elegoo Centauri 2", "elegoolink", False, 260.0, 80.0, "brass"),
    # Nothing here was measured for the original Centauri; brass and the lower
    # limits are the cautious reading, refusing abrasives rather than risking
    # a nozzle.
    Model("Elegoo Centauri", "sdcp", False, 260.0, 80.0, "brass"),
)}

#: The names this module used to key models by. Still read, so a fleet written
#: before identity was recorded keeps working; never written.
_LEGACY_KEYS = {
    "CC1": "Elegoo Centauri Carbon",
    "CC1_COMBO": "Elegoo Centauri Carbon",
    "CC2": "Elegoo Centauri Carbon 2",
    "CC2_COMBO": "Elegoo Centauri Carbon 2",
    "C2": "Elegoo Centauri 2",
    "C2_COMBO": "Elegoo Centauri 2",
}


def identify(text: str) -> Optional[str]:
    """The printer_model a printer's self-description names, or None.

    Longest name first, so "Centauri Carbon 2" is not read as "Centauri
    Carbon" and "Centauri 2" is not read as "Centauri".
    """
    t = " ".join(re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).split())
    for model in sorted(MODELS, key=len, reverse=True):
        short = model.lower().replace("elegoo ", "", 1)
        if re.search(rf"\b{re.escape(short)}\b", t):
            return model
    return None


def model_of(cfg: Optional[Dict[str, Any]], key: str = "") -> Optional[Model]:
    """This printer's model: what it reported, else what its legacy key meant."""
    cfg = cfg or {}
    legacy = (key or "").upper()
    if legacy.startswith("VIRTUAL_"):          # the simulator's twins
        legacy = legacy[len("VIRTUAL_"):]
    found = (identify(str(cfg.get("model") or ""))
             or _LEGACY_KEYS.get(legacy))
    return MODELS.get(found) if found else None


def has_canvas(cfg: Optional[Dict[str, Any]], key: str = "") -> bool:
    cfg = cfg or {}
    if "canvas" in cfg:
        return bool(cfg["canvas"])
    return (key or "").upper().endswith("_COMBO") or bool(cfg.get("has_ams"))


def display_name(cfg: Optional[Dict[str, Any]], key: str = "") -> str:
    """What to call it out loud. A CC2 or C2 with CANVAS is sold as a Combo;
    a CC1 got CANVAS later as an add-on, and is not called one."""
    model = model_of(cfg, key)
    if model is None:
        return str((cfg or {}).get("model") or key or "printer")
    if not has_canvas(cfg, key):
        return model.name
    return (f"{model.name} with CANVAS" if model.protocol == "sdcp"
            else f"{model.name} Combo")


def model_for_key(key: str):
    """(model, fleet entry, resolved key) for a printer named by key or alias."""
    from .config import config
    from .aliases import PrinterAliasManager
    resolved = PrinterAliasManager.resolve_to_key(key) if key else key
    cfg = config.get_printer(resolved) or {}
    return model_of(cfg, resolved), cfg, resolved


@dataclass(frozen=True)
class SlicerProfile:
    """The installed ElegooSlicer's own profile for one model and nozzle."""
    machine: str            # "Elegoo Centauri Carbon 2 0.4 nozzle"
    machine_json: Path
    process: str            # "0.20mm Standard @Elegoo CC2 0.4 nozzle"
    process_json: Optional[Path]
    family: str             # "ECC2": the profile directory and filament suffix
    bed_x: float
    bed_y: float
    height: float


def slicer_profile(printer_model: str, nozzle: float = 0.4,
                   slicer_dir: Optional[Path] = None) -> Optional[SlicerProfile]:
    """This model's machine profile in the installed slicer, or None.

    Matched on the profile's own `printer_model` and `printer_variant`, not on
    a filename pattern, so a slicer update that adds a model is picked up
    without touching this code.
    """
    root = Path(slicer_dir or SLICER_DIR) / "system" / "Elegoo"
    variant = f"{float(nozzle):.1f}"
    for path in sorted((root / "machine").glob("*/*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if data.get("printer_model") != printer_model:
            continue
        if str(data.get("printer_variant") or "") != variant:
            continue
        family = path.parent.name
        process = str(data.get("default_print_profile") or "")
        process_json = root / "process" / family / f"{process}.json"
        xs, ys = [], []
        for corner in data.get("printable_area") or []:
            try:
                x, y = str(corner).lower().split("x")
                xs.append(float(x))
                ys.append(float(y))
            except ValueError:
                pass
        return SlicerProfile(
            machine=str(data.get("name") or path.stem), machine_json=path,
            process=process,
            process_json=process_json if process_json.exists() else None,
            family=family,
            bed_x=max(xs) if xs else 0.0, bed_y=max(ys) if ys else 0.0,
            height=float(data.get("printable_height") or 0.0))
    return None
