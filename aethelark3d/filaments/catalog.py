"""
Aethelark-3D Comprehensive Multi-Manufacturer Filament Database & Regex Engine.
Indexes manufacturer presets with physical print rules for Silk, High-Speed, Matte, TPU, and PETG.
Supports target machine resolution (@ECC for CC1, @ECC2 for CC2 / Centauri Carbon 2).
"""

import json
import os
import re
from pathlib import Path
from typing import Optional, Dict, List, Tuple, Any

PRESETS_DIR = Path(__file__).parent / "presets"
PRESETS_DIR.mkdir(parents=True, exist_ok=True)

# System slicer preset directories
SLICER_SYSTEM_DIRS = [
    PRESETS_DIR,
    Path.home() / ".config" / "ElegooSlicer" / "user" / "default" / "filament",
    Path.home() / ".config" / "ElegooSlicer" / "system" / "Elegoo" / "filament" / "ECC2",
    Path.home() / ".config" / "ElegooSlicer" / "system" / "Elegoo" / "filament" / "ECC",
    Path.home() / ".config" / "ElegooSlicer" / "system" / "Elegoo" / "filament" / "Generic",
    Path.home() / ".config" / "ElegooSlicer" / "system" / "Elegoo" / "filament",
    Path.home() / ".config" / "ElegooSlicer" / "system" / "OrcaFilamentLibrary" / "filament",
    Path.home() / ".config" / "ElegooSlicer" / "system" / "BBL" / "filament",
    Path.home() / ".config" / "ElegooSlicer" / "system" / "Custom" / "filament",
]


class FilamentPhysics:
    """
    Physical print modifiers for specialized filament materials and finishes.
    """
    @staticmethod
    def get_modifiers(material: str, variant: str) -> Dict[str, Any]:
        mat_lower = material.lower()
        var_lower = variant.lower()

        # 1. Matte High-Speed (e.g. AzureFilm HS Matte PLA): Safe flow, 220C/215C, 100% cooling
        if ("matte" in var_lower and any(w in var_lower for w in ["hs", "rapid", "hyper", "high speed"])) or "matte_hs" in var_lower or "hs_matte" in var_lower:
            return {
                "nozzle_first_layer": 220,
                "nozzle_other_layers": 215,
                "bed_temp": 60,
                "flow_ratio": 0.96,
                "outer_wall_speed": 150,
                "max_volumetric_speed": 16.0,   # Safe 16 mm3/s ceiling to prevent heat creep / clogs
                "fan_min_speed": 80,
                "fan_max_speed": 100,
                "notes": "Matte High-Speed Mode: 220°C/215°C nozzle, 60°C bed, 0.96 flow ratio, and safe 16mm³/s volumetric ceiling to prevent heat-creep clogs."
            }

        # 2. Silk PLA: Slow outer walls for high gloss, slightly higher temp, balanced fan
        if "silk" in var_lower or "silk" in mat_lower:
            return {
                "nozzle_first_layer": 225,
                "nozzle_other_layers": 220,
                "bed_temp": 60,
                "outer_wall_speed": 60,         # mm/s for mirror shine
                "fan_min_speed": 50,
                "fan_max_speed": 70,            # Avoid dulling
                "max_volumetric_speed": 14.0,   # mm3/s
                "notes": "Silk Mode: Reduced outer wall speed (60mm/s) + increased temp (220°C) for maximum gloss."
            }

        # 3. High-Speed / Rapid / HS PLA: Max flow, full cooling, high temp
        if any(w in var_lower for w in ["hs", "rapid", "hyper", "high speed", "super"]):
            return {
                "nozzle_first_layer": 225,
                "nozzle_other_layers": 220,
                "bed_temp": 60,
                "outer_wall_speed": 200,
                "fan_min_speed": 100,
                "fan_max_speed": 100,
                "max_volumetric_speed": 22.0,   # mm3/s full Centauri Carbon CoreXY flow
                "notes": "High-Speed Mode: High volumetric flow (22mm³/s) with aggressive cooling."
            }

        # 4. Matte PLA: Flow compensation for textured matte additive
        if "matte" in var_lower or "polyterra" in var_lower:
            return {
                "nozzle_first_layer": 215,
                "nozzle_other_layers": 210,
                "bed_temp": 60,
                "flow_ratio": 0.96,
                "outer_wall_speed": 100,
                "max_volumetric_speed": 16.0,
                "fan_min_speed": 80,
                "fan_max_speed": 100,
                "notes": "Matte Mode: 0.96 flow ratio calibrated for anti-chalk finish."
            }

        # 5. TPU / Flexible: Ultra-low volumetric limit, slow speed, minimal retraction
        if "tpu" in mat_lower or "flex" in var_lower:
            return {
                "nozzle_first_layer": 220,
                "nozzle_other_layers": 215,
                "bed_temp": 50,
                "outer_wall_speed": 35,
                "max_volumetric_speed": 4.0,
                "fan_min_speed": 80,
                "fan_max_speed": 100,
                "retraction_length": 0.6,
                "notes": "TPU Mode: Strict 4mm³/s volumetric ceiling to prevent extruder buckle."
            }

        # 6. PETG / Rapid PETG: Higher bed, low fan
        if "petg" in mat_lower:
            return {
                "nozzle_first_layer": 240,
                "nozzle_other_layers": 235,
                "bed_temp": 75,
                "outer_wall_speed": 120,
                "fan_min_speed": 20,
                "fan_max_speed": 40,
                "max_volumetric_speed": 18.0 if "rapid" in var_lower else 16.0,
                "notes": "PETG Mode: 75°C bed with 40% fan limit for maximum layer adhesion."
            }

        # Default Standard PLA
        return {
            "nozzle_first_layer": 220,
            "nozzle_other_layers": 215,
            "bed_temp": 60,
            "outer_wall_speed": 150,
            "max_volumetric_speed": 18.0,
            "fan_min_speed": 80,
            "fan_max_speed": 100,
            "notes": "Standard PLA Profile."
        }


