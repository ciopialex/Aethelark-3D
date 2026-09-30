"""
Aethelark-3D Python API.
Turnkey Python interface designed for Voice Assistants and AI Agents (Gemini 2.0 Flash).
"""

import contextlib
from pathlib import Path
from typing import Optional, List, Dict, Any, Literal
from .config import config
from .models import UniversalDesign, SearchResult
from .providers import get_provider
from .providers.base import LoginRequired
from .slicers.elegoo import slice_headless, open_gui


def search_models(
    query: str,
    platform: str = "makerworld",
    limit: int = 10,
    sort_by: Literal["default", "downloads", "likes", "least_material", "fastest"] = "default"
) -> List[Dict[str, Any]]:
    """
    Search 3D model repositories with intelligent sorting metrics.
    """
    prov = get_provider(platform)
    res = prov.search(query, limit=limit)
    designs = list(res.designs)

    if sort_by == "downloads":
        designs.sort(key=lambda d: d.download_count, reverse=True)
    elif sort_by == "likes":
        designs.sort(key=lambda d: d.like_count, reverse=True)
    elif sort_by == "least_material":
        designs.sort(key=lambda d: d.min_weight_g)
    elif sort_by == "fastest":
        designs.sort(key=lambda d: d.min_print_time_s)

    output = []
    for d in designs[:limit]:
        p = d.primary_profile
        output.append({
            "id": d.id,
            "title": d.title,
            "creator": d.creator_name,
            "platform": d.platform,
            "downloads": d.download_count,
            "likes": d.like_count,
            "weight": p.formatted_weight if p else "N/A",
            "print_time": p.formatted_print_time if p else "N/A",
            "url": d.url,
            # The repository's own thumbnail. It comes back with the search, at
            # no cost — the deck draws this while browsing, so no model is
            # downloaded until the user picks one. See browse_models.
            "cover_url": small_cover(getattr(d, "cover_url", None)),
        })
    return output


def small_cover(url: Optional[str], width: int = 480) -> Optional[str]:
    """MakerWorld serves the creator's full-size original (4.5 MB, 8 s over
    Wi-Fi, measured); its image host resizes on request (14 KB, 0.7 s)."""
    if not url or "bblmw.com" not in url or "x-oss-process" in url:
        return url
    return f"{url}{'&' if '?' in url else '?'}x-oss-process=image/resize,w_{width}"


def _count_label(n: Any) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return ""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
    if n >= 1000:
        return f"{n / 1000:.1f}k".replace(".0k", "k")
    return str(n)


def _describe(result: Dict[str, Any],
              printer: Optional[str] = None) -> Dict[str, Any]:
    """Attach the shape and size of what was just downloaded.

    The card in the Dynamic Island has always been able to draw a mesh and
    print a bounding box; until now nothing on the request/response path ever
    handed it either, so it fell back to a calibration cube and three dashes.
    `detected_event` in downloads.py already built exactly this payload for the
    file-watcher, which nothing runs. This is the same two facts, on the path
    the model actually calls.

    Both are best-effort by design: `measure` and `preview_mesh` return None
    for a format they cannot read (a STEP body, a G-code file), and a missing
    key leaves the card showing dashes rather than a number nothing produced.
    """
    from .downloads import choose_printer, measure, preview_mesh
    from .slice_info import humanise, read_slice_info

    path = Path(result.get("file_path") or "")
    if not path.is_file():
        return result

    dimensions = measure(path)
    if dimensions:
        result["dimensions"] = dimensions
    preview = preview_mesh(path)
    if preview:
        # Underscore-prefixed: the module bus strips these from what the model
        # hears and keeps them for the island. Only `browse` did this rename,
        # so a plain download pushed ~12KB of base64 into the model's context
        # for geometry it cannot use.
        result["_preview"] = preview

    # What the slicer already worked out. A .3mf saved after slicing carries
    # its own print time, filament weight, material and colour, per plate --
    # measured across this machine's library, about a quarter of them do.
    # Reading it costs milliseconds and is the creator's ground truth rather
    # than our estimate. A file nobody sliced returns nothing, and the card
    # draws a dash, which is the truth about what is known.
    sliced = read_slice_info(path)
    if sliced:
        eta = humanise(sliced["eta_seconds"])
        if eta:
            result["eta"] = eta
            result["eta_seconds"] = sliced["eta_seconds"]
        if sliced["grams"]:
            result["filament_grams"] = sliced["grams"]
        if sliced["plate_count"] > 1:
            result["plate_count"] = sliced["plate_count"]
        # Underscore-prefixed for the same reason as the mesh: the island wants
        # every plate, and the model does not need a per-plate breakdown read
        # aloud. The totals above are what it hears.
        result["_plates"] = sliced["plates"]
        first = next((pl for pl in sliced["plates"] if pl.get("filament_type")), None)
        if first:
            result["filament_type"] = first["filament_type"]
            if first.get("filament_color"):
                result["filament_color"] = first["filament_color"]

    # Which machine this is for. An explicit choice is the user's and is
    # echoed back untouched; without one, the same fit rule the watcher uses.
    if printer:
        result["printer"] = printer
    else:
        try:
            chosen = choose_printer(dimensions, config.get_fleet())
        except Exception:
            chosen = None
        if chosen:
            result["printer"] = chosen
    return result


