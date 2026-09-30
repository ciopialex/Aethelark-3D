"""
Abstract Base Provider for Aethelark-3D repositories.
Standard interface implemented by MakerWorld, Printables, Thingiverse, and Thangs.
"""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import List, Optional, Tuple, Callable
from ..models import UniversalDesign, SearchResult


class LoginRequired(RuntimeError):
    """The site refused because nobody is signed in to it.

    `site` names the account, as the manifest's [[accounts]] declares it, so
    the eagle can hand over the sign-in from its own browser and try again.
    """

    def __init__(self, message: str, site: str):
        super().__init__(message)
        self.site = site


class BaseProvider(ABC):
    """Abstract base class for all 3D model platform providers."""

    @property
    @abstractmethod
    def platform_name(self) -> str:
        """Name of the platform (e.g. 'makerworld', 'printables', 'thingiverse')."""
        pass

    @abstractmethod
    def search(self, query: str, page: int = 1, limit: int = 20) -> SearchResult:
        """Search for models on this platform."""
        pass

    @abstractmethod
    def get_design(self, id_or_url: str) -> UniversalDesign:
        """Fetch full metadata and print profiles for a single design."""
        pass

    @abstractmethod
    def get_download_url(self, design_id: int, profile_id: Optional[int] = None, raw_stl: bool = False) -> Tuple[str, str]:
        """Resolve signed download URL and filename."""
        pass

    @abstractmethod
    def download_file(
        self,
        download_url: str,
        dest_path: Path,
        progress_callback: Optional[Callable[[int, int], None]] = None
    ) -> Path:
        """Stream download file to destination path."""
        pass
