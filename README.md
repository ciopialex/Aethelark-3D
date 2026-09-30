# Aethelark-3D

A module for the [Aethelark](https://github.com/ciopialex/Project-Space-Eagle)
voice assistant that runs Elegoo Centauri Carbon printers: find a model, slice
it with ElegooSlicer, print it, watch the print, and keep track of filament.

You say "find me a phone stand" or "print those on CC1"; it does the searching,
downloading, slicing, uploading and printing. It also quotes what a print costs
in your electricity and filament prices, and tracks your spools. Starting or
stopping a print always asks you first.

## Install

Tick it on Aethelark's first-run screen, or add it later in
**Settings → Modules → Get**, or from a terminal:

```bash
eagle install 3d
```

The module also works on its own as the `a3d` command (`a3d --help`). It finds
printers on your local network.

## What it includes

- Search 3D model sites, download a model, or measure one already on your computer.
- Slice headlessly with ElegooSlicer and send the file to the printer.
- Live status, camera, chamber light, pause, resume and stop.
- Filament spools and a cost quote for each print.
- **PURGEX**, an option that reduces filament waste on multi-color prints from
  single-nozzle systems.

## Development

```bash
git clone https://github.com/ciopialex/Aethelark-3D
cd Aethelark-3D
python -m venv .venv && .venv/bin/pip install -e . pytest
.venv/bin/python -m pytest -q
```

Contributions: see the [Aethelark contributing guide](https://github.com/ciopialex/Project-Space-Eagle/blob/main/CONTRIBUTING.md).

## License

[PolyForm Noncommercial 1.0.0](LICENSE): free for noncommercial use, with credit;
commercial use needs a separate license. Copyright (c) 2025-2026
Alexandru-Mihai Cioponea.