#: Folders a person actually keeps downloaded models in. Checked in order; the
#: first match wins. Downloads first because that is where a browser and most
#: "download" phrasing land.
def _local_model_dirs() -> List[Path]:
    home = Path.home()
    dirs = [home / "Downloads", home / "Desktop", home / "Documents", home]
    try:
        dirs.append(Path(config.download_dir))
    except Exception:
        pass
    # Any folder the user keeps prints in, named the obvious ways — so "I think
    # it's in the 3D_Prints dir" or "in my Models folder" resolves without the
    # user spelling out a path. One level under home, matched by keyword.
    import re
    keyword = re.compile(r"3d|print|model|stl|mesh|slic", re.IGNORECASE)
    try:
        for child in home.iterdir():
            if child.is_dir() and not child.name.startswith(".") and keyword.search(child.name):
                dirs.append(child)
    except OSError:
        pass
    seen, out = set(), []
    for d in dirs:
        if d.is_dir() and d not in seen:
            seen.add(d)
            out.append(d)
    return out


def _normalise(name: str) -> str:
    """Fold spaces, underscores, dashes and case so 'YES_NO_DICE',
    'yes no dice' and 'yes-no-dice' all match the same file."""
    return "".join(ch for ch in name.lower() if ch.isalnum())


def _find_local_model(query: str) -> Optional[Path]:
    """A .stl/.3mf already on disk whose name matches the query, or None.

    Handles three shapes, because a voice model produces all three:
      - a bare name — "the dice" -> YES_NO_DICE.3mf;
      - a real path that exists — used outright;
      - a path-SHAPED guess that does not exist literally — the one Gemini
        actually built from "the dice, in Downloads":
        "~/Downloads/YES & NO dice". The directory is real, but the basename
        is the spoken name, not the on-disk filename. So we match on the
        basename and search that directory too.

    Matched 2026-09-12: the old guard `"/" in query -> None` rejected exactly
    the string the eagle's brain produces, and the print then fell through to a
    web search that would have fetched (and printed) a stranger's model.
    """
    raw = (query or "").strip()
    exts = (".3mf", ".stl")
    if not raw:
        return None

    # A literal path that exists wins outright (expanduser: "~" is never
    # resolved by Path() on its own).
    lit = Path(raw).expanduser()
    if lit.is_file() and lit.suffix.lower() in exts:
        return lit

    # Otherwise match by NAME. Keep only the basename for the name; if the
    # query pointed at a real directory, search there first.
    name = _normalise(Path(raw).name)
    if not name:
        return None
    search_dirs = list(_local_model_dirs())
    parent = Path(raw).expanduser().parent
    if parent.is_dir() and parent not in search_dirs:
        search_dirs.insert(0, parent)

    partial: Optional[Path] = None
    for d in search_dirs:
        try:
            entries = sorted(d.iterdir())
        except OSError:
            continue
        for f in entries:
            if not f.is_file() or f.suffix.lower() not in exts:
                continue
            stem = _normalise(f.stem)
            if stem == name:
                return f
            if partial is None and len(name) >= 3 and (name in stem or stem in name):
                partial = f
    return partial


def _looks_like_a_path(query: str) -> bool:
    """A query the user meant as a file on THIS machine, not a web search.

    A '/' or a leading '~' means they pointed at a location. If we cannot find
    it locally we must fail, not silently print a stranger's model off the web.
    """
    q = (query or "").strip()
    return "/" in q or q.startswith("~")


