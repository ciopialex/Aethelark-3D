"""
Aethelark-3D Printer Aliasing & Callsign Management.
Persists human-assigned friendly names (e.g., 'Zeus', 'Hera', 'Goliath')
and resolves them bidirectionally to machine keys (CC1, CC2, C2_COMBO) and IP addresses.
"""

import json
from pathlib import Path
from typing import Dict, Optional, List, Any
from aethelark3d.config import config, CONFIG_FILE


class PrinterAliasManager:
    """
    Manages custom user-defined printer names and callsigns.
    """

    @classmethod
    def get_aliases(cls) -> Dict[str, str]:
        """
        Return dict of {ALIAS_NAME: PRINTER_KEY}, e.g. {"ZEUS": "CC1", "GOLIATH": "C2_COMBO"}
        """
        return config.get_aliases()

    @classmethod
    def set_alias(cls, alias: str, printer_key: str) -> Dict[str, str]:
        """
        Assign a friendly human name to a physical printer key.
        """
        return config.set_alias(alias, printer_key)

    @classmethod
    def remove_alias(cls, alias: str) -> bool:
        """
        Delete an alias mapping.
        """
        return config.remove_alias(alias)

    @classmethod
    def resolve_to_key(cls, name_or_alias: str) -> str:
        """
        Given a human name, alias, or raw key, return the canonical printer_key.
        """
        query = name_or_alias.strip().upper().replace("-", "_")
        # How people say it: "my Centauri Carbon 2", "the CC2 printer".
        spoken = " ".join(w for w in query.replace("_", " ").split()
                          if w not in ("MY", "THE", "PRINTER", "ON", "A"))
        aliases = cls.get_aliases()

        # 1. Direct match in aliases
        if query in aliases:
            return aliases[query]

        # 2. Case-insensitive alias scan
        for al, pkey in aliases.items():
            if al.upper() == query:
                return pkey

        # 3. Direct canonical key match
        printers = config.get_fleet()
        if query in printers:
            return query

        # 4. The name, the model or the key as spoken. An exact match wins; a
        #    partial one only when it is the only one. "Centauri Carbon" is
        #    inside "Centauri Carbon 2", and taking the first partial match
        #    sent commands for one printer to the other.
        def norm(v) -> str:
            return " ".join(str(v or "").upper().replace("_", " ").replace("-", " ").split())

        said = norm(spoken or query)
        if said:
            if said.replace(" ", "_") in aliases:
                return aliases[said.replace(" ", "_")]
            for al, pkey in aliases.items():
                if norm(al) == said:
                    return pkey
            fields = {k: [norm(p.get("name")), norm(p.get("model")), norm(k)]
                      for k, p in printers.items()}
            exact = [k for k, f in fields.items() if said in f]
            if len(exact) == 1:
                return exact[0]
            partial = [k for k, f in fields.items() if any(said in x for x in f[:2] if x)]
            if len(partial) == 1:
                return partial[0]

        return query

    @classmethod
    def get_display_name(cls, printer_key: str) -> str:
        """
        Get the human callsign for a printer, falling back to config name or key.
        """
        clean_key = printer_key.strip().upper().replace("-", "_")
        aliases = cls.get_aliases()
        for al, pkey in aliases.items():
            if pkey == clean_key:
                return f"{al} ({clean_key})"

        pinfo = config.get_printer(clean_key)
        if pinfo and "name" in pinfo:
            return pinfo["name"]
        return clean_key