# Multi-Manufacturer Regex Patterns
BRAND_PATTERNS = {
    "azurefilm": r"\b(azurefilm|azure film|azure)\b",
    "elegoo": r"\b(elegoo)\b",
    "esun": r"\b(esun|e-sun|e_sun)\b",
    "sunlu": r"\b(sunlu|jayo)\b",
    "polymaker": r"\b(polymaker|polyterra|polylite|polymax|polymide)\b",
    "overture": r"\b(overture)\b",
    "bambu": r"\b(bambu|bambu lab|bbl)\b",
    "creality": r"\b(creality|ender|hyper)\b",
    "kingroon": r"\b(kingroon)\b",
    "anycubic": r"\b(anycubic)\b",
    "geeetech": r"\b(geeetech)\b",
    "eryone": r"\b(eryone)\b",
    "prusament": r"\b(prusament|prusa)\b",
    "generic": r"\b(generic|standard|cheap|basic)\b"
}

MATERIAL_PATTERNS = {
    "pla": r"\b(pla|polyactic|ingeo)\b",
    "petg": r"\b(petg|pet-g|copolyester)\b",
    "tpu": r"\b(tpu|tpe|flex|flexible|95a|85a)\b",
    "abs": r"\b(abs)\b",
    "asa": r"\b(asa)\b",
    "pc": r"\b(pc|polycarbonate)\b",
    "pa": r"\b(pa|nylon|pa-cf|paht)\b",
    "wood": r"\b(wood|timber)\b",
    "marble": r"\b(marble|stone)\b"
}

VARIANT_PATTERNS = {
    "matte_hs": r"\b(matte\s*(hs|rapid|hyper|high speed)|(hs|rapid|hyper|high speed)\s*.*matte)\b",
    "silk": r"\b(silk|shiny|gloss|glossy|metallic|gold|silver|bronze|copper|rainbow)\b",
    "hs": r"\b(hs|rapid|hyper|high speed|high-speed|fast|super speed)\b",
    "plus": r"\b(plus|\+|pro|v2|2\.0|tough|max)\b",
    "matte": r"\b(matte|mat|polyterra|anti-glare)\b",
    "cf": r"\b(cf|carbon|carbon fiber|carbon-fiber)\b",
    "glow": r"\b(glow|luminous|phosphorescent)\b",
    "translucent": r"\b(translucent|clear|transparent)\b",
    "basic": r"\b(basic|standard|regular|classic)\b"
}


