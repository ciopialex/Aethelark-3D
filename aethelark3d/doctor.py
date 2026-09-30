"""
Aethelark-3D Hardware Preflight Doctor Probe.
Ultra-fast (<200ms) hardware diagnostics for Centauri Series 3D printers.
Probes:
1. TCP Network & WebSocket port availability.
2. SDCP Protocol mainboard responsiveness (Cmd 0).
3. Thermal sensor sanity (checks for short/open thermistors on Nozzle/Bed/Chamber).
4. Camera RTSP / Snapshot feed accessibility.
5. Active FilamentVault AMS slot sufficiency.
"""

import socket
import time
import json
import asyncio
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple

from aethelark3d.config import config
from aethelark3d.spools import FilamentVault
from aethelark3d.drivers.factory import get_driver_for_printer
from aethelark3d.drivers.base import PrinterTelemetry, PrinterState


@dataclass
class NetworkDiagnostic:
    ping_ok: bool = False
    latency_ms: float = 0.0
    http_port_ok: bool = False
    sdcp_port_ok: bool = False
    details: str = ""


@dataclass
class ThermalDiagnostic:
    nozzle_temp_c: float = 0.0
    nozzle_target_c: float = 0.0
    nozzle_healthy: bool = True
    bed_temp_c: float = 0.0
    bed_target_c: float = 0.0
    bed_healthy: bool = True
    chamber_temp_c: float = 0.0
    chamber_healthy: bool = True
    anomalies: List[str] = field(default_factory=list)


@dataclass
class CameraDiagnostic:
    rtsp_reachable: bool = False
    rtsp_url: str = ""
    snapshot_reachable: bool = False
    snapshot_url: str = ""
    details: str = ""


@dataclass
class SpoolDiagnostic:
    loaded_spools: Dict[str, Any] = field(default_factory=dict)
    has_low_warning: bool = False
    warnings: List[str] = field(default_factory=list)


@dataclass
class DoctorReport:
    printer_key: str
    printer_name: str
    host: str
    timestamp: float
    duration_ms: float
    overall_status: str  # "HEALTHY", "DEGRADED", "OFFLINE"
    overall_score: float  # 0.0 to 1.0
    network: NetworkDiagnostic = field(default_factory=NetworkDiagnostic)
    thermals: ThermalDiagnostic = field(default_factory=ThermalDiagnostic)
    camera: CameraDiagnostic = field(default_factory=CameraDiagnostic)
    spools: SpoolDiagnostic = field(default_factory=SpoolDiagnostic)
    recommendations: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["filament"] = d.get("spools", {})
        return d


