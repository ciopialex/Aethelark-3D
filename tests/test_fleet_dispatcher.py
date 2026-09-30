"""
Rigorous Adaptive Stress Tests for Aethelark-3D Constraint-Aware Fleet Dispatcher.
Tests physical metallurgy gates, high-temp enclosure constraints, 4-slot AMS routing,
spool-swap minimization with FilamentVault, and heterogeneous batch execution across Digital Twins.
"""

import pytest
from pathlib import Path

from aethelark3d.dispatcher import FleetDispatcher, PrintJob, is_abrasive_material
from aethelark3d.spools import FilamentVault
from aethelark3d.drivers.base import PrinterState, PrinterTelemetry


def test_abrasive_material_nozzle_metallurgy_gating():
    """Verify abrasive filaments (PLA-CF, PA-CF, Glow) route only to hardened nozzles (CC1, CC2) and reject brass (C2_COMBO)."""
    assert is_abrasive_material("PLA-CF Black") is True
    assert is_abrasive_material("eSUN PA-CF") is True
    assert is_abrasive_material("Glow-in-the-dark PLA") is True
    assert is_abrasive_material("Standard PLA") is False

    dispatcher = FleetDispatcher(fleet_keys=["CC1", "CC2", "C2_COMBO"], use_simulator=True)

    job_cf = PrintJob(
        id="job_001",
        model_name="structural_bracket.stl",
        material="PLA-CF",
        color="Black",
        mass_grams=45.0,
        nozzle_temp=230.0,
        bed_temp=55.0
    )

    decision = dispatcher.dispatch_job(job_cf)
    assert decision.is_feasible is True
    assert decision.selected_printer in ["CC1", "CC2"]
    assert "C2_COMBO" in decision.rejection_reasons
    assert "brass nozzle" in decision.rejection_reasons["C2_COMBO"]


def test_high_temp_enclosure_and_thermal_ceiling_gating():
    """Verify high-temp engineering filaments route to enclosed machines and respect nozzle/bed limits."""
    dispatcher = FleetDispatcher(fleet_keys=["CC1", "CC2", "C2_COMBO"], use_simulator=True)

    # 1. Standard ABS (Enclosed required, Bed 95°C, Nozzle 255°C)
    job_abs = PrintJob(
        id="job_002",
        model_name="enclosure_case.stl",
        material="ABS Pro",
        color="Grey",
        mass_grams=60.0,
        nozzle_temp=255.0,
        bed_temp=95.0
    )
    dec_abs = dispatcher.dispatch_job(job_abs)
    assert dec_abs.is_feasible is True
    assert dec_abs.selected_printer in ["CC1", "CC2"]
    assert "C2_COMBO" in dec_abs.rejection_reasons
    assert "open-frame" in dec_abs.rejection_reasons["C2_COMBO"]

    # 2. Extreme High-Temp PA-CF (Nozzle 315°C, Bed 105°C) -> Only CC2 supports 320°C!
    job_pacf = PrintJob(
        id="job_003",
        model_name="aerospace_fitting.stl",
        material="Nylon PA-CF",
        color="Black",
        mass_grams=80.0,
        nozzle_temp=315.0,
        bed_temp=105.0
    )
    dec_pacf = dispatcher.dispatch_job(job_pacf)
    assert dec_pacf.is_feasible is True
    # CC1 max is 300°C -> Rejected! CC2 max is 320°C -> Accepted!
    assert dec_pacf.selected_printer == "CC2"
    assert "CC1" in dec_pacf.rejection_reasons
    assert "300.0°C" in dec_pacf.rejection_reasons["CC1"]


