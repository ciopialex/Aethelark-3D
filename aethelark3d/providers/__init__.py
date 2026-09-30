"""
Providers registry for Aethelark-3D.
"""

from typing import Dict
from .base import BaseProvider
from .makerworld import MakerWorldProvider

PROVIDERS: Dict[str, BaseProvider] = {
    "makerworld": MakerWorldProvider(),
}


def get_provider(name: str = "makerworld") -> BaseProvider:
    provider = PROVIDERS.get(name.lower())
    if not provider:
        raise ValueError(f"Unknown provider '{name}'. Available: {list(PROVIDERS.keys())}")
    return provider
