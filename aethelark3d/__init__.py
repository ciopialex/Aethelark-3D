"""Aethelark-3D: Universal 3D Model Automation & Slicer Handoff Engine.

The public names below are imported LAZILY (PEP 562). Importing the package —
which happens for `a3d register`, for `import aethelark3d.register`, and for the
host bus loading the module — no longer drags curl_cffi, numpy and the slicer
just to reach `config`. Each name loads its submodule only on first access and
is then cached as a plain attribute. This is what lets a bare `register` stop
pulling the whole runtime. Keep this file import-free of heavy submodules.
"""

import importlib

__version__ = "1.1.0"

# public name -> the submodule that defines it
_LAZY = {
    "search_models": "aethelark3d.api",
    "download_model": "aethelark3d.api",
    "find_and_prepare_print": "aethelark3d.api",
    "get_agent_tools": "aethelark3d.agent",
    "execute_tool": "aethelark3d.agent",
    "get_gemini_tools": "aethelark3d.agent",
    "execute_gemini_tool": "aethelark3d.agent",
    "FleetDispatcher": "aethelark3d.dispatcher",
    "PrintJob": "aethelark3d.dispatcher",
    "DispatchDecision": "aethelark3d.dispatcher",
    "resolve_filament_profile": "aethelark3d.filaments.catalog",
    "config": "aethelark3d.config",
}

__all__ = list(_LAZY)


def __getattr__(name):
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(target), name)
    globals()[name] = value  # cache: subsequent access is a plain attribute
    return value


def __dir__():
    return sorted(set(globals()) | set(_LAZY))
