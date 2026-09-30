"""
Aethelark-3D Configuration Management.
Persists settings, printer fleet, and authentication in ~/.config/aethelark3d/config.json
"""

import json
import os
import shutil
from pathlib import Path
from typing import Optional, Dict, Any, List

DEFAULT_CONFIG_DIR = Path.home() / ".config" / "aethelark3d"
DEFAULT_DOWNLOAD_DIR = Path.home() / "Downloads" / "Aethelark3D"
CONFIG_FILE = DEFAULT_CONFIG_DIR / "config.json"

#: The Elegoo line-up as sample printers, for tests and the simulator. Never a
#: user's default: a fleet is what discovery finds on their network.
DEMO_FLEET = {
    "CC1": {
        "name": "Elegoo Centauri Carbon 1 (No AMS)",
        "model": "Elegoo Centauri Carbon",
        # Shipped blank on purpose. A default address is a claim
        # about the buyer's network that cannot be true — on theirs
        # this is either nothing or somebody else's device.
        "host": "",
        "nozzle": "0.4",
        "enclosed": True,
        "has_ams": False
    },
    "CC2": {
        "name": "Elegoo Centauri Carbon 2 (No AMS)",
        "model": "Elegoo Centauri Carbon 2",
        "host": "",
        "nozzle": "0.4",
        "enclosed": True,
        "has_ams": False
    },
    "C2": {
        "name": "Elegoo Centauri 2 (No AMS)",
        "model": "Elegoo Centauri 2",
        "host": "",
        "nozzle": "0.4",
        "enclosed": False,
        "has_ams": False
    },
    "CC1_COMBO": {
        "name": "Elegoo Centauri Carbon 1 Combo (4-Slot AMS)",
        "model": "Elegoo Centauri Carbon",
        "host": "",
        "nozzle": "0.4",
        "enclosed": True,
        "has_ams": True,
        "ams_slots": [
            {"slot": 1, "color": "Black", "material": "PLA Basic", "brand": "Elegoo"},
            {"slot": 2, "color": "Red", "material": "Silk PLA", "brand": "Elegoo"},
            {"slot": 3, "color": "White", "material": "Rapid PLA+", "brand": "Elegoo"},
            {"slot": 4, "color": "Blue", "material": "PETG", "brand": "Elegoo"}
        ]
    },
    "C2_COMBO": {
        "name": "Elegoo Centauri 2 Combo (4-Slot AMS)",
        "model": "Elegoo Centauri 2",
        "host": "",
        "nozzle": "0.4",
        "enclosed": False,
        "has_ams": True,
        "ams_slots": [
            {"slot": 1, "color": "Red", "material": "Silk PLA", "brand": "Elegoo"},
            {"slot": 2, "color": "Black", "material": "PLA Basic", "brand": "Elegoo"},
            {"slot": 3, "color": "White", "material": "PLA+", "brand": "Elegoo"},
            {"slot": 4, "color": "Blue", "material": "Silk PLA", "brand": "Elegoo"}
        ]
    },
    "CC2_COMBO": {
        "name": "Elegoo Centauri Carbon 2 Combo (4-Slot AMS)",
        "model": "Elegoo Centauri Carbon 2",
        "host": "",
        "nozzle": "0.4",
        "enclosed": True,
        "has_ams": True,
        "ams_slots": [
            {"slot": 1, "color": "Black", "material": "Rapid PLA+", "brand": "Elegoo"},
            {"slot": 2, "color": "Grey", "material": "PETG-CF", "brand": "Elegoo"},
            {"slot": 3, "color": "White", "material": "PLA Silk", "brand": "Elegoo"},
            {"slot": 4, "color": "Orange", "material": "TPU 95A", "brand": "Elegoo"}
        ]
    }
}


