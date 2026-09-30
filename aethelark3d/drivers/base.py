"""
Base Printer Driver Protocol for Aethelark-3D.
Defines the polymorphic hardware interface for all printer brands.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Any


class PrinterState(str, Enum):
    IDLE = "IDLE"
    PREPARING = "PREPARING"
    HEATING = "HEATING"
    LEVELING = "LEVELING"
    PRINTING = "PRINTING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    STOPPED = "STOPPED"
    ERROR = "ERROR"
    UPLOADING = "UPLOADING"
    DISCONNECTED = "DISCONNECTED"


@dataclass
class PrinterTelemetry:
    """Normalized real-time telemetry from any printer brand."""
    printer_name: str
    brand: str
    ip_address: str
    state: PrinterState
    substate_code: Optional[int] = None
    nozzle_temp: float = 0.0
    nozzle_target: float = 0.0
    bed_temp: float = 0.0
    bed_target: float = 0.0
    chamber_temp: float = 0.0
    current_layer: int = 0
    total_layers: int = 0
    progress_percent: float = 0.0
    current_file: Optional[str] = None
    time_remaining_seconds: Optional[int] = None
    loaded_spools: Dict[str, Any] = field(default_factory=dict)
    raw_telemetry: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AMSSlotState:
    """State of an individual AMS slot."""
    slot: int
    material: str = "PLA Basic"
    color: str = "Black"
    remaining_grams: float = 1000.0
    is_active: bool = False
    has_runout: bool = False
    rfid_present: bool = False


@dataclass
class PrinterCapability:
    """Hardware capabilities and physical constraints profile."""
    brand: str
    model: str
    nozzle_diameter: float = 0.4
    nozzle_material: str = "hardened_steel"  # "hardened_steel", "brass", "ruby"
    max_nozzle_temp: float = 300.0
    max_bed_temp: float = 100.0
    is_enclosed: bool = True
    build_volume: tuple = (256, 256, 256)
    has_ams: bool = False
    ams_slot_count: int = 1
    has_chamber_light: bool = True
    has_camera: bool = True
    supported_file_extensions: List[str] = field(default_factory=lambda: [".gcode"])
    compatible_materials: List[str] = field(default_factory=lambda: [
        "PLA", "PETG", "TPU", "ABS", "ASA", "PLA-CF", "PETG-CF", "Silk PLA"
    ])
    incompatible_materials: List[str] = field(default_factory=list)


class BasePrinterDriver(ABC):
    """Abstract Polymorphic Printer Driver Interface."""

    def __init__(self, name: str, ip: str, port: int, capabilities: Optional[PrinterCapability] = None):
        self.name = name
        self.ip = ip
        self.port = port
        self.capabilities = capabilities or PrinterCapability(brand="generic", model="generic")

    @abstractmethod
    async def connect(self) -> bool:
        """Establish connection with the printer."""
        pass

    @abstractmethod
    async def disconnect(self) -> None:
        """Close printer connection."""
        pass

    @abstractmethod
    async def get_telemetry(self) -> PrinterTelemetry:
        """Fetch normalized real-time telemetry."""
        pass

    @abstractmethod
    async def upload_job(self, gcode_path: Path) -> str:
        """
        Uploads a sliced G-code / 3MF container to printer storage.
        Returns the remote path / identifier on the printer.
        """
        pass

    @abstractmethod
    async def start_print(self, remote_path: str, auto_level: bool = True) -> bool:
        """Initiate physical print execution."""
        pass

    @abstractmethod
    async def pause_print(self) -> bool:
        """Pause the current print job."""
        pass

    @abstractmethod
    async def resume_print(self) -> bool:
        """Resume the paused print job."""
        pass

    @abstractmethod
    async def stop_print(self) -> bool:
        """Stop/abort the active print job."""
        pass

    @abstractmethod
    async def set_chamber_light(self, state: bool, brightness: int = 100) -> bool:
        """Control chamber / toolhead LED lighting."""
        pass

    @abstractmethod
    async def list_files(self) -> List[Dict[str, Any]]:
        """List files currently stored in local printer memory."""
        pass

    @abstractmethod
    async def get_camera_snapshot(self) -> Optional[bytes]:
        """Fetch current camera frame for vision / inspection."""
        pass

    async def set_light(self, state: bool) -> bool:
        """Alias for set_chamber_light."""
        return await self.set_chamber_light(state)

    async def send_gcode(self, gcode: str) -> bool:
        """Send raw G-code stream to printer."""
        return True
