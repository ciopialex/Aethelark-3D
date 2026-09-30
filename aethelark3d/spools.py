"""
Aethelark-3D Multi-Slot AMS & Filament Inventory Vault.
Ultra-lightweight JSON-backed spool memory for active AMS slots and shelf storage.
"""

import json
import re
import uuid
import time
from pathlib import Path
from typing import Dict, Any, List, Tuple, Optional
from .config import config

#: What a full spool holds when the user does not say. 1 kg is what nearly
#: every PLA and PETG spool is sold as.
FULL_SPOOL_GRAMS = 1000.0


def inventory_file() -> Path:
    """Where the spools are kept: beside the printer config, wherever that is.

    This was a constant fixed to ~/.config at import, so anything that pointed
    the config somewhere else -- a second profile, a sandbox, the test suite --
    moved the printers and left the spools behind in the real home folder. The
    test suite did exactly that: every run added its demo spools to the
    operator's own inventory, 360 of them before anyone noticed.
    """
    return Path(config.config_dir) / "inventory.json"


def filament_grams_from_gcode(gcode_path) -> List[float]:
    """Grams of each filament the sliced job uses, in slot order.

    The slicer writes `; filament used [g] = 12.34` -- or one value per
    filament, comma separated, for a multicolour job. That is the only weight
    that describes THIS print; the one a model site lists is for its own
    settings, and is often missing. Empty when the file does not say.
    """
    try:
        text = Path(gcode_path).read_text(encoding="latin1", errors="ignore")
    except OSError:
        return []
    m = re.search(r";\s*filament used \[g\]\s*=\s*([\d.,\s]+)", text, re.I)
    if not m:
        return []
    grams = []
    for part in m.group(1).split(","):
        try:
            grams.append(float(part.strip()))
        except ValueError:
            continue
    return grams