class Config:
    def __init__(self, config_dir: Optional[Path] = None):
        self.config_dir = config_dir or DEFAULT_CONFIG_DIR
        self.config_file = self.config_dir / "config.json"
        self._data: Dict[str, Any] = {
            "makerworld_cookie": "",
            "makerworld_token": "",
            "download_dir": str(DEFAULT_DOWNLOAD_DIR),
            "slicer_path": "",
            "default_printer": "",
            "printers": {}
        }
        self.load()
        self._migrate_existing_session()

    def _migrate_existing_session(self) -> None:
        """Auto-migrate MakerWorld session from previous setup if available."""
        old_config = Path.home() / ".config" / "makerworld" / "config.json"
        if old_config.exists() and not self.makerworld_cookie:
            try:
                with open(old_config, "r", encoding="utf-8") as f:
                    old_data = json.load(f)
                    if old_data.get("cookie"):
                        self.makerworld_cookie = old_data["cookie"]
                    if old_data.get("token"):
                        self.makerworld_token = old_data["token"]
            except Exception:
                pass

    def load(self) -> None:
        if self.config_file.exists():
            try:
                with open(self.config_file, "r", encoding="utf-8") as f:
                    stored = json.load(f)
                    self._data.update(stored)
                    if "printers" in stored and isinstance(stored["printers"], dict):
                        self._data["printers"] = dict(stored["printers"])
            except Exception as e:
                print(f"[Warning] Failed to load config from {self.config_file}: {e}")

    def save(self) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.config_dir, 0o700)
        except Exception:
            pass

        with open(self.config_file, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2)

        try:
            os.chmod(self.config_file, 0o600)
        except Exception:
            pass

    @property
    def costing(self) -> Dict[str, Any]:
        """What things cost this operator, in lei. Empty until he says.

        Deliberately not seeded with defaults: `costing.Rates` holds those, and
        it needs to know which figures came from a person so a quote can say
        which parts of itself are measured and which are assumed.
        """
        stored = self._data.get("costing")
        return dict(stored) if isinstance(stored, dict) else {}

    def set_rate(self, name: str, value: Any) -> Dict[str, Any]:
        """Record one costing figure and persist it."""
        stored = self.costing
        stored[str(name)] = value
        self._data["costing"] = stored
        self.save()
        return stored

    def set_filament_price(self, material: str, lei_per_kg: float) -> Dict[str, Any]:
        """What one material costs per kilo, e.g. PETG at 70."""
        stored = self.costing
        prices = dict(stored.get("filament_prices") or {})
        prices[str(material).upper()] = float(lei_per_kg)
        stored["filament_prices"] = prices
        self._data["costing"] = stored
        self.save()
        return prices

    @property
    def slicer_path(self) -> str:
        """Where ElegooSlicer is, when it is not somewhere it can be found."""
        return str(self._data.get("slicer_path") or "")

    @slicer_path.setter
    def slicer_path(self, val: str) -> None:
        self._data["slicer_path"] = str(val or "").strip()
        self.save()

    @property
    def makerworld_cookie(self) -> str:
        return self._data.get("makerworld_cookie", "")

    @makerworld_cookie.setter
    def makerworld_cookie(self, val: str) -> None:
        self._data["makerworld_cookie"] = val.strip()
        self.save()

    @property
    def makerworld_token(self) -> str:
        return self._data.get("makerworld_token", "")

    @makerworld_token.setter
    def makerworld_token(self, val: str) -> None:
        self._data["makerworld_token"] = val.strip()
        self.save()

    @property
    def download_dir(self) -> Path:
        p = Path(self._data.get("download_dir", str(DEFAULT_DOWNLOAD_DIR))).expanduser()
        p.mkdir(parents=True, exist_ok=True)
        return p

    @download_dir.setter
    def download_dir(self, val: str) -> None:
        self._data["download_dir"] = str(Path(val).expanduser().resolve())
        self.save()

    @property
    def default_printer(self) -> str:
        """The printer to use when none was named."""
        printers = self._data.get("printers", {}) or {}
        chosen = str(self._data.get("default_printer") or "").strip().upper()
        if chosen and chosen in printers:
            return chosen
        return next(iter(printers), "")

    @default_printer.setter
    def default_printer(self, val: str) -> None:
        self._data["default_printer"] = val.strip().upper()
        self.save()

    @property
    def printers(self) -> Dict[str, Any]:
        return self._data.get("printers", {})

    def addressable_printer_keys(self) -> list:
        """Configured printers that have somewhere to be reached, in config order."""
        addressable = [k for k, p in self.printers.items()
                       if isinstance(p, dict) and str(p.get("host") or "").strip()]
        return addressable or list(self.printers)

    def get_fleet(self) -> Dict[str, Any]:
        return self.printers

    def get_printer(self, printer_key: str) -> Optional[Dict[str, Any]]:
        key = printer_key.upper().replace("-", "_")
        slot = self.printers.get(key)
        if slot is not None:
            return slot
        # A callsign the user (or the voice model) used — "CC2", "Zeus" — that
        # is not itself a fleet key. Resolve it so every caller that looks a
        # printer up by name (the driver factory, the print pre-flight, status)
        # accepts what a person actually says, not only the internal key.
        alias_target = self._data.get("aliases", {}).get(key)
        if alias_target:
            return self.printers.get(str(alias_target).upper().replace("-", "_"))
        named = self.key_for(printer_key)
        return self.printers.get(named) if named else None

    def key_for(self, said: str) -> Optional[str]:
        """The fleet key for a key, alias or name as said ("Centauri Carbon"),
        or None when nothing, or more than one printer, answers to it."""
        if not said:
            return None
        from .aliases import PrinterAliasManager
        key = PrinterAliasManager.resolve_to_key(str(said))
        return key if key in self.printers else None

    def set_printer_ip(self, printer_key: str, ip_address: str) -> None:
        key = printer_key.upper().replace("-", "_")
        if key in self._data["printers"]:
            self._data["printers"][key]["host"] = ip_address.strip()
            self.save()

    def set_access_code(self, printer_key: str, access_code: str) -> bool:
        """Store the code a printer's local API demands, and drop the flag
        that said it was missing.

        The CC2's access code is not optional and not a security toggle: it is
        the printer's password for its own LAN API, always required, and shown
        on the printer's touchscreen. Once it is stored, the driver stops being
        blocked and `needs_access_code` is no longer true.
        """
        key = printer_key.upper().replace("-", "_")
        slot = self._data["printers"].get(key)
        if slot is None:
            return False
        slot["access_code"] = access_code.strip()
        slot["needs_access_code"] = False
        self.save()
        return True

    @property
    def aliases(self) -> Dict[str, str]:
        return self._data.get("aliases", {})

    def get_aliases(self) -> Dict[str, str]:
        return self.aliases

    def set_alias(self, alias: str, printer_key: str) -> Dict[str, str]:
        clean_alias = alias.strip().upper()
        clean_key = printer_key.strip().upper().replace("-", "_")
        if "aliases" not in self._data:
            self._data["aliases"] = {}

        # Remove existing alias pointing to this key if any
        existing = [k for k, v in self._data["aliases"].items() if v == clean_key and k != clean_alias]
        for old in existing:
            del self._data["aliases"][old]

        self._data["aliases"][clean_alias] = clean_key
        self.save()
        return self._data["aliases"]

    def remove_alias(self, alias: str) -> bool:
        clean_alias = alias.strip().upper()
        if "aliases" in self._data and clean_alias in self._data["aliases"]:
            del self._data["aliases"][clean_alias]
            self.save()
            return True
        return False

    def remove_printer(self, printer_key: str) -> bool:
        """Remove a printer from the fleet by key.

        Follows all references in the same change: cleans up any aliases
        pointing to this printer, updates default_printer if it matched,
        and safely unmounts any spools back to shelf inventory.
        """
        key = printer_key.strip().upper().replace("-", "_")
        if "printers" in self._data and key in self._data["printers"]:
            del self._data["printers"][key]

            # 1. Clean up aliases pointing to this printer
            if "aliases" in self._data and isinstance(self._data["aliases"], dict):
                dangling = [al for al, pkey in self._data["aliases"].items() if pkey == key]
                for al in dangling:
                    del self._data["aliases"][al]

            # 2. Update default_printer if it pointed to this printer
            if self.default_printer == key:
                remaining = list(self._data["printers"].keys())
                self._data["default_printer"] = remaining[0] if remaining else ""

            # 3. Save updated config
            self.save()

            # 4. Safely unload any spools mounted on this printer back to shelf
            try:
                from .spools import FilamentVault
                vault_data = FilamentVault._load_data()
                if key in vault_data.get("mounted", {}):
                    import time
                    for slot_key, spool in list(vault_data["mounted"][key].items()):
                        spool["mounted_to"] = None
                        spool["unloaded_at"] = int(time.time())
                        vault_data["shelf"].append(spool)
                    del vault_data["mounted"][key]
                    FilamentVault._save_data(vault_data)
            except Exception:
                pass

            return True
        return False

    def to_dict(self) -> Dict[str, Any]:
        return dict(self._data)


config = Config()
