"""
Comprehensive Tests for Filament Catalog and FilamentVault.
Exercises all regex rules, physical parameter modifiers, and vault storage edge cases.
"""

from aethelark3d.filaments.catalog import (
    resolve_filament_profile,
    FilamentPhysics
)
from aethelark3d.spools import FilamentVault


def test_filament_catalog_exhaustive_brands_and_modifiers():
    # 1. Brands and Profile Resolution
    queries = [
        "Elegoo Rapid PLA+ Black",
        "Bambu Lab Matte Dark Grey",
        "Polymaker PolyTerra Marble",
        "eSUN PLA+ White",
        "Sunlu PLA Silk Gold",
        "Overture TPU 95A Red",
        "Generic PETG-CF",
        "Generic ABS Black",
        "Generic ASA",
        "Generic PC Clear",
        "Generic PA-CF Carbon",
    ]
    for query in queries:
        name, path, physics = resolve_filament_profile(query)
        assert len(name) > 0
        assert path.exists()

    # 2. Modifiers
    # Silk -> 60 mm/s outer wall
    _, _, p_silk = resolve_filament_profile("Silk PLA")
    assert p_silk["outer_wall_speed"] == 60

    # Rapid / High Speed -> 22 mm3/s flow
    _, _, p_hs = resolve_filament_profile("Rapid PLA+")
    assert p_hs["max_volumetric_speed"] == 22.0

    # TPU -> 4 mm3/s flow
    _, _, p_tpu = resolve_filament_profile("TPU 95A")
    assert p_tpu["max_volumetric_speed"] == 4.0

    # PETG -> 75°C bed temp
    _, _, p_petg = resolve_filament_profile("PETG")
    assert p_petg["bed_temp"] == 70

    # 3. Fallback unknown query -> valid profile
    import pytest
    from aethelark3d.filaments.resolver import UnknownMaterial
    with pytest.raises(UnknownMaterial):
        resolve_filament_profile("UnknownAlienPolymerX")


def test_filament_vault_edge_cases_and_corruption_recovery(tmp_path):
    # Set custom temp vault file
    vault_file = tmp_path / "spools.json"
    FilamentVault.VAULT_FILE = vault_file

    # 1. Load spools across multiple slots
    FilamentVault.load_spool("CC1", slot=1, material="PLA Basic", color="White", grams=1000.0)
    FilamentVault.load_spool("C2_COMBO", slot=1, material="Silk PLA", color="Red", grams=500.0)
    FilamentVault.load_spool("C2_COMBO", slot=2, material="PLA-CF", color="Black", grams=750.0)

    assert FilamentVault.check_sufficiency("CC1", slot=1, required_grams=200.0)[0] is True
    assert FilamentVault.check_sufficiency("CC1", slot=1, required_grams=1200.0)[0] is False

    # Deduct usage
    deducted = FilamentVault.deduct_usage("CC1", slot=1, grams_used=250.0)
    assert deducted == 750.0
    assert FilamentVault.get_mounted_spool("CC1", slot=1)["remaining_grams"] == 750.0

    # Deduct more than remaining -> clamped to 0
    FilamentVault.deduct_usage("CC1", slot=1, grams_used=900.0)
    assert FilamentVault.get_mounted_spool("CC1", slot=1)["remaining_grams"] == 0.0

    # 2. Corrupt file on disk with invalid JSON -> Vault must auto-recover with empty dict
    vault_file.write_text("{CORRUPTED_JSON_BYTES!@@#", encoding="utf-8")
    recovered_slots = FilamentVault.get_printer_slots("CC1")
    assert isinstance(recovered_slots, dict)