def test_multicolor_4slot_ams_gating():
    """Verify multi-color jobs route exclusively to AMS units (C2_COMBO)."""
    dispatcher = FleetDispatcher(fleet_keys=["CC1", "CC2", "C2_COMBO"], use_simulator=True)

    job_multicolor = PrintJob(
        id="job_004",
        model_name="art_sculpture_tricolor.3mf",
        material="PLA Basic",
        color="Multi",
        is_multicolor=True,
        colors_required=["Red", "White", "Blue"],
        mass_grams=120.0
    )

    decision = dispatcher.dispatch_job(job_multicolor)
    assert decision.is_feasible is True
    assert decision.selected_printer == "C2_COMBO"
    assert "CC1" in decision.rejection_reasons
    assert "CC2" in decision.rejection_reasons
    assert "no AMS unit" in decision.rejection_reasons["CC1"]


def test_spool_swap_minimization_with_filament_vault():
    """Verify dispatcher prioritizes machines with the exact material and color already loaded in the vault."""
    # Pre-condition: Load specific spool into CC1
    FilamentVault.load_spool("CC1", slot=1, material="Bambu Silk PLA", color="Emerald Green", grams=800.0)

    dispatcher = FleetDispatcher(fleet_keys=["CC1", "CC2", "C2_COMBO"], use_simulator=True)

    job_silk = PrintJob(
        id="job_005",
        model_name="vase.stl",
        material="Silk PLA",
        color="Emerald Green",
        mass_grams=50.0
    )

    decision = dispatcher.dispatch_job(job_silk)
    assert decision.is_feasible is True
    assert decision.selected_printer == "CC1"
    assert decision.spool_swap_required is False
    assert decision.slot_index == 1
    assert "Zero spool swap needed" in decision.rationale


def test_heterogeneous_batch_dispatching():
    """Verify optimal routing across a mixed 3-part production order."""
    dispatcher = FleetDispatcher(fleet_keys=["CC1", "CC2", "C2_COMBO"], use_simulator=True)

    batch = [
        # Part A: Multi-color figure -> C2_COMBO
        PrintJob(id="batch_1", model_name="figure.3mf", material="PLA", is_multicolor=True, colors_required=["Red", "Black"]),
        # Part B: Drone propeller arm -> CC1 (Hardened steel, PLA-CF)
        PrintJob(id="batch_2", model_name="arm.stl", material="PLA-CF", color="Black", nozzle_temp=230.0, bed_temp=55.0),
        # Part C: High-temp turbo duct -> CC2 (315°C PA-CF)
        PrintJob(id="batch_3", model_name="duct.stl", material="PA-CF", color="Black", nozzle_temp=315.0, bed_temp=105.0)
    ]

    decisions = dispatcher.dispatch_batch(batch)
    assert len(decisions) == 3
    assert all(d.is_feasible for d in decisions)

    # Validate exact fleet capability assignments
    assert decisions[0].selected_printer == "C2_COMBO"
    assert decisions[1].selected_printer in ["CC1", "CC2"]
    assert decisions[2].selected_printer == "CC2"


def test_job_economics_and_commercial_quoting():
    """Verify unit cost, power, depreciation, and margin pricing formulations."""
    job = PrintJob(
        id="quote_001",
        model_name="gearbox_housing.stl",
        material="PETG-CF",
        mass_grams=150.0,
        estimated_time_seconds=7200  # 2.0 hours
    )

    quote = FleetDispatcher.calculate_job_economics(
        job=job,
        filament_cost_per_kg=26.0,
        electricity_rate_kwh=0.15,
        printer_power_watts=120.0,
        machine_hourly_depreciation=0.50,
        target_margin_percent=65.0
    )

    # Material: 0.15kg * $26 = $3.90
    assert quote["material_cost_usd"] == 3.90
    # Electricity: (0.12 kW * 2 hr) * $0.15 = $0.036 -> $0.04
    assert quote["electricity_cost_usd"] == 0.04
    # Depreciation: 2 hr * $0.50 = $1.00
    assert quote["depreciation_cost_usd"] == 1.00
    # Total cost = $4.94
    assert quote["total_unit_cost_usd"] == 4.94
    # Selling price at 65% margin = 4.94 / (1 - 0.65) = $14.11
    assert 14.0 <= quote["recommended_selling_price_usd"] <= 14.20
    assert quote["net_profit_usd"] > 9.0