def download_model(
    query_or_id: str,
    platform: str = "makerworld",
    sort_by: str = "default",
    profile_index: Optional[int] = None,
    raw_stl: bool = False,
    output_dir: Optional[Path] = None,
    open_slicer: bool = False,
    printer: Optional[str] = None
) -> Dict[str, Any]:
    """
    Download a 3D model by keyword or ID and organize into local storage.

    Every success carries `dimensions` and `preview` alongside the file path,
    so a caller that wants to draw the part does not have to re-open it.
    """
    prov = get_provider(platform)
    target_dir = Path(output_dir) if output_dir else Path(config.download_dir)

    # 0. Direct Local File Check — a full path the user or model handed us.
    #    expanduser: Path("~/...") does NOT resolve "~" on its own.
    p = Path(query_or_id).expanduser()
    if p.exists() and p.is_file():
        return _describe({
            "success": True,
            "design_id": 0,
            "title": p.name,
            "file_path": str(p.resolve()),
            "platform": "local",
            # Unknown until it is sliced. These were "20.0g" and "30m" for
            # every file on the computer, and were said out loud as fact.
            "weight": None,
            "print_time": None
        }, printer)

    # 0b. NAME that names a file already on this computer.
    #
    # A voice model strips context: "print the YES_NO_DICE in downloads" arrives
    # as query_or_id="YES_NO_DICE", with no path — or, measured 2026-09-12, as
    # the path the model helpfully builds, "~/Downloads/YES & NO dice", whose
    # basename is the spoken name, not the on-disk filename. Both are matched
    # against the folders a person actually keeps models in FIRST, and only a
    # genuine miss falls through to the web. This is what makes "print the dice
    # in my downloads" do what it says with nothing typed.
    local = _find_local_model(query_or_id)
    if local is not None:
        return _describe({
            "success": True,
            "design_id": 0,
            "title": local.name,
            "file_path": str(local.resolve()),
            "platform": "local",
            # Unknown until it is sliced. These were "20.0g" and "30m" for
            # every file on the computer, and were said out loud as fact.
            "weight": None,
            "print_time": None
        }, printer)

    # 0c. The user pointed at a location on THIS machine (a path or a "~"), and
    # nothing there matched. Do NOT web-search: fetching and printing a
    # stranger's model when they meant a local file is an irreversible wrong
    # action. Fail clearly instead.
    if _looks_like_a_path(query_or_id):
        return {
            "success": False,
            "error": (f"I could not find a 3D model matching "
                      f"'{Path(query_or_id).name}' in that location. Nothing "
                      f"was printed. Check the file is in the folder and is a "
                      f".3mf or .stl."),
        }

    try:
        design_id = prov.parse_model_id(query_or_id)
        is_id = True
    except Exception:
        is_id = False

    if is_id:
        design = prov.get_design(str(design_id))
    else:
        search_res = prov.search(query_or_id, limit=10)
        if not search_res.designs:
            return {"success": False, "error": f"No models found for '{query_or_id}'"}
        
        designs = list(search_res.designs)
        if sort_by == "downloads":
            designs.sort(key=lambda d: d.download_count, reverse=True)
        elif sort_by == "least_material":
            designs.sort(key=lambda d: d.min_weight_g)
        elif sort_by == "fastest":
            designs.sort(key=lambda d: d.min_print_time_s)
        
        design = designs[0]
        # Fetch full design details to get complete instances
        design = prov.get_design(str(design.id))

    # Select profile
    selected_profile = None
    if profile_index and 1 <= profile_index <= len(design.profiles):
        selected_profile = design.profiles[profile_index - 1]
    else:
        selected_profile = design.primary_profile

    project_folder = target_dir / f"{design.id}_{design.slug or 'model'}"
    project_folder.mkdir(parents=True, exist_ok=True)

    with _design_lock(project_folder):
        return _download_into(prov, design, selected_profile, project_folder,
                              raw_stl, open_slicer, printer)


