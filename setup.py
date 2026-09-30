"""
Aethelark-3D setup script.
"""

from setuptools import setup, find_packages

setup(
    name="aethelark3d",
    version="1.1.0",
    description="⚡ Aethelark-3D: Universal 3D Model Automation & Slicer Handoff Engine",
    author="Aethelark",
    packages=find_packages(),
    # find_packages() ships only .py files. The module socket the eagle reads
    # (module/manifest.toml + module/island/) and the filament presets the
    # slicer resolves are DATA, and must ride inside the wheel so a plain
    # `pip install git+... && a3d register` carries them. Verified by building
    # the wheel and listing it — not assumed. Kept in step with MANIFEST.in.
    include_package_data=True,
    package_data={
        "aethelark3d": [
            "module/manifest.toml",
            "module/island/*",
            "filaments/presets/*.json",
        ],
    },
    # Every third-party module a3d imports at runtime. The five below were
    # imported but never declared: a clean `pip install` therefore produced a
    # broken module — no thumbnails (Pillow), no rasteriser (numpy), no CC2
    # driver (pycentauri), no SDCP/HTTP printer transport (requests,
    # websockets). It only worked in the dev checkout, which has them installed
    # globally. Found by installing the wheel into a clean venv, not by reading.
    install_requires=[
        "typer>=0.9.0",
        "rich>=13.0.0",
        "curl_cffi>=0.7.0",
        "beautifulsoup4>=4.12.0",
        "pydantic>=2.0.0",
        "playwright>=1.40.0",
        "Pillow>=10.0.0",
        "numpy>=1.24.0",
        "pycentauri>=0.9.1",
        "requests>=2.28.0",
        "websockets>=12.0",
    ],
    entry_points={
        "console_scripts": [
            "aethelark-3d=aethelark3d.cli:main",
            "a3d=aethelark3d.cli:main",
        ],
    },
    python_requires=">=3.8",
)
