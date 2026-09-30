"""
Aethelark-3D Thermodynamics & Quasi-Static Thermal Annealing Engine.
Implements percentage-based bed temperature stepping to mitigate heat creep in enclosed
printers during low-Tg prints (PLA, Silk, TPU), while strictly preserving high bed heat for
chamber-dependent engineering materials (ABS, ASA, PC, PA-CF) that rely on the bed for passive chamber warming.
"""

import re
from typing import Dict, Tuple, Any, Optional, Set


# Materials susceptible to heatsink heat creep in enclosed CoreXY chambers (Tg <= 60°C)
HEAT_CREEP_PRONE_MATERIALS: Set[str] = {
    "pla", "pla+", "rapid pla+", "pla basic", "silk pla", "pla silk",
    "matte pla", "pla matte", "pla-cf", "polyterra", "tpu", "tpu 95a", "flexible"
}

# High-temperature engineering materials requiring high bed temperatures to passively heat the chamber
CHAMBER_HEAT_DEPENDENT_MATERIALS: Set[str] = {
    "abs", "asa", "pc", "polycarbonate", "pa", "nylon", "pa-cf", "paht", "pet-cf", "pps", "peek"
}

# Standard percentage-based annealing gradient across the first 19 layers
# Expressed as multiplier of initial first-layer bed temperature (T_initial)
DEFAULT_ANNEALING_GRADIENT: Dict[Tuple[int, int], float] = {
    (1, 3): 1.00,    # 100% of T_initial: Max mechanical PEI adhesion anchor
    (4, 6): 0.96,    # 96%
    (7, 9): 0.92,    # 92%
    (10, 12): 0.88,  # 88%
    (13, 15): 0.84,  # 84%
    (16, 18): 0.81,  # 81%
    (19, 999999): 0.78  # 78%: Steady-state thermal floor (~46°C for 60°C base)
}


def is_heat_creep_prone(material: str) -> bool:
    """Check if material is susceptible to chamber heat creep."""
    mat_clean = material.lower().strip()
    return any(p in mat_clean for p in HEAT_CREEP_PRONE_MATERIALS)


def is_chamber_dependent(material: str) -> bool:
    """Check if material requires maintaining maximum chamber heat (e.g. ABS/ASA/PC/PA)."""
    mat_clean = material.lower().strip()
    return any(c in mat_clean for c in CHAMBER_HEAT_DEPENDENT_MATERIALS)


def should_apply_thermal_annealing(material: str, is_enclosed: bool = True,
                                   cold_bed: Optional[bool] = None) -> bool:
    """Only for a material that can be printed on a cold bed.

    `cold_bed` is the filament profile's own answer (a Cool Plate temperature
    above zero); without one, the material name decides. Materials that need
    chamber heat never get it.
    """
    if is_chamber_dependent(material):
        return False
    if cold_bed is not None:
        return cold_bed
    return is_heat_creep_prone(material)


def compute_bed_temp_for_layer(
    layer: int,
    initial_temp: float,
    min_safety_floor: float = 40.0,
    gradient: Optional[Dict[Tuple[int, int], float]] = None
) -> float:
    """Calculate the target bed temperature for a given layer index using percentage scaling."""
    grad = gradient or DEFAULT_ANNEALING_GRADIENT
    multiplier = 0.78
    for (start, end), mult in grad.items():
        if start <= layer <= end:
            multiplier = mult
            break

    stepped_temp = round(initial_temp * multiplier, 1)
    return min(initial_temp, max(min_safety_floor, stepped_temp))


def calculate_heat_flux_reduction(initial_bed: float, floor_bed: float, chamber_temp: float = 35.0) -> float:
    """
    Calculates percentage drop in convective heat transfer into chamber:
    Heat Flux Reduction % = ((T_initial - T_chamber) - (T_floor - T_chamber)) / (T_initial - T_chamber) * 100
    """
    delta_initial = max(1.0, initial_bed - chamber_temp)
    delta_floor = max(0.0, floor_bed - chamber_temp)
    reduction = ((delta_initial - delta_floor) / delta_initial) * 100.0
    return round(min(100.0, max(0.0, reduction)), 1)


def apply_quasi_static_annealing(
    gcode_text: str,
    material: str = "PLA",
    initial_bed_temp: Optional[float] = None,
    is_enclosed: bool = True,
    min_safety_floor: float = 40.0,
    cold_bed: Optional[bool] = None,
) -> Tuple[str, Dict[str, Any]]:
    """
    Post-processes raw G-code to inject percentage-based bed temperature stepping (M140 S<temp>)
    across layer transitions for heat-creep-prone filaments.
    """
    if not should_apply_thermal_annealing(material, is_enclosed=is_enclosed, cold_bed=cold_bed):
        return gcode_text, {
            "applied": False,
            "reason": f"Annealing bypassed: {material} is not printed on a cold bed.",
            "heat_flux_reduction_percent": 0.0
        }

    # Extract initial bed temperature if not provided
    if initial_bed_temp is None:
        m140_match = re.search(r"M140\s+S(\d+)", gcode_text)
        m190_match = re.search(r"M190\s+S(\d+)", gcode_text)
        if m190_match:
            initial_bed_temp = float(m190_match.group(1))
        elif m140_match:
            initial_bed_temp = float(m140_match.group(1))
        else:
            initial_bed_temp = 58.0

    lines = gcode_text.splitlines()
    output_lines = []
    current_layer = 0
    last_commanded_temp = initial_bed_temp
    temperature_steps_injected = 0

    # Layer regex patterns (ElegooSlicer, OrcaSlicer, PrusaSlicer, Cura)
    layer_regex = re.compile(r";\s*(?:LAYER(?:_CHANGE)?|layer|BEFORE_LAYER_CHANGE|AFTER_LAYER_CHANGE)[:\s]*(\d+)", re.IGNORECASE)

    for line in lines:
        match = layer_regex.search(line)
        if match:
            try:
                current_layer = int(match.group(1))
            except ValueError:
                current_layer += 1
            
            # Compute stepped bed temperature for current layer
            target_temp = compute_bed_temp_for_layer(current_layer, initial_bed_temp, min_safety_floor)
            
            output_lines.append(line)
            if target_temp != last_commanded_temp and current_layer > 1:
                output_lines.append(f"M140 S{int(target_temp)} ; [Aethelark-3D] Quasi-Static Annealing (Layer {current_layer}: {target_temp}°C)")
                last_commanded_temp = target_temp
                temperature_steps_injected += 1
            continue

        output_lines.append(line)

    floor_temp = compute_bed_temp_for_layer(19, initial_bed_temp, min_safety_floor)
    flux_reduction = calculate_heat_flux_reduction(initial_bed_temp, floor_temp, chamber_temp=35.0)

    stats = {
        "applied": True,
        "material": material,
        "initial_bed_temp": initial_bed_temp,
        "steady_floor_temp": floor_temp,
        "heat_flux_reduction_percent": flux_reduction,
        "steps_injected": temperature_steps_injected,
        "estimated_power_reduction_percent": round((1.0 - (floor_temp / max(1.0, initial_bed_temp))) * 100.0 * 1.5, 1)
    }

    return "\n".join(output_lines), stats
