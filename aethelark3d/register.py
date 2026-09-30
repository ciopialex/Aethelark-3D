"""Install the a3d module socket by COPYING the shipped manifest and island
assets, never by re-rendering them.

A module's socket is two things the host reads: a `manifest.toml` (the tools the
model may call) and, next to it, an `island/` directory (the Dynamic Island
cards). Both are hand-authored data, not derivable from code, so registration is
a file copy. Regenerating a manifest from code is how the sibling module
(Aethelark-Trade) once silently deleted its `[island]` cards on every install —
the renderer emitted no island section. a3d avoids that trap by construction:
its manifest's `[island]` names no cards or files, and its cards are found by
DIRECTORY CONVENTION — the host reads `island/template.html` next to the
manifest (Space-Eagle bus.py defaults the template name and looks in
`<manifest>/../island/`). Ship the assets, copy them, verify them.

The bus scans `~/.aethelark/modules` before its own bundled directory and keeps
the first manifest per key, preferring `<key>/manifest.toml` over `<key>.toml`
(the sorted-glob rule in the host's loader). That is the path written here —
`<modules_dir>/a3d/manifest.toml`, never `a3d.toml`.

This module is the reference shape for the Aethelark module standard: any module
that ships `<package>/module/{manifest.toml, island/}` and an equivalent
`register()` installs the same way. Keep the two implementations in step.
"""

from __future__ import annotations

import shutil
from pathlib import Path

MODULE_KEY = "a3d"
PACKAGE = "aethelark3d"
DEFAULT_MODULES_DIR = Path.home() / ".aethelark" / "modules"

#: Card assets the host finds by directory convention, not by the manifest.
#: The template is what bus.island_fields() reads to know a card's slots; a
#: register that "succeeds" without it installs a socket that draws nothing.
CONVENTION_ISLAND_ENTRYPOINT = "template.html"


class RegistrationError(RuntimeError):
    """Raised rather than leaving a half-installed module socket behind."""


def shipped_module_dir() -> Path:
    """The `module/` payload as installed — wheel or editable checkout alike."""
    from importlib.resources import files

    path = Path(str(files(PACKAGE) / "module"))
    if not (path / "manifest.toml").is_file():
        raise RegistrationError(
            f"packaged manifest missing at {path / 'manifest.toml'} — the wheel "
            f"was built without {PACKAGE}/module (check package_data / MANIFEST.in)")
    return path


def register(modules_dir: Path | str | None = None) -> Path:
    """Copy the shipped manifest and island assets into the modules directory.

    Returns the manifest path the bus will load. Existing files are replaced;
    the copy is verified before it is reported as installed. Idempotent: running
    it twice installs the same bytes and loses nothing.
    """
    import tomllib

    source = shipped_module_dir()
    target_dir = Path(modules_dir) if modules_dir else DEFAULT_MODULES_DIR
    target = target_dir / MODULE_KEY
    target.mkdir(parents=True, exist_ok=True)

    manifest_src = source / "manifest.toml"
    body = manifest_src.read_text(encoding="utf-8")

    # Parse before writing: a manifest that fails to load takes the module
    # offline with one confusing log line at the eagle's next boot.
    parsed = tomllib.loads(body)
    if not parsed.get("key") or not parsed.get("binary"):
        raise RegistrationError("shipped manifest is missing key/binary")
    if parsed.get("key") != MODULE_KEY:
        raise RegistrationError(
            f"shipped manifest key {parsed.get('key')!r} is not {MODULE_KEY!r}")
    if not (parsed.get("tools") or ()):
        raise RegistrationError("shipped manifest declares no tools")

    (target / "manifest.toml").write_text(body, encoding="utf-8")

    # Copy the whole island directory verbatim. This carries both kinds of
    # module: one whose cards are named in a manifest [island] section, and one
    # (a3d) whose cards are found by directory convention. Copy, never render.
    island_src = source / "island"
    copied_assets: list[str] = []
    if island_src.is_dir():
        island_dst = target / "island"
        island_dst.mkdir(exist_ok=True)
        for asset in sorted(island_src.iterdir()):
            if asset.is_file():
                shutil.copy2(asset, island_dst / asset.name)
                copied_assets.append(asset.name)

    # Ship the card assets or fail loudly — never install a socket that draws a
    # blank card. Two independent checks, one per module shape:
    #   1. Every card FILE the manifest names must exist next to it.
    #   2. If the package ships an island/ directory, its convention entrypoint
    #      (template.html) must have landed — that is what a3d's host reads.
    for card in (parsed.get("island") or {}).get("cards", {}).values():
        name = card.get("file")
        if name and not (target / "island" / name).is_file():
            raise RegistrationError(
                f"manifest names island card {name!r} but it was not shipped")
    if island_src.is_dir() and (island_src / CONVENTION_ISLAND_ENTRYPOINT).is_file():
        if not (target / "island" / CONVENTION_ISLAND_ENTRYPOINT).is_file():
            raise RegistrationError(
                f"island/{CONVENTION_ISLAND_ENTRYPOINT} was shipped but did not "
                f"install — the module would draw nothing")

    return target / "manifest.toml"


def registration_summary(manifest_path: Path) -> tuple[list[str], int]:
    """Tool names and island-card count of an installed manifest.

    a3d declares no [island] cards (its cards are convention assets), so the
    count is 0 and the socket still draws — its `island/` directory is beside
    the manifest. The count is the manifest's own declaration, not a claim
    about how many cards render.
    """
    import tomllib

    parsed = tomllib.loads(Path(manifest_path).read_text(encoding="utf-8"))
    names = [t["name"] for t in (parsed.get("tools") or ())]
    cards = len((parsed.get("island") or {}).get("cards", {}))
    return names, cards
