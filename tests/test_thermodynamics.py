"""
Tests for Aethelark-3D Thermodynamics & Percentage-Based Quasi-Static Annealing Engine.
Verifies heat creep mitigation on PLA/Silk/TPU and chamber heat retention on ABS/ASA/PC/PA-CF.
"""

from aethelark3d.slicers.thermodynamics import (
    should_apply_thermal_annealing,
    is_heat_creep_prone,
    is_chamber_dependent,
    compute_bed_temp_for_layer,
    calculate_heat_flux_reduction,
    apply_quasi_static_annealing
)


def test_material_selectivity_for_heat_creep_vs_chamber_heat():
    """Verify that only low-Tg materials receive annealing, and high-temp engineering filaments keep maximum bed heat."""
    # 1. Low-Tg filaments prone to heatsink heat creep in enclosed chambers
    assert is_heat_creep_prone("PLA Basic") is True
    assert is_heat_creep_prone("Elegoo Rapid PLA+") is True
    assert is_heat_creep_prone("Bambu Silk PLA Red") is True
    assert is_heat_creep_prone("Polymaker PolyTerra Matte") is True
    assert is_heat_creep_prone("TPU 95A") is True
    assert should_apply_thermal_annealing("PLA Basic", is_enclosed=True) is True
    assert should_apply_thermal_annealing("Silk PLA", is_enclosed=True) is True

    # 2. Engineering filaments requiring passive chamber heat retention (ABS, ASA, PC, PA-CF)
    assert is_chamber_dependent("ABS Pro") is True
    assert is_chamber_dependent("ASA Black") is True
    assert is_chamber_dependent("Polycarbonate (PC)") is True
    assert is_chamber_dependent("Nylon PA-CF") is True
    assert is_chamber_dependent("PET-CF") is True
    
    # Must explicitly NEVER apply annealing step-down to chamber-dependent materials
    assert should_apply_thermal_annealing("ABS Pro", is_enclosed=True) is False
    assert should_apply_thermal_annealing("ASA Black", is_enclosed=True) is False
    assert should_apply_thermal_annealing("Nylon PA-CF", is_enclosed=True) is False
    assert should_apply_thermal_annealing("PC Clear", is_enclosed=True) is False


def test_percentage_based_dynamic_scaling():
    """Verify temperature stepping scales dynamically by percentage for any initial bed temperature."""
    # Base 60°C initial bed temp
    assert compute_bed_temp_for_layer(1, initial_temp=60.0) == 60.0    # 100%
    assert compute_bed_temp_for_layer(5, initial_temp=60.0) == 57.6    # 96%
    assert compute_bed_temp_for_layer(8, initial_temp=60.0) == 55.2    # 92%
    assert compute_bed_temp_for_layer(11, initial_temp=60.0) == 52.8   # 88%
    assert compute_bed_temp_for_layer(14, initial_temp=60.0) == 50.4   # 84%
    assert compute_bed_temp_for_layer(17, initial_temp=60.0) == 48.6   # 81%
    assert compute_bed_temp_for_layer(20, initial_temp=60.0) == 46.8   # 78% (Floor)

    # Base 55°C initial bed temp
    assert compute_bed_temp_for_layer(1, initial_temp=55.0) == 55.0
    assert compute_bed_temp_for_layer(20, initial_temp=55.0) == 42.9   # 78% floor

    # Safety floor enforcement (never drop below min_safety_floor e.g. 40°C)
    assert compute_bed_temp_for_layer(20, initial_temp=45.0, min_safety_floor=40.0) == 40.0


def test_convective_heat_flux_reduction_calculation():
    """Verify first-principles thermodynamic heat flux drop formula."""
    # 60°C initial bed, 46.8°C floor, 35°C ambient chamber
    # (25K delta down to 11.8K delta -> (25 - 11.8)/25 = 52.8% drop)
    reduction = calculate_heat_flux_reduction(initial_bed=60.0, floor_bed=46.8, chamber_temp=35.0)
    assert 50.0 <= reduction <= 56.0


def test_gcode_annealing_injection_pipeline():
    """Verify post-processor injects M140 commands into G-code toolpaths at layer boundaries."""
    sample_gcode = """
; Header
M140 S60
M104 S220
;LAYER:1
G1 X10 Y10 E1.0
;LAYER:4
G1 X10 Y20 E2.0
;LAYER:7
G1 X20 Y20 E3.0
;LAYER:10
G1 X20 Y10 E4.0
;LAYER:19
G1 X30 Y30 E5.0
;LAYER:25
G1 X40 Y40 E6.0
"""
    # 1. Apply to PLA
    processed_pla, stats_pla = apply_quasi_static_annealing(sample_gcode, material="PLA", is_enclosed=True)
    assert stats_pla["applied"] is True
    assert stats_pla["steps_injected"] >= 4
    assert "M140 S57" in processed_pla  # Layer 4
    assert "M140 S55" in processed_pla  # Layer 7
    assert "M140 S52" in processed_pla  # Layer 10
    assert "M140 S46" in processed_pla  # Layer 19

    # 2. Apply to ABS (must be bypassed)
    processed_abs, stats_abs = apply_quasi_static_annealing(sample_gcode, material="ABS", is_enclosed=True)
    assert stats_abs["applied"] is False
    assert processed_abs == sample_gcode
