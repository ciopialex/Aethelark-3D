"""
Printer Driver Factory for Aethelark-3D.
Resolves and instantiates hardware drivers dynamically based on printer brand.
"""

from typing import Dict, Type
from aethelark3d.config import config
from aethelark3d.drivers.base import BasePrinterDriver, PrinterCapability
from aethelark3d.drivers.elegoo import ElegooSDCPDriver
from aethelark3d.drivers.simulator import VirtualPrinterDriver
from aethelark3d.printer_models import has_canvas, model_of


_DRIVER_REGISTRY: Dict[str, Type[BasePrinterDriver]] = {
    "elegoo": ElegooSDCPDriver,
    "simulator": VirtualPrinterDriver,
    "virtual": VirtualPrinterDriver
}


_ACTIVE_SIMULATORS: Dict[str, VirtualPrinterDriver] = {}


#: SDCP on 3030 is the older, wider-supported path, so an unrecognised machine
#: is tried there rather than on a protocol only newer hardware answers.
DEFAULT_TRANSPORT = ("sdcp", 3030)

#: Default port per protocol, for a config that names a protocol but no port.
FAMILY_DEFAULT_PORT: dict[str, tuple[str, int]] = {
    "elegoolink": ("elegoolink", 80),
    "sdcp":       ("sdcp", 3030),
    "moonraker":  ("moonraker", 7125),
    "octoprint":  ("octoprint", 80),
    "prusalink":  ("prusalink", 80),
    "bambu":      ("bambu", 8883),
}


def resolve_transport(cfg: dict) -> tuple[str, int]:
    """The protocol and port for this printer entry.

    An explicit `port` in config always wins: a machine moved behind a proxy or
    a forwarded port is the operator's business.
    """
    # An explicit protocol from discovery wins over deriving it from the model.
    # A printer probed before identity was recorded stored model "Elegoo
    # 7526B5", which names no model, so without this it fell through to the
    # SDCP default and a CC2 was driven on the wrong protocol.
    explicit = str(cfg.get("protocol") or "").strip().lower()
    if explicit:
        _, default_port = FAMILY_DEFAULT_PORT.get(explicit, DEFAULT_TRANSPORT)
        port = int(cfg["port"]) if cfg.get("port") else default_port
        return explicit, port
    # The model says how it is spoken to: a Centauri Carbon is SDCP on 3030
    # wherever it is plugged in, a Centauri Carbon 2 is ElegooLink on 80.
    model = model_of(cfg)
    protocol, port = (FAMILY_DEFAULT_PORT.get(model.protocol, DEFAULT_TRANSPORT)
                      if model else DEFAULT_TRANSPORT)
    if cfg.get("port"):
        port = int(cfg["port"])
    return protocol, port


def is_addressable(cfg: dict) -> bool:
    """Whether this entry names a machine we could actually reach.

    A declared printer with no host is a slot the user has not filled in, and
    saying so beats dialling 127.0.0.1 and reporting a connection failure that
    describes nothing.
    """
    return bool(str(cfg.get("host") or "").strip())


def reset_simulators():
    """Reset cached digital twin instances to pristine state."""
    _ACTIVE_SIMULATORS.clear()


def register_driver(brand: str, driver_cls: Type[BasePrinterDriver]):
    """Register a new brand driver (e.g. Bambu, Prusa, Klipper) at runtime."""
    _DRIVER_REGISTRY[brand.lower()] = driver_cls


def get_driver_for_printer(
    printer_name: str,
    use_simulator: bool = False,
    physics_speed: float = 1.0,
    fresh: bool = False
) -> BasePrinterDriver:
    """Instantiate and return the appropriate hardware driver or Digital Twin for a printer."""
    key = printer_name.upper().replace("-", "_")
    is_sim = use_simulator or key.startswith("VIRTUAL_") or key in ["SIM", "VIRTUAL"]

    if is_sim:
        if fresh or key not in _ACTIVE_SIMULATORS:
            _ACTIVE_SIMULATORS[key] = VirtualPrinterDriver(name=printer_name, physics_speed=physics_speed)
        return _ACTIVE_SIMULATORS[key]

    cfg = config.get_printer(printer_name)
    if cfg is None:
        raise KeyError(f"There is no printer called {printer_name!r}.")
    brand = cfg.get("brand", "Elegoo").lower()

    model = model_of(cfg, key)
    canvas = has_canvas(cfg, key)
    enclosed = cfg.get("enclosed", model.enclosed if model else True)
    has_ams = canvas
    slots = cfg.get("ams_slots", [])
    ams_count = (len(slots) if slots else int(cfg.get("canvas_slots") or 4)) if canvas else 1
    max_nozzle = model.max_nozzle_temp if model else 300.0
    max_bed = model.max_bed_temp if model else 100.0
    nozzle_mat = model.nozzle_material if model else "hardened_steel"

    caps = PrinterCapability(
        brand=cfg.get("brand", "Elegoo"),
        model=(model.printer_model if model else cfg.get("model", "Centauri Carbon")),
        nozzle_diameter=float(cfg.get("nozzle", 0.4)),
        nozzle_material=nozzle_mat,
        max_nozzle_temp=max_nozzle,
        max_bed_temp=max_bed,
        is_enclosed=enclosed,
        has_ams=has_ams,
        ams_slot_count=ams_count
    )

    driver_cls = _DRIVER_REGISTRY.get(brand, ElegooSDCPDriver)
    return driver_cls(
        name=printer_name,
        ip=cfg.get("host") or "127.0.0.1",
        port=resolve_transport(cfg)[1],
        capabilities=caps,
        access_code=cfg.get("access_code")
    )
