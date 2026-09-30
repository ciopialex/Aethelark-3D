"""
Aethelark-3D Constraint-Aware Fleet Dispatcher & Spool-Optimized Routing Engine.
Routes 3D print jobs across heterogeneous fleets (CC1, CC2, C2_COMBO) by enforcing:
1. Physical metallurgy constraints (hardened steel vs brass for abrasives like CF/GF/Glow).
2. Thermal & enclosure constraints (enclosed chamber & bed temp >= 90°C for ABS/ASA/PC/PA-CF).
3. Multi-color AMS gating (routing N-color toolpaths to 4-Slot AMS units).
4. Spool swap minimization (matching mounted spools in FilamentVault).
5. Dynamic unit economics & commercial quoting calculations.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
import time

from aethelark3d.config import config
from aethelark3d.drivers.base import PrinterCapability, PrinterState, PrinterTelemetry
from aethelark3d.drivers.factory import get_driver_for_printer
from aethelark3d.spools import FilamentVault
from aethelark3d.slicers.thermodynamics import is_chamber_dependent, is_heat_creep_prone


# Abrasive and composite filaments requiring hardened steel nozzles
ABRASIVE_MATERIALS = {"cf", "carbon", "carbon fiber", "gf", "glass fiber", "glow", "wood", "marble", "metal", "stone"}


def is_abrasive_material(material: str) -> bool:
    """Detect if filament is abrasive and wears standard brass nozzles."""
    mat_clean = material.lower().replace("-", " ").replace("_", " ")
    return any(a in mat_clean.split() or a in mat_clean for a in ABRASIVE_MATERIALS)


@dataclass
class PrintJob:
    """Universal 3D print job request."""
    id: str
    model_name: str
    material: str = "PLA Basic"
    color: str = "Black"
    mass_grams: float = 25.0
    estimated_time_seconds: int = 1800  # 30 min
    is_multicolor: bool = False
    colors_required: List[str] = field(default_factory=lambda: ["Black"])
    nozzle_temp: float = 220.0
    bed_temp: float = 55.0
    model_path: Optional[Path] = None


@dataclass
class DispatchDecision:
    """Deterministic routing decision output by the Fleet Dispatcher."""
    job_id: str
    model_name: str
    selected_printer: Optional[str]
    slot_index: int = 1
    spool_swap_required: bool = False
    is_feasible: bool = True
    confidence_score: float = 0.0  # 0.0 - 1.0
    rejection_reasons: Dict[str, str] = field(default_factory=dict)
    rationale: str = ""
    quote: Optional[Dict[str, float]] = None


class FleetDispatcher:
    """
    Intelligent Constraint-Aware Fleet Dispatcher.
    Evaluates hardware capabilities, thermal limits, active spool inventories, and machine queues.
    """

    def __init__(self, fleet_keys: Optional[List[str]] = None, use_simulator: bool = True):
        self.fleet_keys = fleet_keys or list(config.printers.keys())
        self.use_simulator = use_simulator

    def evaluate_printer_for_job(
        self,
        printer_key: str,
        job: PrintJob,
        telemetry: Optional[PrinterTelemetry] = None
    ) -> Tuple[bool, float, str, Dict[str, Any]]:
        """
        Evaluates a single printer for suitability against a PrintJob.
        Returns (is_capable, score, rationale, metadata).
        """
        driver = get_driver_for_printer(printer_key, use_simulator=self.use_simulator)
        caps: PrinterCapability = driver.capabilities
        rejection_reason = ""
        score = 1.0
        spool_swap_needed = True
        matched_slot = 1

        # ---------------------------------------------------------------------
        # 1. HARD CONSTRAINT: Nozzle Metallurgy vs. Abrasive Filaments
        # ---------------------------------------------------------------------
        if is_abrasive_material(job.material):
            if caps.nozzle_material != "hardened_steel":
                return (
                    False,
                    0.0,
                    f"Rejected: Material '{job.material}' is abrasive and requires hardened steel nozzle, but {printer_key} has {caps.nozzle_material} nozzle.",
                    {"slot": 1, "swap": True}
                )
            score += 0.2  # Bonus for properly configured hardened toolhead

        # ---------------------------------------------------------------------
        # 2. HARD CONSTRAINT: Enclosure & Thermal Limits for Engineering Materials
        # ---------------------------------------------------------------------
        if is_chamber_dependent(job.material):
            if not caps.is_enclosed:
                return (
                    False,
                    0.0,
                    f"Rejected: Material '{job.material}' requires enclosed chamber to prevent warping, but {printer_key} is open-frame.",
                    {"slot": 1, "swap": True}
                )
            if job.nozzle_temp > caps.max_nozzle_temp:
                return (
                    False,
                    0.0,
                    f"Rejected: Nozzle target {job.nozzle_temp}°C exceeds {printer_key} max {caps.max_nozzle_temp}°C.",
                    {"slot": 1, "swap": True}
                )
            if job.bed_temp > caps.max_bed_temp:
                return (
                    False,
                    0.0,
                    f"Rejected: Bed target {job.bed_temp}°C exceeds {printer_key} max {caps.max_bed_temp}°C.",
                    {"slot": 1, "swap": True}
                )

        # ---------------------------------------------------------------------
        # 3. HARD CONSTRAINT: Multi-Color / Multi-Material AMS
        # ---------------------------------------------------------------------
        color_count = max(len(job.colors_required), 1 if not job.is_multicolor else 2)
        if job.is_multicolor or color_count > 1:
            if not caps.has_ams:
                return (
                    False,
                    0.0,
                    f"Rejected: Job requires multi-color AMS ({color_count} colors), but {printer_key} has no AMS unit.",
                    {"slot": 1, "swap": True}
                )
            if caps.ams_slot_count < color_count:
                return (
                    False,
                    0.0,
                    f"Rejected: Job requires {color_count} colors, but {printer_key} AMS only has {caps.ams_slot_count} slots.",
                    {"slot": 1, "swap": True}
                )
            score += 0.3

        # ---------------------------------------------------------------------
        # 4. INVENTORY MATCHING: Spool-Swap Minimization via FilamentVault
        # ---------------------------------------------------------------------
        active_slots = FilamentVault.get_printer_slots(printer_key)
        matched_material_slot = None
        exact_color_match = False

        if active_slots:
            job_mat_clean = job.material.lower()
            job_col_clean = job.color.lower()

            for s_k, s_data in active_slots.items():
                slot_num = int(s_k.replace("slot_", ""))
                s_mat = s_data.get("material", "").lower()
                s_col = s_data.get("color", "").lower()
                rem_g = s_data.get("remaining_grams", 0.0)

                # Check material match
                if (s_mat in job_mat_clean or job_mat_clean in s_mat) and rem_g >= (job.mass_grams * 1.05):
                    matched_material_slot = slot_num
                    if s_col == job_col_clean:
                        exact_color_match = True
                        matched_slot = slot_num
                        spool_swap_needed = False
                        break

            if exact_color_match:
                score += 0.5  # Significant bonus: Zero spool swap required!
            elif matched_material_slot is not None:
                score += 0.2
                matched_slot = matched_material_slot

        # ---------------------------------------------------------------------
        # 5. MACHINE STATE & QUEUE LOAD BALANCING
        # ---------------------------------------------------------------------
        if telemetry:
            if telemetry.state in [PrinterState.IDLE, PrinterState.COMPLETED]:
                score += 0.4
            elif telemetry.state == PrinterState.PRINTING:
                # Deduct based on time remaining
                rem_time = telemetry.time_remaining_seconds or 3600
                score -= min(0.3, rem_time / 7200.0)
            elif telemetry.state in [PrinterState.ERROR, PrinterState.DISCONNECTED]:
                score -= 0.8

        rationale = (
            f"Compatible (Score: {score:.2f}). "
            f"{'Zero spool swap needed (Active in Slot ' + str(matched_slot) + ')' if not spool_swap_needed else 'Requires loading ' + job.material + ' ' + job.color}."
        )

        return (True, score, rationale, {"slot": matched_slot, "swap": spool_swap_needed})

    def dispatch_job(self, job: PrintJob, telemetry_map: Optional[Dict[str, PrinterTelemetry]] = None) -> DispatchDecision:
        """
        Dispatches a single PrintJob to the highest-scoring compatible machine in the fleet.
        """
        candidates: List[Tuple[str, float, str, Dict[str, Any]]] = []
        rejections: Dict[str, str] = {}

        for pkey in self.fleet_keys:
            telem = telemetry_map.get(pkey) if telemetry_map else None
            is_capable, score, rationale, meta = self.evaluate_printer_for_job(pkey, job, telemetry=telem)
            if is_capable:
                candidates.append((pkey, score, rationale, meta))
            else:
                rejections[pkey] = rationale

        if not candidates:
            return DispatchDecision(
                job_id=job.id,
                model_name=job.model_name,
                selected_printer=None,
                is_feasible=False,
                rejection_reasons=rejections,
                rationale=f"No compatible machine in fleet ({', '.join(self.fleet_keys)}) meets all physical constraints.",
                quote=self.calculate_job_economics(job)
            )

        # Sort candidate printers by suitability score descending
        candidates.sort(key=lambda c: c[1], reverse=True)
        best_printer, best_score, best_rationale, best_meta = candidates[0]

        return DispatchDecision(
            job_id=job.id,
            model_name=job.model_name,
            selected_printer=best_printer,
            slot_index=best_meta["slot"],
            spool_swap_required=best_meta["swap"],
            is_feasible=True,
            confidence_score=round(best_score, 2),
            rejection_reasons=rejections,
            rationale=best_rationale,
            quote=self.calculate_job_economics(job)
        )

    def dispatch_batch(self, jobs: List[PrintJob]) -> List[DispatchDecision]:
        """
        Dispatches a batch of heterogeneous print jobs across the fleet.
        """
        decisions = []
        for j in jobs:
            dec = self.dispatch_job(j)
            decisions.append(dec)
        return decisions

    @staticmethod
    def calculate_job_economics(
        job: PrintJob,
        filament_cost_per_kg: float = 22.0,
        electricity_rate_kwh: float = 0.15,
        printer_power_watts: float = 120.0,
        machine_hourly_depreciation: float = 0.40,
        target_margin_percent: float = 65.0
    ) -> Dict[str, float]:
        """
        First-Principles Dynamic Unit Economics & Commercial Quoting Engine.
        Cost = Material + Electricity + Machine Depreciation
        Minimum Selling Price = Cost / (1 - Margin/100)
        """
        # Material cost
        mat_cost = (job.mass_grams / 1000.0) * filament_cost_per_kg

        # Electricity cost (Joules / kWh)
        hours = job.estimated_time_seconds / 3600.0
        kwh = (printer_power_watts / 1000.0) * hours
        elec_cost = kwh * electricity_rate_kwh

        # Depreciation cost
        deprec_cost = hours * machine_hourly_depreciation

        total_cost = mat_cost + elec_cost + deprec_cost
        margin_factor = max(0.1, 1.0 - (target_margin_percent / 100.0))
        selling_price = total_cost / margin_factor
        net_profit = selling_price - total_cost

        return {
            "material_cost_usd": round(mat_cost, 2),
            "electricity_cost_usd": round(elec_cost, 2),
            "depreciation_cost_usd": round(deprec_cost, 2),
            "total_unit_cost_usd": round(total_cost, 2),
            "target_margin_percent": target_margin_percent,
            "recommended_selling_price_usd": round(selling_price, 2),
            "net_profit_usd": round(net_profit, 2)
        }