@contextlib.contextmanager
def _design_lock(folder: Path):
    """One download of a design at a time, across processes.

    The island downloads a browse's candidates in the background while the
    user can say "print that one", which downloads the same design again.
    Both wrote the same `<file>.part`; the second now waits and then finds
    the first one's file in the cache. No-op where flock does not exist.
    """
    try:
        import fcntl
    except ImportError:
        yield
        return
    with open(folder / ".download.lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _download_into(prov, design, selected_profile, project_folder: Path,
                   raw_stl: bool, open_slicer: bool, printer) -> Dict[str, Any]:
    # Check if already downloaded locally
    existing_files = list(project_folder.glob("*.3mf")) + list(project_folder.glob("*.stl"))
    if existing_files:
        saved_path = existing_files[0]
        return _describe({
            "success": True,
            "design_id": design.id,
            "title": design.title,
            "file_path": str(saved_path),
            "platform": design.platform,
            "weight": selected_profile.formatted_weight if selected_profile else "N/A",
            "print_time": selected_profile.formatted_print_time if selected_profile else "N/A"
        }, printer)

    # Download from provider
    profile_id = selected_profile.id if selected_profile else None
    try:
        url, filename = prov.get_download_url(design.id, profile_id=profile_id, raw_stl=raw_stl)
        dest_file = project_folder / filename
        saved_path = prov.download_file(url, dest_file)
    except LoginRequired as e:
        # `needs_account` is for the eagle, which owns the only place the user
        # signs in and hands this module the session (manifest [[accounts]]).
        return {"success": False, "error": str(e), "needs_account": e.site,
                "guidance": ("Nothing was downloaded. This needs the user's "
                             "MakerWorld account: they sign in once in "
                             "Settings, under Modules, Aethelark-3D.")}
    except Exception as e:
        # If specific instance had issue, try other designs in search
        return {"success": False, "error": str(e)}

    if open_slicer:
        open_gui(saved_path)

    return _describe({
        "success": True,
        "design_id": design.id,
        "title": design.title,
        "file_path": str(saved_path),
        "platform": design.platform,
        "weight": selected_profile.formatted_weight if selected_profile else "N/A",
        "print_time": selected_profile.formatted_print_time if selected_profile else "N/A"
    }, printer)


def browse_models(
    query: str,
    limit: int = 5,
    printer: Optional[str] = None,
    sort_by: str = "downloads",
    max_workers: int = 5
) -> Dict[str, Any]:
    """A deck of candidates for one query, from the SEARCH — nothing downloaded."""
    fleet = _configured_fleet()
    # The search carries no print profiles, so weight and time came back "N/A"
    # on every card, and sorting by least material or fastest sorted on
    # nothing. Each design's own page has them: ~1 s a request, no captcha,
    # measured 2026-09-25 -- fetched together, so the deck waits about one.
    by_profile = sort_by in ("least_material", "fastest")
    found = search_models(query, limit=max(limit, 10) if by_profile else limit,
                          sort_by="downloads" if by_profile else sort_by)
    found = _with_profiles(found, max_workers)
    if by_profile:
        key = "weight_g" if sort_by == "least_material" else "print_seconds"
        found.sort(key=lambda e: (e.get(key) is None, e.get(key) or 0))
    if not found:
        return {"query": query, "printer": printer, "fleet": fleet,
                "candidates": [], "failed": []}

    # The printer this deck is aimed at, for the routing chip on each card.
    # An exact fit-check needs the model's real size, which we deliberately do
    # not download here; that check happens when the user picks one. For the
    # deck it is the printer they named, or the fleet's first real one.
    dest = printer or (fleet[0]["key"] if fleet else None)

    candidates = []
    for e in found[:limit]:
        candidates.append({
            "success": True,
            "id": e["id"],
            "design_id": e["id"],
            "title": e.get("title"),
            "creator": e.get("creator"),
            "downloads": e.get("downloads"),
            "likes": e.get("likes"),
            "weight": e.get("weight"),
            "print_time": e.get("print_time"),
            "eta": e.get("print_time"),
            "url": e.get("url"),
            "cover_url": small_cover(e.get("cover_url")),
            "likes_label": _count_label(e.get("likes")),
            "downloads_label": _count_label(e.get("downloads")),
            "platform": e.get("platform", "makerworld"),
            "printer": dest,
        })

    return {"query": query, "printer": printer, "fleet": fleet,
            "candidates": candidates,
            "failed": []}


def _with_profiles(entries: List[Dict[str, Any]], max_workers: int = 5,
                   platform: str = "makerworld") -> List[Dict[str, Any]]:
    """Fill each entry's weight and time from its design's default profile.

    A design whose page cannot be read keeps what the search gave it (None),
    rather than failing the deck.
    """
    from concurrent.futures import ThreadPoolExecutor
    prov = get_provider(platform)

    def one(entry):
        try:
            design = prov.get_design(str(entry["id"]))
        except Exception:
            return entry
        prof = design.primary_profile
        if prof is None:
            return entry
        return {**entry,
                "weight": prof.formatted_weight if prof.weight_g else None,
                "print_time": prof.formatted_print_time if prof.prediction_s else None,
                "weight_g": prof.weight_g,
                "print_seconds": prof.prediction_s}

    if not entries:
        return entries
    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(entries)))) as pool:
        return list(pool.map(one, entries))


