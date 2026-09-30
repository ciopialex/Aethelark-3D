"""
ElegooSlicer Engine Backend for Aethelark-3D.
Integrates headless CLI slicing, filament profile resolution, 3D vector thumbnail generation,
and compliant container packaging for Elegoo Centauri Carbon, CC2, and C2 series.
"""

import io
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

from aethelark3d.slicers.base import BaseSlicer, SliceConfig, SliceResult
from aethelark3d.slicers.rasterizer import render_stl_to_image


class ProfileMissing(RuntimeError):
    """This printer cannot be sliced for with the profiles installed.

    Either the printer has not said what model it is, or the installed
    ElegooSlicer has no machine profile for that model. Slicing with some
    other printer's profile instead is how a CC2 was once sent CC1 start
    G-code and hung: never again quietly.
    """

    def __init__(self, message: str, guidance: str):
        super().__init__(message)
        self.guidance = guidance


def _printing_seconds(gcode_text: str) -> Optional[int]:
    """`; estimated printing time (normal mode) = 1d 2h 3m 4s` as seconds."""
    m = re.search(r";\s*estimated printing time[^=]*=\s*([0-9dhms\s]+)", gcode_text, re.I)
    if not m:
        return None
    units = {"d": 86400, "h": 3600, "m": 60, "s": 1}
    parts = re.findall(r"(\d+)\s*([dhms])", m.group(1).lower())
    return sum(int(n) * units[u] for n, u in parts) if parts else None


def profile_for(printer_key: str, nozzle: Optional[float] = None):
    """(Model, SlicerProfile) for a printer in the fleet, or ProfileMissing."""
    from aethelark3d.printer_models import model_for_key, slicer_profile
    model, entry, key = model_for_key(printer_key or "")
    if model is None:
        raise ProfileMissing(
            f"I do not know what model {key or 'this printer'} is.",
            "Nothing was sliced. Ask the user to switch the printer on, then "
            "call a3d_discover so it can say what model it is, and try again.")
    size = float(nozzle or entry.get("nozzle") or 0.4)
    profile = slicer_profile(model.printer_model, size)
    if profile is None:
        raise ProfileMissing(
            f"The installed ElegooSlicer has no profile for the {model.name} "
            f"with a {size:.1f} mm nozzle.",
            f"Nothing was sliced. Tell the user to update ElegooSlicer so it "
            f"knows the {model.name}, then ask again.")
    return model, profile


class SlicerMissing(RuntimeError):
    """Printing needs ElegooSlicer and it is not on this computer."""

    guidance = ("Tell the user printing needs ElegooSlicer. They can install it "
                "from elegoo.com (on Linux, put the AppImage in ~/Applications), "
                "then ask again. Nothing was printed.")


def find_slicer() -> str | None:
    """ElegooSlicer's executable, or None.

    Slicing ran a bare `elegoo` off PATH, which only existed on the machine
    this was written on (a personal symlink to an AppImage), and the
    `slicer_path` setting it declared was never read. A desktop-launched eagle
    may not even have ~/.local/bin on PATH. So: the configured path, then PATH,
    then the places an AppImage is usually put.
    """
    import os
    import shutil
    from aethelark3d.config import config

    home = Path.home()
    candidates = []
    if config.slicer_path:
        candidates.append(Path(config.slicer_path).expanduser())
    on_path = shutil.which("elegoo") or shutil.which("elegoo-slicer")
    if on_path:
        candidates.append(Path(on_path))
    candidates.append(home / ".local" / "bin" / "elegoo")
    for folder in (home / "Applications", home / "Downloads", Path("/opt")):
        candidates += sorted(folder.glob("ElegooSlicer*.AppImage"))
        candidates += sorted(folder.glob("ElegooSlicer*/ElegooSlicer*"))
    for c in candidates:
        try:
            if c.is_file() and os.access(c, os.X_OK):
                return str(c)
        except OSError:
            continue
    return None


