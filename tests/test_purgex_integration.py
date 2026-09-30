"""
PURGEX End-to-End Multi-Color Integration & Physical Benchmark Test Suite.
Validates:
1. LayerVolumetricBudgeter mathematical line area and zero-waste infill routing.
2. SpokedRosePrimeTower section modulus anti-topple mechanics & G-code generation.
3. Full A/B multi-color benchmark comparing default static purge vs PURGEX engine.
4. Optical shielding 3rd-wall enforcement on high-luminance surfaces.
"""

from pathlib import Path
import pytest

from aethelark3d.slicers.purge import (
    LayerVolumetricBudgeter,
    SpokedRosePrimeTower,
    apply_purgex_multi_color_optimization,
    calculate_purging_volume,
    should_enable_third_wall
)
from aethelark3d.slicers.elegoo import ElegooSlicerBackend
from aethelark3d.slicers.base import SliceConfig
from tests.test_real_artifacts import create_synthetic_binary_stl


def test_layer_volumetric_budgeter_math():
    """Verify first-principles flattened-stadium extrusion line mathematics."""
    budgeter = LayerVolumetricBudgeter(line_width=0.45, layer_height=0.20)

    # Line cross-section area A = (0.45 - 0.20)*0.20 + pi*(0.10)^2 ~= 0.0814 mm2
    assert abs(budgeter.line_area_mm2 - 0.0814) < 0.001

    # Case A: Transition requiring 100mm³ with 500mm infill + 200mm support available
    # Available volume = 700 * 0.0814 ~= 57.0 mm³
    # Net chute purge should be 100 - 57.0 = 43.0 mm³
    budget_a = budgeter.calculate_layer_budget(
        infill_length_mm=500.0,
        support_length_mm=200.0,
        required_purge_volume_mm3=100.0
    )
    assert budget_a["absorbed_infill_mm3"] > 56.0
    assert budget_a["chute_purge_mm3"] < 44.0
    assert budget_a["chute_reduction_percent"] > 55.0

    # Case B: Transition requiring 30mm³ with 800mm infill available
    # Available volume = 800 * 0.0814 ~= 65.1 mm³ -> 100% absorbed into internal structure!
    budget_b = budgeter.calculate_layer_budget(
        infill_length_mm=800.0,
        support_length_mm=0.0,
        required_purge_volume_mm3=30.0
    )
    assert budget_b["absorbed_infill_mm3"] == 30.0
    assert budget_b["chute_purge_mm3"] == 0.0
    assert budget_b["chute_reduction_percent"] == 100.0


def test_spoked_rose_prime_tower_mechanics_and_gcode():
    """Verify 8-lobed epicycloid geometry and polar moment of inertia advantage."""
    tower = SpokedRosePrimeTower(center_x=220.0, center_y=220.0, base_radius_mm=10.0, lobe_amplitude_mm=3.5)

    # 1. Structural Section Modulus advantage
    props = tower.get_section_modulus()
    assert props["rose_outer_diameter_mm"] == 27.0
    assert props["anti_topple_advantage_ratio"] >= 3.0

    # 2. G-Code Generation on Layer 0 (Must include Brim + Hub + 8 Spokes)
    gcode_l0 = tower.generate_layer_gcode(layer_index=0, layer_z=0.20)
    gcode_l0_str = "\n".join(gcode_l0)

    assert "; TYPE: Prime Tower Brim" in gcode_l0_str
    assert "; TYPE: Prime Tower Perimeter" in gcode_l0_str
    assert "; TYPE: Prime Tower Hub" in gcode_l0_str
    assert "; TYPE: Prime Tower Radial Spokes" in gcode_l0_str
    assert "Z0.20" in gcode_l0_str

    # 3. G-Code Generation on Layer 20 (No brim, regular tower pass)
    gcode_l20 = tower.generate_layer_gcode(layer_index=20, layer_z=4.20)
    gcode_l20_str = "\n".join(gcode_l20)
    assert "; TYPE: Prime Tower Brim" not in gcode_l20_str
    assert "; TYPE: Prime Tower Radial Spokes" in gcode_l20_str


def test_purgex_ab_benchmark_and_waste_reduction():
    """
    Direct A/B Multi-Color Benchmark:
    Compares 20-toolchange multi-color print between:
    - DEFAULT_PURGE: Static 350mm³ per swap = 7,000 mm³ dumped out chute.
    - PURGEX: Asymmetric CIELAB matrix + sacrificial infill budgeter.
    """
    # Construct synthetic 20-layer multi-color G-code stream
    gcode_lines = ["; HEADER", "G28", "G90", "M83"]
    tool_sequence = [0, 1, 0, 2, 0, 1, 3, 0, 2, 1] * 2  # 20 toolchanges

    for layer_idx, tool in enumerate(tool_sequence):
        gcode_lines.append(f";LAYER_CHANGE Z={0.20 * (layer_idx + 1):.2f}")
        gcode_lines.append(f"T{tool}")
        gcode_lines.append(";TYPE:Internal infill")
        # Simulate 400mm of internal infill path per layer
        for step in range(4):
            gcode_lines.append(f"G1 X{100 + step * 20} Y{100 + step * 20} E1.5")
        gcode_lines.append(";TYPE:Outer wall")
        gcode_lines.append(f"G1 X{100} Y{100} E0.8")

    raw_gcode = "\n".join(gcode_lines)

    # Run PURGEX multi-color optimization
    optimized_gcode, stats = apply_purgex_multi_color_optimization(
        gcode_text=raw_gcode,
        ams_filament_colors=["White", "Black", "Banana Yellow", "Dark Blue"],
        nozzle_diameter=0.4,
        enable_rose_tower=True
    )

    assert stats["toolchanges"] == 19
    assert stats["industry_default_waste_mm3"] == 19 * 350.0
    # Net chute waste should be dramatically reduced by over 60%-75%
    assert stats["purgex_chute_waste_mm3"] < 2800.0
    assert stats["net_savings_percent"] >= 60.0
    assert stats["infill_absorbed_mm3"] > 500.0
    assert stats["rose_tower_used"] is True
    assert stats["third_wall_shields_activated"] > 0

    # Inspect G-code text annotations
    assert "; --- PURGEX TOOLCHANGE" in optimized_gcode
    assert "; [PURGEX: 3rd Wall Optical Shield" in optimized_gcode
    assert "; TYPE: Prime Tower Radial Spokes" in optimized_gcode


def test_end_to_end_purgex_slicing_pipeline(tmp_path):
    """Verify that multi-material slicing in ElegooSlicerBackend passes through PURGEX."""
    stl_path = tmp_path / "multicolor_cube.stl"
    create_synthetic_binary_stl(stl_path, size_mm=20.0)

    slicer = ElegooSlicerBackend()
    cfg = SliceConfig(
        output_dir=tmp_path / "out",
        filament_query="Elegoo Rapid PLA+",
        ams_filaments=["White", "Black", "Red", "Blue"]
    )

    res = slicer.slice(stl_path, cfg)
    assert res.gcode_path.exists()
    assert res.gcode_path.suffix == ".gcode"
    assert res.filament_mass_grams > 0
