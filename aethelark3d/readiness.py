"""Can the eagle drive this printer right now, and if not, what exactly is missing."""
from __future__ import annotations

import asyncio
from typing import Any, Dict

from .config import config


def check(key: str) -> Dict[str, Any]:
    slot = config.get_printer(key) or {}
    protocol = str(slot.get("protocol") or "").lower()
    name = slot.get("name") or key
    out: Dict[str, Any] = {"key": key, "name": name, "ready": False,
                           "needs_access_code": False, "why": ""}
    try:
        from .drivers.factory import get_driver_for_printer
        driver = get_driver_for_printer(key)
    except Exception as e:
        out["why"] = f"{type(e).__name__}: {e}"
        return out

    if getattr(driver, "is_cc2_family", False):
        import requests
        try:
            res = requests.get(f"http://{driver.ip}/system/info",
                               params={"X-Token": driver._effective_code}, timeout=2.0)
            if res.status_code == 200:
                out["ready"] = True
                return out
            if res.status_code in (401, 403):
                out["needs_access_code"] = True
                out["why"] = ("it asks for the access code shown in its network "
                              "settings" if not slot.get("access_code")
                              else "it refused the stored access code")
                return out
            out["why"] = f"its local API answered {res.status_code}"
        except Exception:
            out["why"] = ("its local API is off: turn on LAN Only in the printer's "
                          "network settings")
        return out

    async def _probe() -> bool:
        try:
            return bool(await driver.connect())
        finally:
            try:
                await driver.disconnect()
            except Exception:
                pass

    try:
        out["ready"] = asyncio.run(_probe())
    except Exception as e:
        out["why"] = f"{type(e).__name__}: {e}"
        return out
    if not out["ready"]:
        out["why"] = "it did not answer; check it is on and on this Wi-Fi"
    if protocol == "sdcp":
        out["needs_access_code"] = False
    return out


def guidance(checks: list[Dict[str, Any]]) -> str:
    if not checks:
        return ""
    if all(c["ready"] for c in checks):
        names = ", ".join(c["name"] for c in checks)
        return (f"Ready to print: {names}. Nothing else is needed; do not ask "
                f"for an access code.")
    parts = []
    for c in checks:
        if c["ready"]:
            parts.append(f"{c['name']} is ready.")
        elif c["needs_access_code"]:
            parts.append(f"{c['name']} needs the access code from its screen's "
                         f"network settings: ask the user to read it out, then "
                         f"call a3d_pair.")
        else:
            parts.append(f"{c['name']} is not usable yet: {c['why']}.")
    return " ".join(parts)