class HardwareDoctor:
    """
    Hardware diagnostic probe for Elegoo Centauri Carbon, CC2, and C2_COMBO.
    """

    @classmethod
    def _test_tcp_port(cls, host: str, port: int, timeout_s: float = 0.15) -> Tuple[bool, float]:
        """Test TCP socket connection and measure latency in milliseconds."""
        start = time.perf_counter()
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout_s)
            result = sock.connect_ex((host, port))
            sock.close()
            latency = (time.perf_counter() - start) * 1000.0
            return (result == 0, latency)
        except Exception:
            return (False, 0.0)

    @classmethod
    def probe_printer(
        cls,
        printer_key: str = "",
        timeout: float = 3.5,
        use_simulator: bool = False
    ) -> DoctorReport:
        """
        Execute sub-second preflight diagnosis across all subsystems.
        """
        start_time = time.perf_counter()
        pkey = (printer_key or config.default_printer).upper().replace("-", "_")
        pinfo = config.get_printer(pkey) or {}
        pname = pinfo.get("name", f"Printer {pkey}")
        host = pinfo.get("host", "127.0.0.1")

        is_sim = use_simulator or pkey.startswith("VIRTUAL_") or pkey in ["SIM", "VIRTUAL"]
        recommendations: List[str] = []
        anomalies: List[str] = []

        # ---------------------------------------------------------------------
        # 1. NETWORK PROBE
        # ---------------------------------------------------------------------
        driver = get_driver_for_printer(pkey, use_simulator=is_sim)
        is_cc2 = getattr(driver, "is_cc2_family", False)

        if is_sim:
            net_diag = NetworkDiagnostic(
                ping_ok=True,
                latency_ms=0.5,
                http_port_ok=True,
                sdcp_port_ok=True,
                details="Connected to Digital Twin loopback simulator."
            )
        else:
            http_ok, http_lat = cls._test_tcp_port(host, 80, timeout_s=min(0.4, timeout))
            port_to_test = 1883 if is_cc2 else 3030
            sdcp_ok, sdcp_lat = cls._test_tcp_port(host, port_to_test, timeout_s=min(0.4, timeout))
            ping_ok = http_ok or sdcp_ok
            avg_lat = (http_lat + sdcp_lat) / 2.0 if (http_ok and sdcp_ok) else (http_lat if http_ok else sdcp_lat)

            if not ping_ok:
                net_diag = NetworkDiagnostic(
                    ping_ok=False,
                    latency_ms=0.0,
                    http_port_ok=False,
                    sdcp_port_ok=False,
                    details=f"Host {host} is unreachable. Check power and WiFi connection."
                )
                recommendations.append(f"Ensure {pname} is powered on and assigned IP '{host}'.")
            else:
                net_diag = NetworkDiagnostic(
                    ping_ok=True,
                    latency_ms=round(avg_lat, 2),
                    http_port_ok=http_ok,
                    sdcp_port_ok=sdcp_ok,
                    details=f"Network online ({avg_lat:.1f}ms latency)."
                )

        # ---------------------------------------------------------------------
        # 2. DRIVER & THERMAL TELEMETRY PROBE
        # ---------------------------------------------------------------------
        async def fetch_telemetry_safe() -> PrinterTelemetry:
            if not is_cc2:
                await driver.connect()
            return await driver.get_telemetry()

        try:
            telemetry: PrinterTelemetry = asyncio.run(
                asyncio.wait_for(fetch_telemetry_safe(), timeout=max(3.5, timeout))
            )
        except Exception as e:
            telemetry = PrinterTelemetry(
                printer_name=pname,
                brand=pinfo.get("brand", "Elegoo"),
                ip_address=host,
                state=PrinterState.DISCONNECTED,
                raw_telemetry={"error": str(e)}
            )

        # Thermal sanity bounds
        nozzle = telemetry.nozzle_temp
        nozzle_tgt = telemetry.nozzle_target
        bed = telemetry.bed_temp
        bed_tgt = telemetry.bed_target
        chamber = telemetry.chamber_temp

        nozzle_ok = True
        bed_ok = True
        chamber_ok = True

        if telemetry.state == PrinterState.DISCONNECTED:
            nozzle_ok = False
            bed_ok = False
            chamber_ok = False
            anomalies.append("Mainboard offline: Unable to read thermistor telemetry.")
        else:
            # Check thermistor open/short circuit limits
            if nozzle < 5.0 or nozzle > 350.0:
                nozzle_ok = False
                anomalies.append(f"Nozzle thermistor reading out-of-range ({nozzle:.1f}°C). Check hotend wiring.")
                recommendations.append("Inspect hotend thermistor connector on printhead breakout board.")

            # On CC2, bed temperature may be omitted from partial MQTT deltas if unchanged
            if bed == 0.0 and is_cc2 and nozzle_ok and telemetry.state == PrinterState.PRINTING:
                bed_ok = True
            elif bed < 5.0 or bed > 125.0:
                bed_ok = False
                anomalies.append(f"Bed thermistor reading out-of-range ({bed:.1f}°C). Check heated bed cable.")
                recommendations.append("Check heated bed cable harness underneath Y-axis bed carriage.")

            if chamber < 0.0 or chamber > 85.0:
                chamber_ok = False
                anomalies.append(f"Chamber thermistor reading anomaly ({chamber:.1f}°C).")

        thermal_diag = ThermalDiagnostic(
            nozzle_temp_c=round(nozzle, 1),
            nozzle_target_c=round(nozzle_tgt, 1),
            nozzle_healthy=nozzle_ok,
            bed_temp_c=round(bed, 1),
            bed_target_c=round(bed_tgt, 1),
            bed_healthy=bed_ok,
            chamber_temp_c=round(chamber, 1),
            chamber_healthy=chamber_ok,
            anomalies=anomalies
        )

        # ---------------------------------------------------------------------
        # 3. CAMERA RTSP / HTTP PROBE
        # ---------------------------------------------------------------------
        rtsp_url = f"rtsp://{host}:554/live"
        snapshot_url = f"http://{host}:8080/?action=snapshot"
        
        if is_sim:
            cam_diag = CameraDiagnostic(
                rtsp_reachable=True,
                rtsp_url=rtsp_url,
                snapshot_reachable=True,
                snapshot_url=snapshot_url,
                details="Simulated 1080p RTSP Chamber Stream Active"
            )
        elif not net_diag.ping_ok:
            cam_diag = CameraDiagnostic(
                rtsp_reachable=False,
                rtsp_url=rtsp_url,
                snapshot_reachable=False,
                snapshot_url=snapshot_url,
                details="Camera unreachable (host offline)"
            )
        else:
            rtsp_ok, _ = cls._test_tcp_port(host, 554, timeout_s=min(0.2, timeout))
            snap_ok, _ = cls._test_tcp_port(host, 8080, timeout_s=min(0.2, timeout))
            cam_diag = CameraDiagnostic(
                rtsp_reachable=rtsp_ok,
                rtsp_url=rtsp_url,
                snapshot_reachable=snap_ok,
                snapshot_url=snapshot_url,
                details="Chamber Camera Online" if (rtsp_ok or snap_ok) else "Camera stream port (554/8080) inactive"
            )
            if not (rtsp_ok or snap_ok):
                recommendations.append("Camera stream not detected. Enable camera in touchscreen settings if available.")

        # ---------------------------------------------------------------------
        # 4. SPOOL VAULT DIAGNOSTIC
        # ---------------------------------------------------------------------
        slots = FilamentVault.get_printer_slots(pkey)
        spool_warnings: List[str] = []
        has_low = False

        if not slots:
            spool_warnings.append("No active spools mapped in FilamentVault. Run 'a3d spool --load'.")
        else:
            for s_name, s_data in slots.items():
                rem = s_data.get("remaining_grams", 1000.0)
                mat = s_data.get("material", "Unknown")
                col = s_data.get("color", "Unknown")
                if rem < 25.0:
                    has_low = True
                    spool_warnings.append(f"Slot {s_name.upper()} ({mat} {col}) is critically low ({rem:.1f}g remaining < 25g).")
                    recommendations.append(f"Swap {s_name.upper()} spool ({mat} {col}) to prevent mid-print runout.")

        spool_diag = SpoolDiagnostic(
            loaded_spools=slots,
            has_low_warning=has_low,
            warnings=spool_warnings
        )

        # ---------------------------------------------------------------------
        # 5. OVERALL STATUS CALCULATION
        # ---------------------------------------------------------------------
        score = 1.0
        if not net_diag.ping_ok or telemetry.state == PrinterState.DISCONNECTED:
            overall_status = "OFFLINE"
            score = 0.0
        elif not nozzle_ok or not bed_ok:
            overall_status = "DEGRADED"
            score = 0.4
        elif has_low or not cam_diag.rtsp_reachable:
            overall_status = "HEALTHY_WITH_WARNINGS" if not has_low else "DEGRADED"
            score = 0.85 if not has_low else 0.75
        else:
            overall_status = "HEALTHY"
            score = 1.0

        if not recommendations and overall_status == "HEALTHY":
            recommendations.append("Printer is 100% preflight verified and ready for autonomous dispatch.")

        duration_ms = (time.perf_counter() - start_time) * 1000.0

        return DoctorReport(
            printer_key=pkey,
            printer_name=pname,
            host=host,
            timestamp=time.time(),
            duration_ms=round(duration_ms, 1),
            overall_status=overall_status,
            overall_score=round(score, 2),
            network=net_diag,
            thermals=thermal_diag,
            camera=cam_diag,
            spools=spool_diag,
            recommendations=recommendations
        )
