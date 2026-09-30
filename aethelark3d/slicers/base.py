"""
Base Slicer Engine Protocol for Aethelark-3D.
Defines the polymorphic slicing interface for Elegoo, Orca, BambuStudio, and PrusaSlicer.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Any


@dataclass
class SliceConfig:
    """Universal slicing parameters."""
    printer_key: str = "CC1"
    machine_name: str = "Elegoo Centauri Carbon 0.4 nozzle"
    process_profile: str = "0.20mm Standard @Elegoo CC 0.4 nozzle"
    filament_query: Optional[str] = None
    ams_filaments: Optional[List[str]] = None
    layer_height: float = 0.20
    wall_loops: Optional[int] = None
    infill_percent: Optional[int] = None
    enable_supports: bool = False
    nozzle_temp: Optional[int] = None
    bed_temp: Optional[int] = None
    supports: Optional[str] = None
    enable_purgex: bool = True  # False preserves 100% untouched manufacturer default purge
    output_dir: Optional[Path] = None


@dataclass
class SliceResult:
    """Metadata result returned after slicing."""
    gcode_path: Path
    model_name: str
    filament_name: str
    filament_mass_grams: Optional[float]
    total_layers: Optional[int]
    estimated_time_seconds: Optional[int]
    raw_volume_cm3: Optional[float]
    thumbnail_paths: List[Path] = field(default_factory=list)


class BaseSlicer(ABC):
    """Abstract Slicing Engine Interface."""

    @abstractmethod
    def slice(self, model_path: Path, config: SliceConfig) -> SliceResult:
        """Slice a 3D model (STL/STEP/3MF) into machine G-code."""
        pass

    @abstractmethod
    def generate_thumbnails(self, model_path: Path, output_dir: Path, color_rgb: tuple = (230, 45, 55)) -> List[Path]:
        """Generate 512x512 RGBA and 680x510 RGB thumbnails from 3D geometry."""
        pass

    @abstractmethod
    def package_container(self, gcode_path: Path, model_path: Path, result: SliceResult) -> Path:
        """Package G-code, slice_info.config, and PNG thumbnails into compliant printer container."""
        pass