class ElegooSlicerBackend(BaseSlicer):
    """Production ElegooSlicer Engine with automated thumbnail packaging."""

    def __init__(self, datadir: Optional[Path] = None):
        self.datadir = datadir or (Path.home() / ".config" / "ElegooSlicer")

    def resolve_filament_profile(self, query: str, printer: str = "CC1") -> Tuple[str, Path, Dict[str, Any]]:
        """Match query to comprehensive multi-manufacturer filament presets database."""
        from aethelark3d.filaments.catalog import resolve_filament_profile as _cat_resolve
        return _cat_resolve(query, printer=printer)

    def _extract_physics(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Extract thermal and kinematic properties."""
        fil_type = data.get("filament_type", ["PLA"])[0] if isinstance(data.get("filament_type"), list) else "PLA"
        is_silk = "silk" in data.get("name", "").lower()

        return {
            "type": fil_type,
            "nozzle_temp": int(data.get("nozzle_temperature_initial_layer", [220])[0]),
            "bed_temp": int(data.get("hot_plate_temp_initial_layer", [55])[0]),
            "max_volumetric_speed": float(data.get("filament_max_volumetric_speed", [14.0])[0]),
            "outer_wall_speed": 60 if is_silk else int(data.get("outer_wall_speed", [120])[0] if "outer_wall_speed" in data else 120)
        }

    def generate_thumbnails(self, model_path: Path, output_dir: Path, color_rgb: tuple = (220, 45, 55)) -> List[Path]:
        output_dir.mkdir(parents=True, exist_ok=True)
        
        # 1. 512x512 RGBA Touchscreen preview
        plate_1 = output_dir / "plate_1.png"
        img_plate = render_stl_to_image(model_path, size=(512, 512), color_rgb=color_rgb, is_rgba=True)
        img_plate.save(plate_1, format="PNG")

        # 2. 680x510 RGB App preview
        thumb_mid = output_dir / "thumbnail_middle.png"
        img_mid = render_stl_to_image(model_path, size=(680, 510), color_rgb=color_rgb, is_rgba=False)
        img_mid.save(thumb_mid, format="PNG")

        # 3. 251x188 RGB Fleet thumbnail
        thumb_small = output_dir / "thumbnail_small.png"
        img_small = render_stl_to_image(model_path, size=(251, 188), color_rgb=color_rgb, is_rgba=False)
        img_small.save(thumb_small, format="PNG")

        return [plate_1, thumb_mid, thumb_small]

    #: The version stamp this slicer's CLI accepts on a project. A project saved
    #: by a newer Bambu Studio is refused on its stamp alone.
    PROJECT_STAMP = b"ElegooSlicer-01.05.01.06"
    _FOREIGN_ARCHIVE = ("Metadata/slice_info.config",)

    @staticmethod
    def _creator_project(model_path: Path) -> Optional[dict]:
        """The settings a downloaded project was saved with, or None."""
        if model_path.suffix.lower() != ".3mf" or not zipfile.is_zipfile(model_path):
            return None
        try:
            with zipfile.ZipFile(model_path) as z:
                if "Metadata/project_settings.config" not in z.namelist():
                    return None
                body = json.loads(z.read("Metadata/project_settings.config").decode("utf-8", "ignore"))
            return body if isinstance(body, dict) else None
        except Exception:
            return None

    @staticmethod
    def _cold_bed(filament_flat: Optional[Path]) -> Optional[bool]:
        """Whether the filament profile allows a Cool Plate, or None if unknown."""
        if not filament_flat:
            return None
        try:
            v = json.loads(Path(filament_flat).read_text(encoding="utf-8")).get("cool_plate_temp")
            v = v[0] if isinstance(v, list) and v else v
            return float(v) > 0
        except (TypeError, ValueError, OSError, json.JSONDecodeError):
            return None

    @staticmethod
    def _plate_count(model_path: Path) -> int:
        try:
            with zipfile.ZipFile(model_path) as z:
                return z.read("Metadata/model_settings.config").count(b"<plate>")
        except Exception:
            return 0

    @staticmethod
    def _creator_material(project: dict) -> str:
        for key in ("filament_settings_id", "filament_type"):
            v = project.get(key)
            if isinstance(v, list) and v and v[0]:
                return str(v[0])
            if isinstance(v, str) and v:
                return v
        return ""

    @classmethod
    def _creator_settings(cls, project: dict, filament_query: str) -> dict:
        """The creator's settings that apply to this print: what they changed
        from the defaults, only when the same material is printed. Supports are
        always the slicer's own slim tree supports."""
        from aethelark3d.filaments.resolver import parse
        chosen = {}
        theirs, ours = parse(cls._creator_material(project)), parse(filament_query)
        looks = {"silk", "matte", "marble", "wood", "glow", "sparkle", "galaxy", "translucent"}
        same = (theirs.material and theirs.material == ours.material
                and theirs.composite == ours.composite
                and (theirs.variants & looks) == (ours.variants & looks))
        if same:
            changed = project.get("different_settings_to_system")
            first = changed[0] if isinstance(changed, list) and changed else ""
            for k in str(first).split(";"):
                k = k.strip()
                if (k and k in project and not k.endswith("_filament")
                        and k != "enable_support" and not k.startswith(("support_", "tree_support_"))):
                    chosen[k] = project[k]
        chosen.update(cls._support_settings("auto"))
        return chosen

    def _baseline(self, exe: str, flats: List[Path], filament_flat: Optional[Path],
                  bed_type: str, out_dir: Path) -> Dict[str, str]:
        """Every setting ElegooSlicer resolves for this printer, process and
        filament, its own built-in defaults included, read from the config block
        of a 10 mm cube sliced with them. Cached per combination."""
        import hashlib
        parts = [Path(f).read_bytes() for f in flats]
        if filament_flat:
            parts.append(Path(filament_flat).read_bytes())
        parts.append(bed_type.encode())
        digest = hashlib.sha1(b"\0".join(parts)).hexdigest()[:16]
        home = out_dir / "_baseline"
        cached = home / f"{digest}.json"
        if cached.exists():
            return json.loads(cached.read_text(encoding="utf-8"))
        home.mkdir(parents=True, exist_ok=True)
        cube = home / "cube.stl"
        if not cube.exists():
            v = [(0, 0, 0), (10, 0, 0), (10, 10, 0), (0, 10, 0),
                 (0, 0, 10), (10, 0, 10), (10, 10, 10), (0, 10, 10)]
            faces = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
                     (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
            self._write_binary_stl([tuple(v[i] for i in f) for f in faces], cube)
        gcode = home / "plate_1.gcode"
        gcode.unlink(missing_ok=True)
        cmd = [exe, "--datadir", str(self.datadir), "--slice", "0", "--outputdir", str(home),
               "--load-settings", ";".join(str(f) for f in flats)]
        if filament_flat:
            cmd.extend(["--load-filaments", str(filament_flat)])
        cmd.extend(["--curr-bed-type", bed_type, str(cube)])
        subprocess.run(cmd, capture_output=True, text=True)
        if not gcode.exists():
            return {}
        values: Dict[str, str] = {}
        inside = False
        for line in gcode.read_text(encoding="latin1", errors="ignore").splitlines():
            if line.startswith("; CONFIG_BLOCK_START"):
                inside = True
            elif line.startswith("; CONFIG_BLOCK_END"):
                break
            elif inside and " = " in line:
                key, value = line[2:].split(" = ", 1)
                values[key.strip()] = value
        cached.write_text(json.dumps(values), encoding="utf-8")
        return values

    @staticmethod
    def _project_value(text: str, like: Any) -> Any:
        """A config-block value in the type the project file stores it as."""
        def unquote(v: str) -> str:
            v = v.strip()
            return v[1:-1] if len(v) >= 2 and v[0] == v[-1] == '"' else v
        if isinstance(like, list):
            items = [unquote(x) for x in (text.split(";") if ";" in text else text.split(","))]
            if len(items) == 1 and len(like) > 1:
                items = items * len(like)
            return items
        return unquote(text).replace("\\n", "\n")

    def _project_3mf(self, model_path: Path, out_dir: Path, project: dict,
                     flats: List[Path], filament_flat: Optional[Path], bed_type: str,
                     baseline: Optional[Dict[str, str]] = None) -> Path:
        """The creator's project with this printer's settings in place of theirs.

        What ElegooSlicer does when a foreign project is opened and the printer
        and filament are switched: geometry, painted supports and per-object
        placement stay; printer, process and filament become ours.
        """
        n = len(project.get("filament_type") or [1]) or 1
        bodies = [json.loads(Path(f).read_text(encoding="utf-8")) for f in flats]
        ours: Dict[str, Any] = {}
        for body in bodies:
            ours.update(body)
        names = {"printer_settings_id": bodies[0].get("name"),
                 "print_settings_id": bodies[1].get("name") if len(bodies) > 1 else None,
                 "filament_settings_id": None}
        if filament_flat:
            fil = json.loads(Path(filament_flat).read_text(encoding="utf-8"))
            names["filament_settings_id"] = [fil.get("name")] * n
            for k, v in fil.items():
                ours[k] = (v * n) if isinstance(v, list) and len(v) == 1 else v
        for k in ("inherits", "name", "from", "instantiation", "setting_id", "type"):
            ours.pop(k, None)
        merged = dict(project)
        for k, v in project.items():
            if baseline and k in baseline and k not in ours:
                merged[k] = self._project_value(baseline[k], v)
        merged.update(ours)
        for k, v in names.items():
            if v:
                merged[k] = v
        for k in [k for k in merged if "compatible" in k]:
            merged[k] = [] if isinstance(merged[k], list) else ""
        merged["different_settings_to_system"] = ["", "", ""]
        merged["curr_bed_type"] = bed_type

        out = out_dir / "_project.3mf"
        with zipfile.ZipFile(model_path) as zi, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zo:
            for item in zi.infolist():
                name = item.filename
                if (name in self._FOREIGN_ARCHIVE or name.startswith("Auxiliaries/")
                        or name.endswith(".gcode")):
                    continue
                data = zi.read(name)
                if name == "Metadata/project_settings.config":
                    data = json.dumps(merged, indent=1).encode("utf-8")
                elif name == "3D/3dmodel.model":
                    data = re.sub(rb'(<metadata name="Application">)[^<]*',
                                  rb"\g<1>" + self.PROJECT_STAMP, data)
                zo.writestr(item, data)
        return out

    def _run_project(self, exe: str, packed: Path, out_dir: Path):
        """Slice plate 1 of a project.

        Values the CLI rejects as out of range (a newer Bambu Studio's
        "automatic" 0 and -1) are removed so it uses its own defaults. Parts
        laid out for another printer's bed that collide or fall off this one
        are re-arranged by the slicer.
        """
        cmd = [exe, "--datadir", str(self.datadir), "--slice", "1",
               "--outputdir", str(out_dir), str(packed)]
        done = out_dir / "plate_1.gcode"
        result = subprocess.run(cmd, capture_output=True, text=True)
        rejected = set(re.findall(r"^(\w+): \S+ not in range", result.stdout + result.stderr, re.M))
        if rejected and not done.exists():
            self._drop_project_keys(packed, rejected)
            result = subprocess.run(cmd, capture_output=True, text=True)
        said = result.stdout + result.stderr
        if not done.exists() and ("conflicts found" in said or "Nothing to be sliced" in said):
            result = subprocess.run(cmd[:-1] + ["--arrange", "1", cmd[-1]], capture_output=True, text=True)
        return result

    @staticmethod
    def _drop_project_keys(packed: Path, keys: set) -> None:
        with zipfile.ZipFile(packed) as z:
            items = [(i, z.read(i.filename)) for i in z.infolist()]
        with zipfile.ZipFile(packed, "w", zipfile.ZIP_DEFLATED) as z:
            for item, data in items:
                if item.filename == "Metadata/project_settings.config":
                    body = json.loads(data)
                    for k in keys:
                        body.pop(k, None)
                    data = json.dumps(body, indent=1).encode("utf-8")
                z.writestr(item, data)

    @staticmethod
    def _creator_thumbnail(model_path: Path, out_dir: Path) -> Optional[Path]:
        """The picture the model's creator exported with their project."""
        import zipfile
        if model_path.suffix.lower() != ".3mf" or not zipfile.is_zipfile(model_path):
            return None
        wanted = ("Metadata/plate_1.png", "Auxiliaries/.thumbnails/thumbnail_middle.png",
                  "Auxiliaries/.thumbnails/thumbnail_3mf.png", "Metadata/thumbnail.png")
        try:
            with zipfile.ZipFile(model_path) as z:
                names = set(z.namelist())
                for name in wanted:
                    if name in names:
                        out = out_dir / "_creator_thumb.png"
                        out.write_bytes(z.read(name))
                        return out
        except Exception:
            return None
        return None

    @staticmethod
    def _support_settings(mode: str) -> dict:
        if mode == "off":
            return {"enable_support": "0"}
        if mode == "on":
            return {"enable_support": "1"}
        return {"enable_support": "1", "support_type": "tree(auto)", "support_style": "tree_slim"}

    def _flat_system_profile(self, path: Path, kind: str, out_dir: Path,
                             overrides: Optional[dict] = None) -> Path:
        """A machine or process profile with its whole `inherits` chain merged.

        The CLI keeps only a loaded profile's own keys. Loading the Centauri
        Carbon's process leaf alone sliced at 60 mm/s walls and 500 mm/s^2
        (8h35m for a phone stand); its machine leaf alone capped acceleration at
        1000 instead of 20000.
        """
        import json as _json
        try:
            idx = {}
            for f in (self.datadir / "system" / "Elegoo" / kind).rglob("*.json"):
                try:
                    nm = _json.loads(f.read_text(encoding="utf-8")).get("name")
                except Exception:
                    continue
                if nm and nm not in idx:
                    idx[nm] = f
            merged: dict = {}
            node = _json.loads(path.read_text(encoding="utf-8"))
            steps = 0
            while node and steps < 12:
                for k, v in node.items():
                    merged.setdefault(k, v)
                parent = node.get("inherits")
                if not parent or parent not in idx:
                    break
                node = _json.loads(idx[parent].read_text(encoding="utf-8"))
                steps += 1
            merged.pop("inherits", None)
            merged.update(overrides or {})
            flat = out_dir / f"{path.stem} (flattened).json"
            flat.write_text(_json.dumps(merged), encoding="utf-8")
            return flat
        except Exception:
            return path

    def _geometry_only(self, model_path: Path, out_dir: Path) -> Path:
        """A .3mf reduced to its raw mesh, so no embedded project settings for a
        foreign printer survive into the slice. Returns the original path for a
        file that is already a bare mesh, or if the conversion fails (better to
        try the real file than to refuse the print).

        The 3MF is read with the module's own indexed-mesh parser
        (downloads._mesh_points) rather than a third-party library — the parser
        already exists for previews and measurement, and it means no extra
        dependency to fail on a fresh install (a missing dep here would fall
        back to the raw .3mf and hang the printer, the very bug this avoids).
        """
        if model_path.suffix.lower() != ".3mf":
            return model_path
        try:
            from aethelark3d.downloads import _mesh_points
            triangles = _mesh_points(model_path)
            if not triangles:
                return model_path
            geom_dir = out_dir / "geometry"
            geom_dir.mkdir(parents=True, exist_ok=True)
            stl_path = geom_dir / (model_path.stem + ".stl")
            self._write_binary_stl(triangles, stl_path)
            return stl_path
        except Exception:
            return model_path

    @staticmethod
    def _write_binary_stl(triangles, path: Path) -> None:
        """Write triangles (each three (x,y,z) vertices) as a binary STL.

        The per-facet normal is computed from the winding so the file is
        well-formed; a slicer recomputes them anyway, but a zero normal trips
        some stricter loaders.
        """
        import struct
        with open(path, "wb") as fh:
            fh.write(b"aethelark geometry-only".ljust(80, b" "))
            fh.write(struct.pack("<I", len(triangles)))
            for tri in triangles:
                (ax, ay, az), (bx, by, bz), (cx, cy, cz) = tri
                ux, uy, uz = bx - ax, by - ay, bz - az
                vx, vy, vz = cx - ax, cy - ay, cz - az
                nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
                length = (nx * nx + ny * ny + nz * nz) ** 0.5 or 1.0
                fh.write(struct.pack("<3f", nx / length, ny / length, nz / length))
                fh.write(struct.pack("<9f", ax, ay, az, bx, by, bz, cx, cy, cz))
                fh.write(struct.pack("<H", 0))

    def _filament_index(self) -> dict:
        """name -> file, across the Elegoo filament presets and their bases.

        Built once per backend. Needed to walk a filament's `inherits` chain by
        name, because the base presets (fdm_filament_pla, fdm_filament_common)
        live above the per-machine folders.
        """
        cached = getattr(self, "_fil_idx", None)
        if cached is not None:
            return cached
        import json as _json
        idx = {}
        root = self.datadir / "system" / "Elegoo"
        for f in root.rglob("*.json"):
            try:
                name = _json.loads(f.read_text(encoding="utf-8")).get("name")
            except Exception:
                continue
            if name and name not in idx:
                idx[name] = f
        self._fil_idx = idx
        return idx

    def _flattened_filament(self, fil_path: Path, out_dir: Path) -> Path:
        """Resolve a filament's full inheritance chain into ONE complete profile.

        ElegooSlicer's CLI does not resolve `inherits` for a filament handed to
        --load-filaments: it keeps only the leaf's own keys, so a leaf that
        sets just the nozzle temperature loses the bed temperature and the
        nozzle falls to a default. Measured on Elegoo PLA Basic: bed came out
        45C and 200C instead of the profile chain's real 60C / 220C. Merging
        the chain ourselves (child wins over parent) hands the slicer every
        value explicitly, so the temperatures are the manufacturer's, not a
        fallback. Falls back to the raw file if anything goes wrong.
        """
        import json as _json
        try:
            idx = self._filament_index()
            merged: dict = {}
            data = _json.loads(fil_path.read_text(encoding="utf-8"))
            name = data.get("name")
            steps = 0
            while name and steps < 12:
                node = _json.loads(idx[name].read_text(encoding="utf-8")) if name in idx else data
                for k, v in node.items():
                    merged.setdefault(k, v)
                name = node.get("inherits")
                steps += 1
            if not merged.get("nozzle_temperature"):
                return fil_path
            merged["name"] = f"Aethelark {data.get('name', 'filament')} (flat)"
            merged.pop("inherits", None)
            flat = out_dir / "_flat_filament.json"
            flat.write_text(_json.dumps(merged), encoding="utf-8")
            return flat
        except Exception:
            return fil_path

    def slice(self, model_path: Path, config: Optional[SliceConfig] = None) -> SliceResult:
        model_path = Path(model_path)
        if not model_path.exists():
            raise FileNotFoundError(f"Model file not found: {model_path}")

        cfg = config or SliceConfig()
        out_dir = cfg.output_dir or (Path.home() / "Downloads" / "Aethelark3D" / "sliced")
        out_dir.mkdir(parents=True, exist_ok=True)

        # A downloaded .3mf is usually a project saved for another printer.
        # Its geometry and painted supports are kept; its printer, process and
        # filament settings are replaced with this printer's (_project_3mf).
        # Only if that cannot be sliced does it fall back to the bare mesh.
        source_path = model_path
        project = self._creator_project(model_path)
        creator_thumb = self._creator_thumbnail(model_path, out_dir)

        exe = find_slicer()
        if exe is None:
            raise SlicerMissing("Printing needs ElegooSlicer to prepare the model, "
                                "and it is not installed on this computer.")

        # The machine profile follows the printer's reported model, looked up
        # in the installed slicer (printer_models) -- never the fleet key,
        # which for a discovered printer names no model at all.
        pkey = getattr(cfg, "printer_key", "") or ""
        model, profile = profile_for(pkey)
        machine_family = profile.family
        default_machine = profile.machine
        default_process = profile.process

        machine_name = cfg.machine_name if cfg.machine_name and "Centauri Carbon" not in cfg.machine_name else default_machine
        process_profile = cfg.process_profile if cfg.process_profile and "@Elegoo CC" not in cfg.process_profile else default_process

        machine_json = self.datadir / "system" / "Elegoo" / "machine" / machine_family / f"{machine_name}.json"
        process_json = self.datadir / "system" / "Elegoo" / "process" / machine_family / f"{process_profile}.json"
        if not machine_json.exists():
            raise ProfileMissing(f"ElegooSlicer's profile {machine_name!r} is missing.",
                                 "Nothing was sliced. Tell the user to reinstall or "
                                 "update ElegooSlicer, then ask again.")
        machine_flat = self._flat_system_profile(machine_json, "machine", out_dir)

        fil_name = f"Elegoo PLA @{machine_family}"
        fil_color = (220, 50, 50)
        fil_flat = None
        if cfg.filament_query:
            from aethelark3d.filaments.resolver import apply_overrides, plan as _plan_filament
            fil_plan = _plan_filament(cfg.filament_query, machine_family,
                                      nozzle=cfg.nozzle_temp, bed=cfg.bed_temp, printer=model)
            fil_name, fil_path = fil_plan.name, fil_plan.path
            if fil_path and fil_path.exists():
                # --load-filaments keeps only a leaf's own keys, so the chain is
                # flattened first; the plan's brand settings and temperatures
                # are then written over it.
                import json as _json
                fil_flat = self._flattened_filament(fil_path, out_dir)
                body = apply_overrides(_json.loads(fil_flat.read_text(encoding="utf-8")), fil_plan)
                fil_flat = out_dir / "_flat_filament.json"
                fil_flat.write_text(_json.dumps(body), encoding="utf-8")

        # The build plate decides the bed temperature, and with none named the
        # slicer defaults to "Cool Plate". Textured PEI is what these machines
        # ship with.
        bed_type = getattr(cfg, "bed_type", None) or "Textured PEI Plate"

        def process_with(overrides: dict) -> Optional[Path]:
            if not process_json.exists():
                return None
            return self._flat_system_profile(process_json, "process", out_dir, overrides)

        plate_gcode = out_dir / "plate_1.gcode"
        plate_gcode.unlink(missing_ok=True)
        result = None
        if project is not None:
            overrides = (self._support_settings(cfg.supports) if cfg.supports
                         else self._creator_settings(project, cfg.filament_query or ""))
            plain = process_with({})
            try:
                baseline = self._baseline(exe, [machine_flat] + ([plain] if plain else []),
                                          fil_flat, bed_type, out_dir)
            except Exception as e:
                print(f"[slicer] no baseline for this printer and filament ({e})", file=sys.stderr)
                baseline = {}
            process_flat = process_with(overrides)
            try:
                if not baseline:
                    raise RuntimeError("the printer's own settings could not be resolved")
                packed = self._project_3mf(model_path, out_dir, project,
                                           [machine_flat] + ([process_flat] if process_flat else []),
                                           fil_flat, bed_type, baseline)
                result = self._run_project(exe, packed, out_dir)
            except Exception as e:
                print(f"[slicer] the project could not be sliced as a project ({e}); "
                      f"slicing its bare mesh instead", file=sys.stderr)
                result = None
            if result is not None and not plate_gcode.exists():
                result = None

        if result is None:
            support_mode = cfg.supports or "auto"
            model_path = self._geometry_only(model_path, out_dir)
            process_flat = process_with(self._support_settings(support_mode))
            cmd = [exe, "--datadir", str(self.datadir), "--slice", "0", "--outputdir", str(out_dir),
                   "--load-settings", ";".join(str(x) for x in (machine_flat, process_flat) if x)]
            if fil_flat:
                cmd.extend(["--load-filaments", str(fil_flat)])
            cmd.extend(["--curr-bed-type", bed_type, str(model_path)])
            result = subprocess.run(cmd, capture_output=True, text=True)
        model_path = source_path

        if not plate_gcode.exists():
            said = (result.stdout or "") + (result.stderr or "")
            if project is not None and self._plate_count(source_path) > 1:
                raise RuntimeError(
                    f"its file is laid out on several plates for another printer, and its "
                    f"first plate could not be prepared for the {model.name if model else 'printer'}")
            if "Nothing to be sliced" in said or "print volume" in said:
                raise RuntimeError(
                    f"it does not fit on the {model.name if model else 'printer'}, which prints up to "
                    f"{profile.bed_x:.0f} x {profile.bed_y:.0f} x {profile.height:.0f} mm")
            raise RuntimeError(f"the slicer could not prepare it ({said.strip()[-300:]})")

        # Parse layers, filament grams, and print time from G-code comments
        gcode_text = plate_gcode.read_text(encoding="latin1", errors="ignore")

        # Strip BED_MESH_CALIBRATE from the start gcode. Levelling is the
        # firmware's own sweep, run from `printer_check: true` in the 1020 start
        # command (drivers/elegoo.py) — exactly like a genuine ElegooSlicer print
        # with Auto-levelling ON, whose gcode carries NO BED_MESH_CALIBRATE
        # (verified against real ElegooSlicer exports, 2026-09-12). Elegoo's CC2
        # machine profile injects a BED_MESH line anyway; leaving it in put a
        # SECOND mesh next to the firmware's and the CC2 hung in AUTO_LEVELING
        # for 8+ minutes. One leveller — the firmware's.
        stripped = self._strip_start_bed_mesh(gcode_text)
        if stripped != gcode_text:
            plate_gcode.write_text(stripped, encoding="latin1")
            gcode_text = stripped

        # Apply Quasi-Static Thermal Annealing for low-Tg materials in enclosed fleet
        from aethelark3d.slicers.thermodynamics import apply_quasi_static_annealing
        processed_gcode, anneal_stats = apply_quasi_static_annealing(
            gcode_text=gcode_text,
            material=fil_name,
            is_enclosed=model.enclosed,
            cold_bed=self._cold_bed(fil_flat),
        )
        if anneal_stats.get("applied"):
            plate_gcode.write_text(processed_gcode, encoding="latin1")
            gcode_text = processed_gcode

        # Apply PURGEX Multi-Color Optimization if enabled
        if cfg.enable_purgex:
            from aethelark3d.slicers.purge import apply_purgex_multi_color_optimization
            ams_colors = cfg.ams_filaments or ["White", "Black", "Red", "Blue"]
            purgex_gcode, purgex_stats = apply_purgex_multi_color_optimization(
                gcode_text=gcode_text,
                ams_filament_colors=ams_colors,
                nozzle_diameter=0.4,
                enable_rose_tower=True,
                printer_key=pkey
            )
            if purgex_stats.get("toolchanges", 0) > 0:
                plate_gcode.write_text(purgex_gcode, encoding="latin1")
                gcode_text = purgex_gcode
        
        # The slicer's own figures, read from the comments it writes. The time
        # was a constant -- `time_sec = 840  # Default ~14m` -- so every print
        # was announced, and shown on the island, as fourteen minutes; the
        # mass and layers fell back to 9.2 g and 100. Not found now means
        # not known (None), never a number nobody measured.
        layers_m = re.search(r";\s*total layer number:\s*(\d+)", gcode_text, re.I)
        total_layers = int(layers_m.group(1)) if layers_m else None

        time_sec = _printing_seconds(gcode_text)

        mass_m = re.search(r";\s*total filament used \[g\]\s*=\s*([\d\.]+)", gcode_text, re.I) \
            or re.search(r";\s*filament used \[g\]\s*=\s*([\d\.]+)", gcode_text, re.I)
        filament_mass = float(mass_m.group(1)) if mass_m else None

        vol_m = re.search(r";\s*filament used \[cm3\]\s*=\s*([\d\.,\s]+)", gcode_text, re.I)
        vol_cm3 = (sum(float(v) for v in re.findall(r"[\d\.]+", vol_m.group(1)))
                   if vol_m else None)

        # Generate 3D Thumbnails
        thumbs = ([creator_thumb] if creator_thumb else
                  self.generate_thumbnails(model_path, out_dir / "temp_thumbs", color_rgb=fil_color))

        slice_res = SliceResult(
            gcode_path=plate_gcode,
            model_name=model_path.name,
            filament_name=fil_name,
            filament_mass_grams=filament_mass,
            total_layers=total_layers,
            estimated_time_seconds=time_sec,
            raw_volume_cm3=vol_cm3,
            thumbnail_paths=thumbs
        )

        # Package into finalized container
        final_package = self.package_container(plate_gcode, model_path, slice_res, printer_key=pkey)
        slice_res.gcode_path = final_package
        return slice_res

    @staticmethod
    def _strip_start_bed_mesh(gcode_text: str) -> str:
        """Remove BED_MESH_CALIBRATE lines so the firmware's printer_check sweep
        is the ONLY levelling — matching genuine ElegooSlicer output, whose gcode
        has none. Replaced with a comment so line-based tooling stays aligned."""
        out = []
        for line in gcode_text.split("\n"):
            if line.strip().startswith("BED_MESH_CALIBRATE"):
                out.append("; BED_MESH_CALIBRATE removed by Aethelark-3D "
                           "- levelling is the firmware printer_check sweep")
            else:
                out.append(line)
        return "\n".join(out)

    @staticmethod
    def _embed_thumbnail(gcode_text: str, plate_png_path: Path) -> str:
        """Inject the 144x144 PNG thumbnail block the Elegoo firmware reads for
        its file browser.

        Measured 2026-09-11 on a real Centauri Carbon 2: the preview, layer
        count, time and weight the printer shows come from THIS block, embedded
        in the gcode itself — not from a Metadata/plate_1.png inside a zip. A
        gcode with no block shows a placeholder icon and "0" layers and will not
        run. Headless `elegoo --slice` writes the `; thumbnails = 144x144/PNG`
        config line but no actual image (it has no GL context to render one), so
        we render and inject it here, matching ElegooSlicer's own on-wire format:
        `; THUMBNAIL_BLOCK_START` / `; thumbnail begin 144x144 <b64-char-count>`,
        base64 wrapped at 78 chars per `; `-prefixed line, right after the header.
        """
        try:
            import base64
            from PIL import Image
            img = Image.open(plate_png_path).convert("RGBA").resize((144, 144), Image.LANCZOS)
            bio = io.BytesIO()
            img.save(bio, format="PNG", optimize=True)
            b64 = base64.b64encode(bio.getvalue()).decode()
        except Exception:
            return gcode_text  # a preview is cosmetic; never block a print on it
        body = "\n".join("; " + b64[i:i + 78] for i in range(0, len(b64), 78))
        block = (
            "; THUMBNAIL_BLOCK_START\n"
            f"; thumbnail begin 144x144 {len(b64)}\n"
            f"{body}\n"
            "; thumbnail end\n"
            "; THUMBNAIL_BLOCK_END\n"
        )
        marker = "; HEADER_BLOCK_END\n"
        if marker in gcode_text:
            return gcode_text.replace(marker, marker + "\n" + block, 1)
        return block + gcode_text

    def package_container(self, gcode_path: Path, model_path: Path, result: SliceResult, printer_key: str = "CC1") -> Path:
        """Finalise the printable file as RAW gcode with an embedded thumbnail."""
        out_dir = gcode_path.parent
        clean_stem = "".join(c if c.isalnum() or c in "_-" else "_" for c in model_path.stem).strip("_")
        clean_fil = "".join(c if c.isalnum() or c in "_-" else "_" for c in result.filament_name).strip("_")

        # The same family the slice used, so the name the printer shows and
        # the profile that produced the G-code cannot disagree.
        prefix = profile_for(printer_key)[1].family

        final_name = f"{prefix}_0.4_{clean_stem}_{clean_fil}.gcode"
        target_path = out_dir / final_name

        gcode_text = gcode_path.read_text(encoding="latin1", errors="ignore")
        if result.thumbnail_paths:
            gcode_text = self._embed_thumbnail(gcode_text, result.thumbnail_paths[0])
        target_path.write_text(gcode_text, encoding="latin1", errors="ignore")
        return target_path


# Module-level convenience aliases
_default_backend = ElegooSlicerBackend()

def resolve_filament_profile(query: str, printer: str = "CC1") -> Tuple[str, Path, Dict[str, Any]]:
    return _default_backend.resolve_filament_profile(query, printer=printer)

def slice_headless(
    model_path: Path,
    filament_query: Optional[str] = None,
    ams_filaments: Optional[List[str]] = None,
    enable_purgex: bool = True,
    printer_key: str = "CC1",
    **kwargs
) -> Path:
    cfg = SliceConfig(
        printer_key=printer_key,
        filament_query=filament_query,
        ams_filaments=ams_filaments,
        enable_purgex=enable_purgex,
        nozzle_temp=kwargs.get("nozzle_temp"),
        bed_temp=kwargs.get("bed_temp"),
        supports=kwargs.get("supports"),
    )
    res = _default_backend.slice(model_path, cfg)
    return res.gcode_path

def open_gui(file_path: Path) -> subprocess.Popen:
    exe = find_slicer()
    if exe is None:
        raise SlicerMissing("ElegooSlicer is not installed on this computer.")
    return subprocess.Popen([exe, str(file_path)])