def parse_spoken_filament(text: str) -> Dict[str, str]:
    """
    Parse any natural language voice string into structured filament metadata using Regex.
    Examples:
        - "AzureFilm HS Black Matte PLA" -> {brand: 'azurefilm', material: 'pla', variant: 'matte_hs', color: 'Black'}
        - "eSUN PLA HS Black" -> {brand: 'esun', material: 'pla', variant: 'hs', color: 'Black'}
        - "Sunlu Matte White" -> {brand: 'sunlu', material: 'pla', variant: 'matte', color: 'White'}
    """
    clean = text.lower().strip()

    # 1. Detect Brand
    detected_brand = "elegoo"
    for brand, pattern in BRAND_PATTERNS.items():
        if re.search(pattern, clean):
            detected_brand = brand
            break

    # 2. Detect Material
    detected_mat = "pla"
    for mat, pattern in MATERIAL_PATTERNS.items():
        if re.search(pattern, clean):
            detected_mat = mat
            break

    # 3. Detect Variant (Check composite patterns like matte_hs first)
    has_matte = bool(re.search(r"\b(matte|mat|polyterra)\b", clean))
    has_hs = bool(re.search(r"\b(hs|rapid|hyper|high speed|high-speed|fast)\b", clean))
    
    if has_matte and has_hs:
        detected_var = "matte_hs"
    else:
        detected_var = "basic"
        for var, pattern in VARIANT_PATTERNS.items():
            if re.search(pattern, clean):
                detected_var = var
                break

    # 4. Detect Color
    colors = ["black", "white", "red", "blue", "green", "yellow", "orange", "purple", "grey", "gray", "silver", "gold", "pink", "brown", "cyan", "clear"]
    detected_color = "Black"
    for c in colors:
        if re.search(rf"\b{c}\b", clean):
            detected_color = c.capitalize()
            break

    return {
        "brand": detected_brand,
        "material": detected_mat,
        "variant": detected_var,
        "color": detected_color,
        "raw_query": text
    }


def _flatten_profile(path: Path) -> Dict[str, Any]:
    """Resolve a filament preset's `inherits` chain into one merged dict."""
    idx = getattr(_flatten_profile, "_idx", None)
    if idx is None:
        idx = {}
        for d in SLICER_SYSTEM_DIRS:
            base = Path(d)
            if not base.is_dir():
                continue
            for f in base.rglob("*.json"):
                try:
                    nm = json.loads(f.read_text(encoding="utf-8")).get("name")
                except Exception:
                    continue
                if nm and nm not in idx:
                    idx[nm] = f
        _flatten_profile._idx = idx
    merged: Dict[str, Any] = {}
    try:
        cur = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return merged
    seen: set = set()
    steps = 0
    while cur and steps < 12:
        for k, v in cur.items():
            merged.setdefault(k, v)
        inh = cur.get("inherits")
        if not inh or inh in seen or inh not in idx:
            break
        seen.add(inh)
        try:
            cur = json.loads(idx[inh].read_text(encoding="utf-8"))
        except Exception:
            break
        steps += 1
    return merged


def _physics_from_profile(path: Path) -> Dict[str, Any]:
    """The REAL settings a resolved profile prints at, per machine.

    Replaces the hardcoded get_modifiers table for any filament we actually
    have a profile for — so `a3d filament` reports what the printer will do
    (Elegoo PETG on a Centauri: nozzle 250, not the table's 235), not a guess
    that ignores the machine.
    """
    m = _flatten_profile(path)

    def num(key, default):
        v = m.get(key)
        if isinstance(v, list) and v:
            v = v[0]
        try:
            return type(default)(v)
        except (TypeError, ValueError):
            return default

    # Only the machine-dependent values the material table gets wrong. The
    # speed/fan/quality hints (outer_wall_speed, retraction) are left to
    # get_modifiers, which the caller merges under these — they are process
    # settings a filament profile does not carry.
    out: Dict[str, Any] = {}
    if "nozzle_temperature_initial_layer" in m:
        out["nozzle_first_layer"] = num("nozzle_temperature_initial_layer", 220)
    if "nozzle_temperature" in m:
        out["nozzle_other_layers"] = num("nozzle_temperature", 215)
    if "textured_plate_temp" in m:
        out["bed_temp"] = num("textured_plate_temp", 60)
    # Flow (max_volumetric_speed) is intentionally NOT taken from the profile
    # here: the real slice loads the profile directly (--load-filaments), so
    # gcode already gets its true flow. This display/synthesis value stays with
    # get_modifiers' deliberate safety ceilings, which resolution mis-matches
    # cannot inflate (e.g. "TPU 95A" resolving to a RAPID profile at 12 mm3/s).
    return out


def resolve_filament_profile(query: str, printer: str = "CC1") -> Tuple[str, Path, Dict[str, Any]]:
    """(profile name, profile path, physics) for a spoken filament on a printer.

    One source of truth: filaments.resolver. The material is never substituted;
    an unknown one raises filaments.resolver.UnknownMaterial.
    """
    from aethelark3d.printer_models import model_for_key, slicer_profile
    from .resolver import parse, plan
    model, entry, _ = model_for_key(printer or "")
    profile = (slicer_profile(model.printer_model, float(entry.get("nozzle") or 0.4))
               if model else None)
    p = plan(query, profile.family if profile else "ECC", printer=model)
    spec = parse(query)
    physics = FilamentPhysics.get_modifiers(p.material, " ".join(sorted(spec.variants)))
    physics.update({k: v for k, v in p.physics().items() if v is not None})
    return p.name, p.path, physics
