"""
Unit tests for HardwareDoctor preflight diagnostic engine.
Tests network ping, SDCP socket heartbeat, thermal limit checks, RTSP camera probe, and offline recovery.
"""

from unittest.mock import patch
from aethelark3d.doctor import HardwareDoctor, DoctorReport
from aethelark3d.spools import FilamentVault


def test_doctor_simulated_healthy_probe():
    report: DoctorReport = HardwareDoctor.probe_printer("CC1", use_simulator=True)
    assert report.printer_key == "CC1"
    assert report.overall_status in ["HEALTHY", "HEALTHY_WITH_WARNINGS"]
    assert report.overall_score >= 0.75
    assert report.network.ping_ok is True
    assert report.thermals.nozzle_healthy is True
    assert report.thermals.bed_healthy is True
    assert report.camera.rtsp_reachable is True
    assert report.duration_ms < 500.0


def test_doctor_offline_printer_probe():
    # Probe non-existent IP with short timeout
    with patch.object(HardwareDoctor, "_test_tcp_port", return_value=(False, 0.0)):
        report = HardwareDoctor.probe_printer("CC2", timeout=0.1, use_simulator=False)
        assert report.overall_status == "OFFLINE"
        assert report.overall_score == 0.0
        assert report.network.ping_ok is False
        assert len(report.recommendations) > 0


def test_doctor_thermal_sensor_anomaly_detection():
    # Simulate a broken thermistor reading 1024°C (short circuit) or -50°C (open circuit)
    report = HardwareDoctor.probe_printer("CC1", use_simulator=True)
    # Artificially inject thermal anomaly into report checking logic
    with patch("aethelark3d.drivers.simulator.VirtualPrinterDriver.get_telemetry") as mock_telem:
        from aethelark3d.drivers.base import PrinterTelemetry, PrinterState
        mock_telem.return_value = PrinterTelemetry(
            printer_name="Centauri Carbon",
            brand="Elegoo",
            ip_address="127.0.0.1",
            state=PrinterState.ERROR,
            nozzle_temp=450.0, # Dangerous out of bounds!
            bed_temp=25.0
        )
        anomaly_report = HardwareDoctor.probe_printer("CC1", use_simulator=True)
        assert anomaly_report.thermals.nozzle_healthy is False
        assert anomaly_report.overall_status in ["DEGRADED", "OFFLINE"]
        assert any("out-of-range" in a for a in anomaly_report.thermals.anomalies)


def test_doctor_low_spool_warning():
    # Load critically low spool (< 20g)
    FilamentVault.load_spool("CC1", slot=1, material="Silk PLA", color="Gold", grams=12.0)
    report = HardwareDoctor.probe_printer("CC1", use_simulator=True)
    assert report.spools.has_low_warning is True
    assert any("critically low" in w for w in report.spools.warnings)


def test_doctor_to_dict_carries_filament_alias():
    # `to_dict()` aliases `spools` to `filament` so a consumer looking for
    # either name finds the same facts. No card currently reads `filament`
    # (Space-Eagle/docs/MODULE_CONTRACT.md's a3d field list has no such key),
    # but the alias should still resolve to what `spools` holds rather than
    # silently going missing or drifting from it.
    report = HardwareDoctor.probe_printer("CC1", use_simulator=True)
    d = report.to_dict()
    assert "filament" in d
    assert d["filament"] == d["spools"]
