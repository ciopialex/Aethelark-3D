"""
Unit and Science Tests for PURGEX Asymmetric Color-Transition Matrix Engine.
Verifies CIELAB Delta E00, asymmetric washout physics (Black->White vs White->Black),
and the adaptive 3rd-wall optical shielding decider.
"""

from aethelark3d.slicers.purge import (
    parse_color_to_rgb,
    rgb_to_lab,
    cielab_delta_e00,
    calculate_purging_volume,
    should_enable_third_wall
)


def test_color_parsing_and_cielab_conversion():
    """Verify parsing hex codes, named colors, and accurate D65 CIELAB conversions."""
    # 1. Pure White
    white_rgb = parse_color_to_rgb("#FFFFFF")
    white_lab = rgb_to_lab(white_rgb)
    assert white_lab.l >= 99.0
    assert abs(white_lab.a) < 1.0
    assert abs(white_lab.b) < 1.0

    # 2. Pure Black
    black_rgb = parse_color_to_rgb("#000000")
    black_lab = rgb_to_lab(black_rgb)
    assert black_lab.l <= 1.0

    # 3. Delta E00 between Black and White should be ~100
    de_bw = cielab_delta_e00(black_lab, white_lab)
    assert 95.0 <= round(de_bw, 1) <= 100.0


def test_asymmetric_washout_physics_savings():
    """Verify asymmetric washout: White->Black requires 60%+ less volume than Black->White."""
    # 1. Dark -> Light (Black -> White)
    flush_b_to_w = calculate_purging_volume(from_color="Black", to_color="White")
    assert flush_b_to_w["delta_luminance"] > 70.0
    assert 260.0 <= flush_b_to_w["required_volume_mm3"] <= 350.0

    # 2. Light -> Dark (White -> Black)
    flush_w_to_b = calculate_purging_volume(from_color="White", to_color="Black")
    assert flush_w_to_b["delta_luminance"] < -70.0
    assert 75.0 <= flush_w_to_b["required_volume_mm3"] <= 125.0
    assert flush_w_to_b["savings_percent"] >= 64.0  # Over 64% reduction vs 350mm3 baseline

    # Direct asymmetry ratio check
    ratio = flush_b_to_w["required_volume_mm3"] / flush_w_to_b["required_volume_mm3"]
    assert ratio >= 2.3, f"Expected Black->White to require >=2.3x more volume than White->Black, got {ratio:.2f}"

    # 3. Close chromatic neighbors (Red -> Orange)
    flush_r_to_o = calculate_purging_volume(from_color="Red", to_color="Orange")
    assert flush_r_to_o["required_volume_mm3"] <= 165.0
    assert flush_r_to_o["savings_percent"] >= 50.0


def test_adaptive_third_wall_optical_decider():
    """Verify 3rd wall is selectively enabled only for light diffuse colors and bypassed for dark/translucent."""
    # 1. White / Light diffuse surfaces with high toolchange count -> MUST enable 3rd wall
    enable_white, reason_white = should_enable_third_wall(surface_color="Snow White", toolchange_count=60)
    assert enable_white is True
    assert "optical shield" in reason_white

    enable_ivory, _ = should_enable_third_wall(surface_color="Ivory", toolchange_count=30)
    assert enable_ivory is True

    enable_yellow, _ = should_enable_third_wall(surface_color="Banana Yellow", toolchange_count=45)
    assert enable_yellow is True

    # 2. Dark opaque surfaces -> MUST bypass 3rd wall (saves material)
    enable_black, reason_black = should_enable_third_wall(surface_color="Jet Black", toolchange_count=200)
    assert enable_black is False
    assert "100% optical opacity" in reason_black

    enable_navy, _ = should_enable_third_wall(surface_color="Navy Blue", toolchange_count=100)
    assert enable_navy is False

    enable_cf, _ = should_enable_third_wall(surface_color="Black Carbon Fiber", toolchange_count=80)
    assert enable_cf is False

    # 3. Translucent / Clear PETG -> MUST bypass
    enable_clear, reason_clear = should_enable_third_wall(surface_color="Clear Transparent", material_type="PETG")
    assert enable_clear is False
    assert "Translucent" in reason_clear
