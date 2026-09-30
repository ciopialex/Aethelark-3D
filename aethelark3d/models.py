"""
Universal data models for Aethelark-3D.
Normalized across MakerWorld, Printables, Thingiverse, and Thangs.
"""

from typing import List, Optional, Dict, Any, Literal
from pydantic import BaseModel, Field


class FilamentSpec(BaseModel):
    """Filament definition with material, color, and vendor."""
    material: str = "PLA"          # e.g., PLA, PLA+, Silk PLA, PETG, TPU
    color: Optional[str] = "Black"
    brand: Optional[str] = "Generic"
    preset_file: Optional[str] = None  # JSON filename in ElegooSlicer


class UniversalProfile(BaseModel):
    """Normalized print profile (3MF instance or sliced plate)."""
    id: int
    profile_id: Optional[int] = None
    title: str = "Standard Profile"
    weight_g: Optional[float] = None
    prediction_s: Optional[int] = None
    rating_score: Optional[float] = 0.0
    download_count: Optional[int] = 0
    is_default: bool = False
    source_platform: str = "makerworld"

    @property
    def formatted_print_time(self) -> str:
        if not self.prediction_s or self.prediction_s <= 0:
            return "N/A"
        hours = self.prediction_s // 3600
        mins = (self.prediction_s % 3600) // 60
        if hours > 0:
            return f"{hours}h {mins}m"
        return f"{mins}m"

    @property
    def formatted_weight(self) -> str:
        if self.weight_g is not None and self.weight_g > 0:
            return f"{self.weight_g:.1f}g"
        return "N/A"


class UniversalDesign(BaseModel):
    """Normalized 3D project model across all platforms."""
    id: int
    title: str
    slug: Optional[str] = ""
    platform: str = "makerworld"       # 'makerworld', 'printables', 'thingiverse', 'thangs'
    url: str
    cover_url: Optional[str] = None
    summary: Optional[str] = ""
    creator_name: str = "Unknown"
    download_count: int = 0
    like_count: int = 0
    print_count: int = 0
    profiles: List[UniversalProfile] = []
    tags: List[str] = []
    license: Optional[str] = None

    @property
    def primary_profile(self) -> Optional[UniversalProfile]:
        if not self.profiles:
            return None
        for p in self.profiles:
            if p.is_default:
                return p
        return self.profiles[0]

    @property
    def min_weight_g(self) -> float:
        """Get the lowest filament weight among all profiles."""
        weights = [p.weight_g for p in self.profiles if p.weight_g and p.weight_g > 0]
        return min(weights) if weights else 999999.0

    @property
    def min_print_time_s(self) -> int:
        """Get the fastest print duration among all profiles."""
        times = [p.prediction_s for p in self.profiles if p.prediction_s and p.prediction_s > 0]
        return min(times) if times else 999999


class SearchResult(BaseModel):
    query: str
    total: int
    platform: str
    designs: List[UniversalDesign]