def _configured_fleet() -> List[Dict[str, str]]:
    """The printers the user could legitimately name, in a stable order.

    Only those with an address. A key declared and never filled in is a slot,
    not a printer, and offering it as a destination produces a job that fails
    later and less clearly than declining it now — the same rule
    `choose_printer` applies when it picks one by fit.
    """
    try:
        fleet = config.get_fleet() or {}
    except Exception:
        return []
    return [{"key": key, "name": str(cfg.get("name") or key)}
            for key, cfg in fleet.items()
            if str(cfg.get("host") or "").strip()]


#: Where a model the user already has is likely to be. Ordered: the folder
#: they keep prints in, then the folder this tool downloads into, then the
#: browser's.
def _local_search_dirs() -> List[Path]:
    dirs = [Path.home() / "3D_Prints"]
    try:
        dirs.append(Path(config.download_dir))
    except Exception:
        pass
    dirs += [Path.home() / "Downloads", Path.home() / "Desktop"]
    seen, out = set(), []
    for d in dirs:
        d = d.expanduser()
        if d.is_dir() and str(d) not in seen:
            seen.add(str(d))
            out.append(d)
    return out


def find_local_models(name: str, directory: Optional[str] = None,
                      limit: int = 8) -> List[Path]:
    """Model files on this machine whose name looks like `name`.

    "Print my keychain file" is a different request from "find me a keychain":
    the user is naming something they already have, and an absolute path is a
    lot to say out loud. Matched on the filename stem, case-insensitively, so
    "keychain" finds Keychain_Draft_v1.stl.

    Exact stem matches come first — asked for "benchy", a file actually called
    benchy.stl should outrank Baby_Seal_Benchy.stl.
    """
    from .downloads import MODEL_SUFFIXES

    raw = (name or "").strip()
    if not raw:
        return []
    # A voice model writes "YES & NO dice"; the file is YES_NO_DICE.stl. Match
    # on the NORMALISED stem (fold spaces, underscores, punctuation, case) so
    # the two meet — a plain `needle in stem` substring never would.
    needle = _normalise(Path(raw).name)
    if not needle:
        return []

    direct = Path(raw).expanduser()
    if direct.is_file() and direct.suffix.lower() in MODEL_SUFFIXES:
        return [direct.resolve()]

    # `directory` may be a real path, a spoken keyword ("downloads", "my
    # desktop"), or junk. Resolve it; on a keyword miss, fall back to the
    # standard dirs rather than searching nothing and reporting "not found".
    roots: List[Path]
    if directory:
        d = Path(directory).expanduser()
        if d.is_dir():
            roots = [d]
        else:
            kw = _normalise(directory)
            named = {"downloads": Path.home() / "Downloads",
                     "desktop": Path.home() / "Desktop",
                     "documents": Path.home() / "Documents",
                     "home": Path.home()}
            hit = next((p for k, p in named.items() if k in kw and p.is_dir()), None)
            roots = [hit] if hit else _local_search_dirs()
    else:
        roots = _local_search_dirs()

    hits: List[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        try:
            entries = sorted(root.rglob("*"))
        except OSError:
            continue
        for entry in entries:
            if not entry.is_file() or entry.suffix.lower() not in MODEL_SUFFIXES:
                continue
            stem = _normalise(entry.stem)
            if needle == stem or (len(needle) >= 3 and (needle in stem or stem in needle)):
                hits.append(entry.resolve())

    seen, unique = set(), []
    for h in hits:
        if str(h) not in seen:
            seen.add(str(h))
            unique.append(h)
    # Exact normalised-stem matches first, then shorter names.
    unique.sort(key=lambda p: (_normalise(p.stem) != needle, len(p.stem), str(p)))
    return unique[:limit]


def local_models(name: str, directory: Optional[str] = None,
                 printer: Optional[str] = None, limit: int = 8
                 ) -> Dict[str, Any]:
    """The same deck shape a browse returns, for files already on this machine.

    Deliberately identical, so one match renders as a card the user can look at
    and several render as a carousel they can flip through — the island already
    knows how to do both and does not need to learn a third thing.
    """
    fleet = _configured_fleet()
    found = find_local_models(name, directory=directory, limit=limit)
    if not found:
        return {"query": name, "printer": printer, "fleet": fleet,
                "candidates": [], "failed": []}

    candidates, failed = [], []
    for path in found:
        entry = _describe({"success": True, "design_id": 0, "title": path.name,
                           "file_path": str(path), "platform": "local"}, printer)
        if entry.get("_preview"):
            entry["id"] = str(path)
            candidates.append(entry)
        else:
            failed.append({"id": str(path), "title": path.name,
                           "error": "no readable geometry in this file"})
    return {"query": name, "printer": printer, "fleet": fleet,
            "candidates": candidates, "failed": failed}


async def connect_with_rediscovery(driver, printer_key: str, make_driver):
    """Connect, and if the stored address is stale, go and find the real one."""
    try:
        await driver.connect()
        return driver
    except Exception as first:
        try:
            from .discovery import find_and_adopt
            healed = find_and_adopt()
        except Exception:
            raise first
        moved_to = (healed.get("moved") or {}).get(printer_key)
        if not moved_to:
            raise first
        print(f"[a3d] {printer_key} had moved to {moved_to}; reconnecting.")
        fresh = make_driver()
        await fresh.connect()
        return fresh


def find_and_prepare_print(
    query: str,
    sort_by: str = "downloads",
    printer_key: str = "",
    filament: Optional[str] = None,
    ams_slots: Optional[List[str]] = None,
    open_slicer: bool = False,
    auto_start: bool = True,
    use_simulator: bool = False,
    enable_purgex: bool = False,   # PurgeX is opt-in: the basic pipeline
                                   # uses standard purge and PurgeX is
                                   # offered, never applied silently.
    auto_level: bool = True,
    nozzle_temp: Optional[int] = None,
    bed_temp: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Complete 100% autonomous pipeline: Search -> Pick Best -> Download -> Headless Slice -> Upload to Printer -> Start Print.
    """
    printer_key = printer_key or config.default_printer
    from .drivers.factory import get_driver_for_printer
    from .spools import FilamentVault
    from .config import config
    import asyncio
    printer_key = config.key_for(printer_key) or printer_key

    # Pre-flight: a print that will be SENT to a printer needing its access key
    # is stopped here, before any download or slice. Doing ten minutes of work
    # and then failing at the printer — or worse, on a locked printer, silently
    # never starting — is the opposite of frictionless. Answer the one thing
    # that has to happen first: read the key off the printer and pair it.
    if auto_start and not use_simulator:
        slot = config.get_printer(printer_key)
        if slot and slot.get("needs_access_code") and not slot.get("access_code"):
            return {
                "success": False,
                "needs_pairing": True,
                "printer": printer_key,
                "error": (f"{slot.get('name', printer_key)} was found but still "
                          f"needs its access key before I can send a print. "
                          f"Read the key off the printer's screen (network "
                          f"settings) and pair it first."),
            }

    from .filaments.resolver import UnknownMaterial, describe, parse, plan as plan_filament
    from .printer_models import model_for_key, slicer_profile
    printer_name = (config.get_printer(printer_key) or {}).get("name") or printer_key
    spool = FilamentVault.get_mounted_spool(printer_key, slot=1)
    if not filament and spool:
        filament = " ".join(x for x in (spool.get("material"), spool.get("color")) if x)
    if not filament:
        return {"success": False, "needs_filament": True, "printer": printer_key,
                "error": f"I don't know which filament is loaded on {printer_name}.",
                "guidance": ("Ask the user which filament is on that printer (brand and "
                             "material, and temperatures if they know them), record it "
                             "with a3d_load_spool, then ask to print again.")}
    if spool and not nozzle_temp and not bed_temp:
        said, loaded = parse(filament), parse(spool.get("material") or "")
        if said.material == loaded.material and said.brand in ("", loaded.brand):
            nozzle_temp, bed_temp = spool.get("nozzle_temp"), spool.get("bed_temp")
    model_entry = model_for_key(printer_key)
    prof = (slicer_profile(model_entry[0].printer_model, float(model_entry[1].get("nozzle") or 0.4))
            if model_entry[0] else None)
    try:
        fil_plan = plan_filament(filament, prof.family if prof else "ECC",
                                 nozzle=nozzle_temp, bed=bed_temp, printer=model_entry[0])
    except UnknownMaterial as e:
        return {"success": False, "printer": printer_key, "error": str(e),
                "guidance": "Nothing was started. Ask the user which material it is."}
    from .filaments.resolver import over_limit
    too_hot = over_limit(fil_plan, model_entry[0])
    if too_hot:
        return {"success": False, "printer": printer_key,
                "error": f"On {printer_name}, the {too_hot}.",
                "guidance": "Nothing was started. Tell the user the limit and ask "
                            "which temperature to use instead."}

    res = download_model(query_or_id=query, sort_by=sort_by, open_slicer=open_slicer)
    if not res.get("success"):
        return res

    file_path = Path(res["file_path"])
    
    # What is loaded, from the spools the user assigned. Slot 1 names the
    # filament profile; on a printer with a CANVAS every slot is passed on, in
    # slot order, so PurgeX works from the colours actually loaded rather than
    # its placeholder White/Black/Red/Blue.
    from .printer_models import has_canvas
    resolved_filament = filament
    entry = config.get_printer(printer_key) or {}
    if ams_slots is None and has_canvas(entry, printer_key):
        count = int(entry.get("canvas_slots") or 4)
        loaded = [FilamentVault.get_mounted_spool(printer_key, slot=n) for n in range(1, count + 1)]
        if any(loaded):
            ams_slots = [" ".join(x for x in ((sp or {}).get("color"), (sp or {}).get("material")) if x)
                         or "empty" for sp in loaded]

    # 1. Slice. A failure here is an answer, not a traceback: the CLI printed
    # one and the eagle heard only "did not emit valid JSON".
    try:
        gcode_path = slice_headless(
            model_path=file_path,
            filament_query=resolved_filament,
            ams_filaments=ams_slots,
            enable_purgex=enable_purgex,
            printer_key=printer_key,
            nozzle_temp=nozzle_temp,
            bed_temp=bed_temp,
        )
    except Exception as e:
        return {
            "success": False,
            "action": "not_sliced",
            "model_title": res["title"],
            "printer": printer_key,
            "printer_name": printer_name,
            "model_file": str(file_path),
            "error": f"{res['title']} could not be prepared for printing: {e}",
            "guidance": getattr(e, "guidance", None) or (
                "Tell the user the model could not be prepared, so nothing was "
                "sent to the printer. The downloaded file is kept."),
        }

    # 2. Is there enough filament? A heads-up, never a refusal.
    from .spools import filament_grams_from_gcode
    slot_grams = [(i + 1, g) for i, g in
                  enumerate(filament_grams_from_gcode(gcode_path)) if g > 0]
    filament_warnings = []
    for slot_no, need in slot_grams:
        enough, left, _ = FilamentVault.check_sufficiency(
            printer_key, slot=slot_no, required_grams=need)
        if not enough:
            spool = FilamentVault.get_mounted_spool(printer_key, slot=slot_no) or {}
            what = " ".join(x for x in (spool.get("color"), spool.get("material")) if x) or "filament"
            filament_warnings.append(
                f"slot {slot_no} needs about {need:.0f} g and the {what} there "
                f"has about {left:.0f} g left")

    # 3. Polymorphic Driver Upload & Print Start
    driver = get_driver_for_printer(printer_key, use_simulator=use_simulator)
    
    async def _connect_or_rediscover():
        nonlocal driver
        driver = await connect_with_rediscovery(
            driver, printer_key,
            lambda: get_driver_for_printer(printer_key,
                                           use_simulator=use_simulator,
                                           fresh=True))

    async def _execute_print():
        await _connect_or_rediscover()
        remote_path = await driver.upload_job(gcode_path)
        started = False
        if auto_start:
            # Was hardcoded True, so nothing above this line could turn bed
            # levelling off — not the CLI, not the manifest, not the user.
            # Levelling costs minutes at the start of every job and is worth
            # skipping on a machine that has not moved since the last one.
            started = await driver.start_print(remote_path,
                                               auto_level=auto_level)
            if started:
                for slot_no, need in slot_grams:
                    FilamentVault.deduct_usage(printer_key, slot=slot_no,
                                               grams_used=need)
        return True, started

    upload_success = False
    print_started = False
    printer_error = ""
    try:
        upload_success, print_started = asyncio.run(_execute_print())
    except Exception as e:
        printer_error = str(e)

    result = {
        "success": True,
        "action": "printing_started" if print_started else ("uploaded" if upload_success else "sliced_only"),
        "model_title": res["title"],
        "printer": printer_key,
        "printer_name": printer_name,
        "filament": describe(fil_plan),
        "filament_said": resolved_filament,
        "model_file": str(file_path),
        "gcode_file": str(gcode_path),
        "uploaded": upload_success,
        "print_command_sent": print_started,
        "filament_grams": {f"slot_{n}": round(g, 1) for n, g in slot_grams},
    }
    if print_started:
        from . import job_thumbs
        thumb = job_thumbs.lookup(Path(str(gcode_path)).name)
        result["_card"] = {"variant": "job", "title": printer_name, "printer": printer_name,
                           "printer_key": printer_key, "badge": "Started",
                           "detail": f"{res['title']} · {describe(fil_plan)}",
                           "thumb": thumb.as_uri() if thumb else ""}
        result["_stage"] = "capsule"
    if filament_warnings:
        result["filament_warning"] = "; ".join(filament_warnings) + "."
        result["guidance"] = ("Mention it once: the spool may run out during this "
                              "print, and the printer will pause so a new one can "
                              "be loaded.")
    if auto_start and not print_started:
        result["success"] = False
        reached = ("it was uploaded but the printer did not start it"
                   if upload_success else "the printer could not be reached")
        result["error"] = (f"{res['title']} was prepared, but {reached}"
                           + (f": {printer_error}" if printer_error else "") + ".")
        result["guidance"] = ("Tell the user the print did NOT start. The prepared "
                              "file is kept, so it can be sent again once the "
                              "printer is on and connected.")
    return result


def print_batch(jobs: List[Dict[str, str]]) -> Dict[str, Any]:
    """Start one print per (model, printer) pair, in the order they were picked.

    Sequential and not parallel: each job slices and uploads to a specific
    machine, and a failure on one printer says nothing about the others. One
    offline machine must not cancel the prints that were going to work.
    """
    started, failed = [], []
    if not isinstance(jobs, list):
        return {"started": [], "failed": [{"model_id": None, "printer": None,
                "error": f"jobs must be a list, got {type(jobs).__name__}"}]}
    for job in jobs:
        if not isinstance(job, dict):
            # A malformed entry is a failed job like any other -- it does not
            # get to take the rest of the batch down with it, and whatever
            # already dispatched before it must not be un-dispatched by an
            # exception now.
            failed.append({"model_id": None, "printer": None,
                           "error": f"malformed job entry: expected an object, got {type(job).__name__}"})
            continue
        model_id, printer = job.get("model_id"), job.get("printer")
        # Per job: a batch can mix a first print that levels with later ones
        # on the same bed that do not need to.
        level = job.get("auto_level", True)
        level = True if level is None else bool(level)
        # Both are rejected the same way, and for the same reason. A missing
        # printer would otherwise default to a machine nobody named; a missing
        # model id would otherwise be str(None) -> "None", handed to the
        # search as a query and *attempted*. That fails safe only if the
        # search happens to find nothing, and "probably nothing" is not a
        # property to give the code that puts plastic on a bed.
        if not printer:
            failed.append({**job, "error": "no printer named"})
            continue
        if not model_id:
            failed.append({**job, "error": "no model named"})
            continue
        try:
            res = find_and_prepare_print(str(model_id), printer_key=printer,
                                         auto_level=level)
        except Exception as e:
            failed.append({**job, "error": str(e)})
            continue
        # success=True is necessary but not sufficient: find_and_prepare_print
        # returns it unconditionally once slicing succeeds, even when the
        # driver never reached the printer (action="sliced_only") or reached
        # it but the start command was rejected (action="uploaded"). Only
        # print_command_sent means plastic is actually being laid down; that
        # is the one field this function is allowed to call "started".
        if res.get("success") and res.get("print_command_sent"):
            started.append({**job})
            continue
        if res.get("error"):
            error = res["error"]
        elif res.get("uploaded"):
            error = "uploaded to the printer, but it did not start printing"
        else:
            error = "sliced, but could not reach the printer"
        entry = {**job, "error": error}
        if res.get("needs_account"):
            entry["needs_account"] = res["needs_account"]
        failed.append(entry)
    out = {"started": started, "failed": failed}
    # One missing sign-in fails every job the same way; say it once, where the
    # eagle looks, so it can fix the cause rather than read out each failure.
    wanted = next((f["needs_account"] for f in failed if f.get("needs_account")), None)
    if wanted:
        out["needs_account"] = wanted
    return out