class FilamentVault:
    """
    Ultra-lightweight persistent filament database (< 5 KB footprint).
    Tracks spools across physical shelf inventory and multi-slot AMS units.
    """
    @classmethod
    def _load_data(cls) -> Dict[str, Any]:
        path = inventory_file()
        if not path.exists():
            return {
                "shelf": [],
                "mounted": {}
            }
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {"shelf": [], "mounted": {}}

    @classmethod
    def _save_data(cls, data: Dict[str, Any]) -> None:
        path = inventory_file()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print(f"[Aethelark-3D] Inventory save warning: {e}")

    @classmethod
    def load_spool(
        cls,
        printer_key: str,
        slot: int = 1,
        material: str = "PLA",
        color: str = "",
        grams: Optional[float] = None,
        is_used: bool = False,
        is_new: bool = False,
        size_grams: Optional[float] = None,
        nozzle_temp: Optional[int] = None,
        bed_temp: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Put a spool on a printer's slot (1-4 on a multicolour unit, 1 otherwise).

        Three ways to say what it holds, and the caller must pick one:
          is_new   -- a sealed spool: `size_grams` (1 kg when unsaid) is left.
          grams    -- this much is left on it.
          is_used  -- the same material and colour, taken back off the shelf.
        """
        pkey = printer_key.upper().replace("-", "_")
        data = cls._load_data()
        slot_key = f"slot_{slot}"
        color = (color or "").strip()

        # 1. Only an explicit "the one from the shelf" takes a spool back.
        matched_shelf_index = None
        if is_used and data["shelf"]:
            mat_clean = material.lower()
            col_clean = color.lower()
            for idx, item in enumerate(data["shelf"]):
                if (item["material"].lower() in mat_clean or mat_clean in item["material"].lower()) and \
                   (not col_clean or item["color"].lower() == col_clean
                    or col_clean in item["color"].lower()):
                    matched_shelf_index = idx
                    break

        if matched_shelf_index is not None:
            spool = data["shelf"].pop(matched_shelf_index)
            spool["mounted_to"] = {"printer": pkey, "slot": slot}
            spool["last_loaded"] = int(time.time())
            matched_from_shelf = True
        else:
            size = float(size_grams) if size_grams else FULL_SPOOL_GRAMS
            remaining = size if (is_new or grams is None) else float(grams)
            spool = {
                "id": str(uuid.uuid4())[:8],
                "material": material,
                "color": color,
                "initial_grams": max(size, remaining),
                "remaining_grams": remaining,
                "mounted_to": {"printer": pkey, "slot": slot},
                "last_loaded": int(time.time())
            }
            matched_from_shelf = False

        if nozzle_temp:
            spool["nozzle_temp"] = int(nozzle_temp)
        if bed_temp:
            spool["bed_temp"] = int(bed_temp)
        if pkey not in data["mounted"]:
            data["mounted"][pkey] = {}
        data["mounted"][pkey][slot_key] = spool
        cls._save_data(data)

        spool["matched_from_shelf"] = matched_from_shelf
        return spool

    @classmethod
    def unload_spool(cls, printer_key: str, slot: int = 1) -> Optional[Dict[str, Any]]:
        pkey = printer_key.upper().replace("-", "_")
        data = cls._load_data()
        slot_key = f"slot_{slot}"

        if pkey in data["mounted"] and slot_key in data["mounted"][pkey]:
            spool = data["mounted"][pkey].pop(slot_key)
            if not data["mounted"][pkey]:
                del data["mounted"][pkey]
            spool["mounted_to"] = None
            spool["unloaded_at"] = int(time.time())
            data["shelf"].append(spool)
            cls._save_data(data)
            return spool
        return None

    @classmethod
    def get_mounted_spool(cls, printer_key: str, slot: int = 1) -> Optional[Dict[str, Any]]:
        pkey = printer_key.upper().replace("-", "_")
        data = cls._load_data()
        slot_key = f"slot_{slot}"
        return data.get("mounted", {}).get(pkey, {}).get(slot_key)

    @classmethod
    def get_printer_slots(cls, printer_key: str) -> Dict[str, Any]:
        pkey = printer_key.upper().replace("-", "_")
        data = cls._load_data()
        return data.get("mounted", {}).get(pkey, {})

    @classmethod
    def get_active_spools(cls, printer_key: str) -> Dict[str, Any]:
        return cls.get_printer_slots(printer_key)

    @classmethod
    def get_shelf_inventory(cls) -> List[Dict[str, Any]]:
        data = cls._load_data()
        return data.get("shelf", [])

    @classmethod
    def deduct_usage(cls, printer_key: str, slot: int = 1, grams_used: float = 0.0) -> float:
        pkey = printer_key.upper().replace("-", "_")
        data = cls._load_data()
        slot_key = f"slot_{slot}"

        spool = data.get("mounted", {}).get(pkey, {}).get(slot_key)
        if spool:
            rem = max(0.0, spool.get("remaining_grams", 1000.0) - grams_used)
            spool["remaining_grams"] = round(rem, 1)
            cls._save_data(data)
            return rem
        return 0.0

    @classmethod
    def check_sufficiency(
        cls,
        printer_key: str,
        slot: int = 1,
        required_grams: float = 0.0
    ) -> Tuple[bool, float, str]:
        pkey = printer_key.upper().replace("-", "_")
        spool = cls.get_mounted_spool(pkey, slot=slot)
        if not spool:
            return True, 1000.0, f"No spool mapped in {pkey} Slot {slot}; safety gate bypassed."

        rem = spool.get("remaining_grams", 1000.0)
        buffer_needed = required_grams * 1.05

        if rem < buffer_needed:
            msg = (
                f"🛑 Insufficient filament in {pkey} Slot {slot}: Model requires {required_grams:.1f}g (buffer: {buffer_needed:.1f}g), "
                f"but only {rem:.1f}g of {spool.get('material')} ({spool.get('color')}) remains. Please swap spool."
            )
            return False, rem, msg

        msg = (
            f"✅ Filament sufficient in {pkey} Slot {slot}: Required {required_grams:.1f}g | Remaining {rem:.1f}g "
            f"({spool.get('material')} {spool.get('color')})."
        )
        return True, rem, msg


# Backward compatibility alias
SpoolLedger = FilamentVault
