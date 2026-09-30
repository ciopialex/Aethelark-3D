"""Spoken filament -> the slicer profile to print with, and the temperatures.

Order of trust:
  1. the printer family's own profile for the same material (machine-tuned
     speeds and flow), e.g. "Elegoo ASA @ECC" for a Centauri Carbon;
  2. temperatures from a profile for the same brand and material, if one exists;
  2. the brand's own material settings (temperatures, flow, cooling), from a
     local profile or OrcaSlicer's library;
  3. on an enclosed printer with no chamber heater, a hotter bed for materials
     that want a warm chamber;
  4. temperatures the user gave, which win over everything.
A different material is never substituted. No profile for the material means
no plan, and the caller says so.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

MATERIALS = ("pla", "petg", "pctg", "pet", "abs", "asa", "pc", "paht", "pa", "tpu",
             "pva", "hips", "pp", "pps", "pvb")
COMPOSITES = ("cf", "gf")
VARIANTS = ("plus", "pro", "silk", "matte", "hs", "rapid", "basic", "translucent",
            "marble", "wood", "galaxy", "sparkle", "aero", "lw", "fr", "prime",
            "hyper", "tough", "glow", "metal")
BRANDS = {
    "elegoo": ("elegoo",), "esun": ("esun", "e sun"), "sunlu": ("sunlu",),
    "polymaker": ("polymaker", "polyterra", "polylite", "polymax"),
    "bambu": ("bambu", "bbl"), "overture": ("overture",), "azurefilm": ("azurefilm", "azure"),
    "creality": ("creality",), "prusament": ("prusament", "prusa"), "generic": ("generic",),
    "jayo": ("jayo",), "eryone": ("eryone",), "kingroon": ("kingroon",), "anycubic": ("anycubic",),
    "coex": ("coex",), "kexcelled": ("kexcelled",), "fiberon": ("fiberon",),
}
WARM_CHAMBER = ("abs", "asa", "pc", "pa", "paht", "pps")
CHAMBER_BED_BOOST = 10
MATERIAL_KEYS = ("nozzle_temperature_range_low", "nozzle_temperature_range_high",
                 "filament_flow_ratio", "filament_density", "fan_min_speed", "fan_max_speed",
                 "fan_cooling_layer_time", "slow_down_layer_time", "slow_down_min_speed",
                 "close_fan_the_first_x_layers", "full_fan_speed_layer", "overhang_fan_speed",
                 "overhang_fan_threshold", "filament_max_volumetric_speed")
_BED_KEYS = ("textured_plate_temp", "hot_plate_temp", "cool_plate_temp", "eng_plate_temp",
             "supertack_plate_temp", "textured_cool_plate_temp")


@dataclass
class Spec:
    material: str = ""
    composite: str = ""
    brand: str = ""
    variants: frozenset = frozenset()


@dataclass
class Plan:
    name: str
    path: Path
    material: str
    brand: str
    source: str
    nozzle: Optional[int] = None
    nozzle_first: Optional[int] = None
    bed: Optional[int] = None
    bed_first: Optional[int] = None
    overrides: Dict[str, int] = field(default_factory=dict)
    label: str = ""
    brand_keys: Dict[str, Any] = field(default_factory=dict)

    def physics(self) -> Dict[str, Any]:
        return {"type": self.material.upper(), "nozzle_first_layer": self.nozzle_first,
                "nozzle_other_layers": self.nozzle, "bed_temp": self.bed,
                "bed_first_layer": self.bed_first, "source": self.source}


class UnknownMaterial(ValueError):
    pass


def parse(text: str) -> Spec:
    s = (text or "").lower().replace("+", " plus ").replace("-", " ").replace("_", " ")
    s = s.replace("carbon fiber", "cf").replace("carbon fibre", "cf").replace("glass fiber", "gf")
    words = re.findall(r"[a-z0-9.]+", s)
    material = next((w for w in words if w in MATERIALS), "")
    if material == "pet" and "g" in words:
        material = "petg"
    composite = next((w for w in words if w in COMPOSITES), "")
    brand = ""
    joined = " " + " ".join(words) + " "
    for key, names in BRANDS.items():
        if any(f" {n} " in joined for n in names):
            brand = key
            break
    variants = frozenset(w for w in words if w in VARIANTS)
    if not material and variants & {"silk", "matte", "marble", "wood", "galaxy", "sparkle", "glow"}:
        material = "pla"
    return Spec(material, composite, brand, variants)


def _preset_files() -> List[Path]:
    from .catalog import PRESETS_DIR
    from .online import cache_dir
    root = Path.home() / ".config" / "ElegooSlicer"
    dirs = [PRESETS_DIR, cache_dir(), root / "user" / "default" / "filament",
            root / "system" / "Elegoo" / "filament", root / "system" / "OrcaFilamentLibrary" / "filament"]
    out = []
    for d in dirs:
        if d.is_dir():
            out.extend(p for p in d.rglob("*.json") if "@" in p.stem)
    return out


def _tag(path: Path) -> str:
    return path.stem.rsplit("@", 1)[-1].strip()


def _name(path: Path) -> str:
    return path.stem.rsplit("@", 1)[0].strip()


def _temps(path: Path) -> Dict[str, Optional[int]]:
    from .catalog import _flatten_profile
    m = _flatten_profile(path)

    def num(key):
        v = m.get(key)
        if isinstance(v, list) and v:
            v = v[0]
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return None

    bed_key = next((k for k in _BED_KEYS if m.get(k) not in (None, "", ["0"], "0")), None)
    return {
        "nozzle": num("nozzle_temperature"),
        "nozzle_first": num("nozzle_temperature_initial_layer"),
        "bed": num(bed_key) if bed_key else None,
        "bed_first": num(bed_key + "_initial_layer") if bed_key else None,
    }


def _own_material_keys(path: Path, brand: str) -> Dict[str, Any]:
    """The brand profile's material settings: its own keys and those of any
    parent that is still the same brand."""
    index = {_name(p): p for p in _preset_files()}
    keys: Dict[str, Any] = {}
    node, steps = path, 0
    while node and steps < 6:
        try:
            body = json.loads(node.read_text(encoding="utf-8"))
        except Exception:
            break
        for k, v in body.items():
            if (k in MATERIAL_KEYS or k in _BED_KEYS or k.startswith("nozzle_temperature")
                    or any(k == b + "_initial_layer" for b in _BED_KEYS)) and v not in (["nil"], "nil"):
                keys.setdefault(k, v)
        parent = str(body.get("inherits") or "")
        nxt = index.get(parent.rsplit("@", 1)[0].strip())
        node = nxt if parent and parse(parent).brand == brand and nxt != node else None
        steps += 1
    return keys


def _first_int(v) -> Optional[int]:
    if isinstance(v, list):
        v = v[0] if v else None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _score(want: Spec, have: Spec) -> float:
    score = 0.0
    if want.brand and have.brand == want.brand:
        score += 10
    if have.brand == "elegoo":
        score += 1
    score += 3 * len(want.variants & have.variants)
    score -= 2 * len(have.variants - want.variants)
    return score


def plan(query: str, family: str, nozzle: Optional[int] = None,
         bed: Optional[int] = None, printer=None, fetch: bool = True) -> Plan:
    want = parse(query)
    if not want.material:
        raise UnknownMaterial(f"'{query}' does not name a filament material I know "
                              f"(PLA, PETG, ABS, ASA, PC, PA, TPU and their CF/GF blends).")
    files = _preset_files()
    same = []
    for p in files:
        have = parse(_name(p))
        if have.material == want.material and have.composite == want.composite:
            same.append((p, have))

    tuned = [(p, h) for p, h in same if _tag(p) == family]
    generic = [(p, h) for p, h in same if _tag(p) in ("System", "base") and h.brand in ("generic", "")]
    pool = tuned or generic
    if not pool:
        kind = want.material.upper() + (f"-{want.composite.upper()}" if want.composite else "")
        raise UnknownMaterial(f"No slicer profile exists for {kind} on this printer, so it "
                              f"cannot be printed safely from here.")
    base, base_spec = max(pool, key=lambda ph: (_score(want, ph[1]), -len(ph[0].stem)))
    temps = _temps(base)
    source = f"{_name(base)} profile"

    label = _name(base)
    brand_keys: Dict[str, Any] = {}
    if want.brand and base_spec.brand != want.brand:
        from .online import ONLINE_TAG, fetch as fetch_online
        branded = [(p, h) for p, h in same if h.brand == want.brand
                   and _tag(p) in (family, "System", "base", ONLINE_TAG)]
        if not branded and fetch:
            got = fetch_online(query)
            if got:
                branded = [(got, parse(_name(got)))]
        if branded:
            bp, _ = max(branded, key=lambda ph: (_score(want, ph[1]), _tag(ph[0]) == family))
            brand_keys = _own_material_keys(bp, want.brand)
            for k, key in (("nozzle", "nozzle_temperature"),
                           ("nozzle_first", "nozzle_temperature_initial_layer")):
                v = _first_int(brand_keys.get(key))
                if v:
                    temps[k] = v
            bed_key = next((k for k in _BED_KEYS if _first_int(brand_keys.get(k))), None)
            if bed_key:
                temps["bed"] = _first_int(brand_keys[bed_key])
                temps["bed_first"] = _first_int(brand_keys.get(bed_key + "_initial_layer")) or temps["bed"]
            mvs_base = _first_int(_flat_value(base, "filament_max_volumetric_speed"))
            mvs_brand = _first_int(brand_keys.get("filament_max_volumetric_speed"))
            if mvs_base and mvs_brand:
                brand_keys["filament_max_volumetric_speed"] = [str(min(mvs_base, mvs_brand))]
            label = _name(bp)
            source += f" with {_name(bp)} settings"

    if (printer is not None and not bed and want.material in WARM_CHAMBER
            and getattr(printer, "enclosed", False) and not getattr(printer, "chamber_heater", False)
            and temps.get("bed")):
        cap = int(getattr(printer, "max_bed_temp", 0) or 0)
        warm = temps["bed"] + CHAMBER_BED_BOOST
        warm = min(warm, cap) if cap else warm
        if warm > temps["bed"]:
            temps["bed"] = temps["bed_first"] = warm
            source += ", bed raised to warm the chamber"

    overrides: Dict[str, int] = {}
    if nozzle:
        temps["nozzle"] = temps["nozzle_first"] = int(nozzle)
        overrides["nozzle"] = int(nozzle)
    if bed:
        temps["bed"] = temps["bed_first"] = int(bed)
        overrides["bed"] = int(bed)
    if overrides:
        source += " with your temperatures"

    return Plan(name=_name(base), path=base, material=want.material, brand=want.brand,
                source=source, overrides=overrides, label=label, brand_keys=brand_keys, **temps)


def _flat_value(path: Path, key: str):
    from .catalog import _flatten_profile
    try:
        return _flatten_profile(path).get(key)
    except Exception:
        return None


def apply_overrides(profile: Dict[str, Any], p: Plan) -> Dict[str, Any]:
    """Write the plan's temperatures into a flattened filament profile."""
    def as_list(v: int):
        return [str(int(v))]

    unused = {k for k in _BED_KEYS if _first_int(profile.get(k)) == 0}
    for k, v in p.brand_keys.items():
        if k in unused or k[:-len("_initial_layer")] in unused:
            continue
        profile[k] = v if isinstance(v, list) else [str(v)]

    if p.nozzle:
        profile["nozzle_temperature"] = as_list(p.nozzle)
        profile["nozzle_temperature_initial_layer"] = as_list(p.nozzle_first or p.nozzle)
        lo, hi = profile.get("nozzle_temperature_range_low"), profile.get("nozzle_temperature_range_high")
        try:
            if lo and int(float(lo[0])) > p.nozzle:
                profile["nozzle_temperature_range_low"] = as_list(p.nozzle)
            if hi and int(float(hi[0])) < p.nozzle:
                profile["nozzle_temperature_range_high"] = as_list(p.nozzle)
        except (TypeError, ValueError, IndexError):
            pass
    if p.bed:
        for k in _BED_KEYS:
            if _first_int(profile.get(k)) == 0:
                continue
            profile[k] = as_list(p.bed)
            profile[k + "_initial_layer"] = as_list(p.bed_first or p.bed)
    return profile


def over_limit(p: Plan, model) -> Optional[str]:
    """Why the plan's temperatures exceed what `model` can reach, or None."""
    if model is None:
        return None
    too = []
    if p.nozzle and model.max_nozzle_temp and p.nozzle > model.max_nozzle_temp:
        too.append(f"nozzle {p.nozzle}°C is above its maximum of {model.max_nozzle_temp:.0f}°C")
    if p.bed and model.max_bed_temp and p.bed > model.max_bed_temp:
        too.append(f"bed {p.bed}°C is above its maximum of {model.max_bed_temp:.0f}°C")
    return " and ".join(too) or None


def describe(p: Plan) -> str:
    parts = [p.label or p.name]
    if p.nozzle:
        parts.append(f"nozzle {p.nozzle}°C")
    if p.bed:
        parts.append(f"bed {p.bed}°C")
    return ", ".join(parts)
