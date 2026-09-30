"""Finding printers on the network, so nobody has to type an IP.

A printer's address is assigned by DHCP and changes — a router reboot, a move
between a home network and a phone hotspot, and the address written in the
config points at nothing. Until now that was terminal: the fleet was whatever
someone had typed, and a printer that moved simply became "offline" with no
way back that did not involve a human finding its IP and running `fleet
--set-ip`.

Discovery is a UDP broadcast, defined by SDCP v3.0.0: send `M99999` to port
3000 and every board on the segment answers with its identity.

    {"Id": "...", "Data": {"Name": "...", "MachineName": "...",
                           "BrandName": "...", "MainboardIP": "192.168.1.2",
                           "MainboardID": "000000000001d354",
                           "ProtocolVersion": "V3.0.0",
                           "FirmwareVersion": "V1.0.0"}}

`MainboardID` is the part that matters. It is burned into the board and does
not change when the address does, so it — not the IP, and not the name a user
can edit — is what identifies a printer across networks.

Nothing here talks to a printer beyond asking it to say who it is. No print is
started, nothing is uploaded, no state on the machine is touched.
"""
from __future__ import annotations

import json
import socket
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .printer_models import display_name

#: SDCP v3.0.0 device discovery.
DISCOVERY_PORT = 3000
DISCOVERY_PAYLOAD = b"M99999"

#: How long to listen. Boards answer within milliseconds on a quiet LAN; this
#: is generous because a busy printer answers late, not never.
DEFAULT_TIMEOUT = 2.0


@dataclass(frozen=True)
class Device:
    """One printer that answered, reduced to what is worth acting on."""
    mainboard_id: str
    ip: str
    name: str = ""
    machine: str = ""
    brand: str = ""
    firmware: str = ""
    protocol: str = ""
    raw: Dict[str, Any] = field(default_factory=dict, compare=False)

    @property
    def label(self) -> str:
        return self.name or self.machine or self.mainboard_id


def parse_reply(payload: bytes, sender_ip: str = "") -> Optional[Device]:
    """One UDP reply as a Device, or None if it is not one.

    The address is taken from the packet body when the board supplies it and
    from the sender otherwise: a board behind NAT reports an address that is
    right for its own segment and wrong for ours, and the socket knows where
    the packet actually came from.
    """
    try:
        blob = json.loads(payload.decode("utf-8", "replace"))
    except (ValueError, AttributeError):
        return None
    if not isinstance(blob, dict):
        return None
    data = blob.get("Data")
    if not isinstance(data, dict):
        return None

    board = str(data.get("MainboardID") or "").strip()
    ip = str(data.get("MainboardIP") or "").strip() or sender_ip
    if not board or not ip:
        return None
    return Device(
        mainboard_id=board,
        ip=ip,
        name=str(data.get("Name") or "").strip(),
        machine=str(data.get("MachineName") or "").strip(),
        brand=str(data.get("BrandName") or "").strip(),
        firmware=str(data.get("FirmwareVersion") or "").strip(),
        protocol=str(data.get("ProtocolVersion") or "").strip(),
        raw=blob,
    )


def _directed_broadcasts() -> List[str]:
    """x.y.z.255 for the address this machine uses to reach the LAN."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("10.255.255.255", 1))
            ip = s.getsockname()[0]
        finally:
            s.close()
        parts = ip.split(".")
        if len(parts) == 4 and not ip.startswith("127."):
            base = ".".join(parts[:3])
            return [f"{base}.255"] + [f"{base}.{n}" for n in range(1, 255)
                                      if f"{base}.{n}" != ip]
    except OSError:
        pass
    return []


def _known_hosts() -> List[str]:
    try:
        from .config import config as _cfg
        return [str(p.get("host")) for p in (_cfg.printers or {}).values()
                if isinstance(p, dict) and p.get("host")]
    except Exception:
        return []


def discover(timeout: float = DEFAULT_TIMEOUT,
             targets: Optional[Iterable[str]] = None,
             port: int = DISCOVERY_PORT,
             sweep_local: Optional[bool] = None,
             probe_timeout: float = 0.6) -> List[Device]:
    """Every SDCP board that answers, de-duplicated by mainboard id.

    `targets` exists so this can be pointed at a specific address instead of
    the broadcast one — a test can drive the real code against a loopback
    responder, and a user on a segment that drops broadcast can name a host.
    """
    addresses = list(targets) if targets else ["255.255.255.255", *_directed_broadcasts(),
                                                *_known_hosts()]
    found: Dict[str, Device] = {}

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(0.25)
        sock.bind(("", 0))

        broadcasts = [a for a in addresses if a.endswith(".255")]
        unicasts = [a for a in addresses if not a.endswith(".255")]

        def ask(targets_now):
            sock.setblocking(False)
            for address in targets_now:
                try:
                    sock.sendto(DISCOVERY_PAYLOAD, (address, port))
                except OSError:
                    continue
            sock.settimeout(0.25)

        ask(addresses)
        asks = 1
        next_ask = time.monotonic() + 0.4
        deadline = time.monotonic() + max(0.1, timeout)
        while time.monotonic() < deadline:
            if time.monotonic() >= next_ask:
                ask(broadcasts + (unicasts if asks < 2 else []))
                asks += 1
                next_ask = time.monotonic() + 0.4
            try:
                payload, sender = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            device = parse_reply(payload, sender_ip=sender[0] if sender else "")
            if device:
                found.setdefault(device.mainboard_id, device)
    finally:
        sock.close()

    # A broadcast only finds machines that implement the broadcast. Sweep the
    # local segments as well, so an ElegooLink printer — which answers no SDCP
    # at all, on any port — is found rather than silently absent. Merged on
    # mainboard id, so a machine that answers both ways is one machine and the
    # SDCP identity wins, being the richer of the two.
    # Only when nobody named an address. `targets` means "ask exactly these",
    # and a caller who says that does not want the rest of the segment as well
    # — including a test driving a fake board on loopback, which would
    # otherwise pick up whatever real printers happen to be in the room.
    if sweep_local is None:
        sweep_local = not targets
    if sweep_local:
        try:
            for device in sweep(timeout=probe_timeout):
                found.setdefault(device.mainboard_id, device)
        except Exception:
            pass        # a sweep that fails must not lose the broadcast result

    return sorted(found.values(), key=lambda d: (d.label.lower(), d.ip))


#: An ElegooLink machine's fingerprint, none of which needs a credential.
#:
#: The Centauri Carbon 2 does not speak SDCP at all. Measured 2026-09-10
#: against one sitting on the same subnet, powered on and printing:
#:
#:     M99999 -> 255.255.255.255:3000   no reply
#:     M99999 -> 192.168.8.255:3000     no reply
#:     M99999 -> 192.168.8.105:3000     no reply   (its real address)
#:     M99999 -> 192.168.8.105:3030     no reply
#:
#: while the same machine answered:
#:
#:     tcp/80    Server: libhv/1.3.4
#:     /system/info -> 401 Unauthorized   (the endpoint EXISTS, wants a token)
#:     tcp/8080  multipart/x-mixed-replace  (the camera)
#:     tcp/1883  MQTT CONNACK 5, not authorised
#:
#: So `discover()` could never find it, however long it listened. A 401 rather
#: than a 404 is the useful half: it says a printer is there and needs its
#: access code, which is a thing to tell the user rather than silence.
ELEGOOLINK_PORT = 80
ELEGOOLINK_SERVER_HINT = "libhv"
ELEGOOLINK_PROBE_PATH = "/system/info"


def _mac_for(ip: str) -> str:
    """The hardware address, read from the kernel's neighbour table.

    Wanted for the same reason `MainboardID` is: DHCP moves the address and the
    MAC does not, so it is what identifies a machine across networks. An
    ElegooLink printer will not tell us its mainboard id without a token, and
    this is the identifier available without one.
    """
    try:
        with open("/proc/net/arp", encoding="utf-8") as fh:
            next(fh, None)
            for line in fh:
                cols = line.split()
                if len(cols) >= 4 and cols[0] == ip and cols[3] != "00:00:00:00:00:00":
                    return cols[3].lower()
    except OSError:
        pass
    return ""


#: One row per printer family, and adding a family is a row rather than a
#: rewrite. Each entry is what that family answers on a LAN with no credential
#: supplied — finding a machine and being allowed to drive it are different
#: questions, and this only asks the first.
#:
#: `match` is given (status, server_header, body_prefix) and says whether this
#: is that family. A 401 counts: it means the endpoint EXISTS and wants a
#: token, which is a printer to tell the user about, not an absence.
#:
#: VERIFIED against real hardware: elegoolink only — a Centauri Carbon 2 on
#: 192.168.8.105, 2026-09-10. The others are written from each project's
#: documented HTTP surface and have NOT been confirmed against a machine here.
#: They are marked so nobody reads this table as measurement.
_PROBES: Tuple[Dict[str, Any], ...] = (
    {
        "family": "elegoolink", "brand": "Elegoo", "port": 80,
        "path": "/system/info", "verified": True,
        "match": lambda st, srv, body: st in (200, 401) and "libhv" in srv,
    },
    {
        "family": "moonraker", "brand": "Klipper", "port": 7125,
        "path": "/printer/info", "verified": False,
        "match": lambda st, srv, body: st in (200, 401) and (
            "klipper" in body.lower() or "result" in body.lower()),
    },
    {
        "family": "octoprint", "brand": "OctoPrint", "port": 5000,
        "path": "/api/version", "verified": False,
        "match": lambda st, srv, body: st in (200, 403) and (
            "octoprint" in body.lower() or "octoprint" in srv),
    },
    {
        "family": "octoprint", "brand": "OctoPrint", "port": 80,
        "path": "/api/version", "verified": False,
        "match": lambda st, srv, body: st in (200, 403) and "octoprint" in body.lower(),
    },
    {
        "family": "prusalink", "brand": "Prusa", "port": 80,
        "path": "/api/version", "verified": False,
        "match": lambda st, srv, body: st in (200, 401) and (
            "prusa" in body.lower() or "prusa" in srv),
    },
)

#: Families found by an open port alone, because their control channel is
#: encrypted or binary and says nothing useful to an unauthenticated probe.
_PORT_ONLY: Tuple[Dict[str, Any], ...] = (
    {"family": "bambu", "brand": "Bambu Lab", "port": 8883, "verified": False},
    {"family": "gcode-tcp", "brand": "unknown", "port": 9100, "verified": False},
)


#: How each family is actually driven, once found. The discovery probe knows
#: the family; without this the fleet entry fell back to the CC1's SDCP port
#: (3030) for everything, so a discovered Centauri Carbon 2 — which is
#: ElegooLink/MQTT — was recorded as if it spoke a protocol it does not.
FAMILY_TRANSPORT: Dict[str, Tuple[str, int]] = {
    "elegoolink": ("elegoolink", 80),
    "sdcp":       ("sdcp", 3030),
    "moonraker":  ("moonraker", 7125),
    "octoprint":  ("octoprint", 80),
    "prusalink":  ("prusalink", 80),
    "bambu":      ("bambu", 8883),
}


def transport_for(device: "Device") -> Tuple[str, int]:
    """The (protocol, port) to store for a discovered printer."""
    return FAMILY_TRANSPORT.get(device.protocol, ("sdcp", 3030))


def _suggest_name(brand: str, mac: str, ip: str) -> str:
    """A name for a machine that has not told us its own.

    A probed printer gives up no model and no user-set name without a
    credential, and `Device.label` falls back to the mainboard id — so the
    eagle would announce "elegoolink:aa:bb:cc:dd:ee:ff". Brand plus the tail of
    the MAC is short, stable across DHCP, and distinguishes two of the same
    brand without inventing a model number nobody told us.
    """
    tail = mac.replace(":", "")[-6:].upper() if mac else ip.split(".")[-1]
    brand_part = (brand or "Printer").split()[0]
    return f"{brand_part} {tail}"


def _default_credential_opens(ip: str, path: str, timeout: float) -> bool:
    """True if the ElegooLink factory default opens this printer's local API."""
    return _system_info(ip, path, timeout) is not None


def _system_info(ip: str, path: str, timeout: float) -> Optional[Dict[str, Any]]:
    """The printer's own `system_info`, if the factory default opens its API.

    The same request answers who the printer is. Measured on a Centauri Carbon
    2, 2026-09-25: `machine_model: "Centauri Carbon 2"`, `sn`, and
    `software_version.ota_version` -- so a probed printer is named after what
    it is rather than after the tail of its MAC.

    A plain HTTP GET with the default token in the query string — the same
    request the printer answers 200 to for a slicer that has done nothing
    special. The token is imported from the driver rather than repeated here,
    so there is one place that knows it. Any error is treated as "not open",
    so a slow or odd printer falls back to asking for a code rather than being
    wrongly declared ready.
    """
    try:
        import http.client
        from urllib.parse import urlencode
        from aethelark3d.drivers.elegoo import DEFAULT_CC2_ACCESS_CODE
        conn = http.client.HTTPConnection(ip, 80, timeout=timeout)
        try:
            conn.request("GET", f"{path}?{urlencode({'X-Token': DEFAULT_CC2_ACCESS_CODE})}")
            resp = conn.getresponse()
            body = resp.read(65536)
            if resp.status != 200:
                return None
        finally:
            conn.close()
        try:
            blob = json.loads(body.decode("utf-8", "replace"))
        except ValueError:
            return {}
        info = blob.get("system_info") if isinstance(blob, dict) else None
        return info if isinstance(info, dict) else {}
    except Exception:
        return None


def _http_probe(ip: str, port: int, path: str, timeout: float):
    """(status, server, body_prefix), or None if nothing answered."""
    import http.client
    try:
        conn = http.client.HTTPConnection(ip, port, timeout=timeout)
        try:
            conn.request("GET", path)
            resp = conn.getresponse()
            return (resp.status,
                    (resp.getheader("Server") or "").lower(),
                    resp.read(512).decode("utf-8", "replace"))
        finally:
            conn.close()
    except Exception:
        return None


def _port_open(ip: str, port: int, timeout: float) -> bool:
    import socket as _s
    try:
        with _s.create_connection((ip, port), timeout=timeout):
            return True
    except Exception:
        return False


def _open_ports(ip: str, ports: Iterable[int], timeout: float) -> set:
    """Which of these ports answer, asked all at once.

    The expensive case is an address with nothing on it: every probe pays the
    full timeout before giving up. Running the ports concurrently makes a dead
    host cost one timeout instead of one per family, which is the difference
    between a 17-second sweep and a 2-second one on a /24.
    """
    from concurrent.futures import ThreadPoolExecutor
    ports = list(ports)
    with ThreadPoolExecutor(max_workers=len(ports)) as pool:
        results = pool.map(lambda pt: (pt, _port_open(ip, pt, timeout)), ports)
        return {pt for pt, ok in results if ok}


def probe_host(ip: str, timeout: float = 0.6) -> Optional[Device]:
    """One host, asked only whether it is a printer and of which family.

    Sends no credential, uploads nothing and starts nothing.
    """
    candidate_ports = {p["port"] for p in _PROBES} | {p["port"] for p in _PORT_ONLY}
    live = _open_ports(ip, candidate_ports, timeout)
    if not live:
        return None

    for probe in _PROBES:
        if probe["port"] not in live:
            continue
        answered = _http_probe(ip, probe["port"], probe["path"], timeout)
        if not answered:
            continue
        status, server, body = answered
        try:
            hit = probe["match"](status, server, body)
        except Exception:
            hit = False
        if not hit:
            continue
        mac = _mac_for(ip)
        # A 401/403 means the local API wants a token. For the ElegooLink
        # family that does NOT mean a person has to type one: the factory
        # default is in force out of the box, and if it opens the API the
        # printer is ready with zero setup. Only a printer that refuses the
        # default — someone set a custom code — actually needs a human.
        needs_code = status in (401, 403)
        info: Dict[str, Any] = {}
        if needs_code and probe["family"] == "elegoolink":
            opened = _system_info(ip, probe["path"], max(timeout, 1.5))
            if opened is not None:
                needs_code = False
                info = opened
        machine = str(info.get("machine_model") or "").strip()
        firmware = str((info.get("software_version") or {}).get("ota_version") or "").strip()
        return Device(
            mainboard_id=f"{probe['family']}:{mac or ip}",
            ip=ip, brand=probe["brand"], protocol=probe["family"],
            machine=machine, firmware=firmware,
            # Something a person can be told. `label` falls back to the
            # mainboard id, and "I found elegoolink:aa:bb:cc:dd:ee:ff" is not
            # a sentence anyone wants read out loud. The MAC tail
            # distinguishes two of the same brand without naming a model we
            # were never told.
            name=machine or _suggest_name(probe["brand"], mac, ip),
            raw={"probe": "http", "port": probe["port"], "status": status,
                 "serial": str(info.get("sn") or ""),
                 "server": server, "needs_access_code": needs_code,
                 "mac": mac, "family_verified_on_hardware": probe["verified"]},
        )

    for probe in _PORT_ONLY:
        if probe["port"] in live:
            mac = _mac_for(ip)
            return Device(
                mainboard_id=f"{probe['family']}:{mac or ip}",
                ip=ip, brand=probe["brand"], protocol=probe["family"],
                name=_suggest_name(probe["brand"], mac, ip),
                raw={"probe": "port", "port": probe["port"],
                     "needs_access_code": True, "mac": mac,
                     "family_verified_on_hardware": probe["verified"]},
            )
    return None


#: Kept as the name the ElegooLink work introduced; it is now one row of the
#: table above.
def probe_elegoolink(ip: str, timeout: float = 0.6) -> Optional[Device]:
    device = probe_host(ip, timeout)
    return device if device and device.protocol == "elegoolink" else None


def local_subnets() -> List[str]:
    """Every IPv4 /24 this machine is actually on, from the routing table."""
    import ipaddress
    import subprocess

    nets: List[str] = []
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr", "show", "scope", "global"],
                             capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return nets
    for line in out.splitlines():
        for token in line.split():
            if "/" not in token:
                continue
            try:
                iface = ipaddress.ip_interface(token)
            except ValueError:
                continue
            if iface.network.prefixlen >= 22 and not iface.ip.is_loopback:
                cidr = str(iface.network)
                if cidr not in nets:
                    nets.append(cidr)
    return nets


def sweep(cidrs: Optional[Iterable[str]] = None, timeout: float = 0.6,
          workers: int = 128) -> List[Device]:
    """Every ElegooLink printer on the local segments.

    A broadcast asks the segment a question and hopes; this asks each address
    directly, which is what finds a machine that does not implement the
    broadcast. 254 hosts at 64 at a time is about two seconds.
    """
    import ipaddress
    from concurrent.futures import ThreadPoolExecutor

    targets: List[str] = []
    for cidr in (list(cidrs) if cidrs else local_subnets()):
        try:
            net = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            continue
        if net.num_addresses > 1024:
            continue
        targets.extend(str(h) for h in net.hosts())

    found: Dict[str, Device] = {}
    if not targets:
        return []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for device in pool.map(lambda h: probe_host(h, timeout), targets):
            if device:
                found.setdefault(device.mainboard_id, device)
    return sorted(found.values(), key=lambda d: (d.label.lower(), d.ip))


def reconcile(devices: Iterable[Device], fleet: Dict[str, Dict[str, Any]]
              ) -> Tuple[Dict[str, str], Dict[str, str], List[Device]]:
    """Work out what the fleet should say, given what actually answered.

    Returns three things and keeps them apart on purpose, because they have
    different consequences: addresses that moved, identities learned for the
    first time, and boards nobody has a slot for.

    Matching is by mainboard id where one is on record, because that is the
    only identifier that survives the address changing. Otherwise by the
    printer's own name against the slot's key or name — how a first-time match
    has to work, since no id has been recorded yet.
    """
    moved: Dict[str, str] = {}
    learned: Dict[str, str] = {}
    unknown: List[Device] = []

    by_id = {str(cfg.get("mainboard_id") or "").lower(): key
             for key, cfg in fleet.items() if cfg.get("mainboard_id")}

    def by_name(device: Device) -> Optional[str]:
        wanted = {device.name.lower(), device.machine.lower()} - {""}
        for key, cfg in fleet.items():
            if str(cfg.get("mainboard_id") or ""):
                continue        # already identified; do not steal it by name
            names = {key.lower(), str(cfg.get("name") or "").lower()} - {""}
            if names & wanted:
                return key
        return None

    # A MAC is burned in like a mainboard id, and an entry written before ids
    # were recorded may carry only that. Matched before names: a printer that
    # starts reporting its model is renamed ("Elegoo 7526B5" -> "Centauri
    # Carbon 2"), and a name match then missed it and added it a second time.
    by_mac = {str(cfg.get("mac") or "").lower(): key
              for key, cfg in fleet.items() if cfg.get("mac")}

    def by_hardware(device: Device) -> Optional[str]:
        mac = str(device.raw.get("mac") or "").lower()
        return by_mac.get(mac) if mac else None

    for device in devices:
        key = (by_id.get(device.mainboard_id.lower()) or by_hardware(device)
               or by_name(device))
        if key is None:
            unknown.append(device)
            continue
        if not fleet[key].get("mainboard_id"):
            learned[key] = device.mainboard_id
        if str(fleet[key].get("host") or "").strip() != device.ip:
            moved[key] = device.ip

    return moved, learned, unknown


def _slot_name(device: Device, taken: Iterable[str]) -> str:
    """A config key for a board nobody has a slot for.

    Derived from what the printer calls itself, uppercased and stripped to the
    characters a key is allowed — a user typing `--printer` should be able to
    say it, and the model has to be able to repeat it back.
    """
    base = "".join(c if c.isalnum() else "_"
                   for c in (device.name or device.machine or "PRINTER")).upper()
    base = "_".join(part for part in base.split("_") if part) or "PRINTER"
    taken = set(taken)
    if base not in taken:
        return base
    n = 2
    while f"{base}_{n}" in taken:
        n += 1
    return f"{base}_{n}"


def adopt(devices: Iterable[Device], cfg=None, add_unknown: bool = True
          ) -> Dict[str, Any]:
    """Make the stored fleet agree with the printers that answered.

    Writes three kinds of change and reports each separately, because they are
    not equally interesting to a person: an address that moved is routine, an
    identity learned means this printer can now be followed across networks,
    and a new printer is something they may not have known was there.

    Only ever adds or corrects. A configured printer that did not answer is
    left exactly as it was — it is far more likely to be switched off than to
    have ceased to exist, and deleting a user's fleet because a printer was
    asleep would be its own kind of bug.
    """
    from .config import config as _default_config
    cfg = cfg or _default_config

    devices = list(devices)
    # NOT `cfg.get_fleet() or {}`: an empty fleet is falsy, so that expression
    # hands back a fresh throwaway dict and every printer discovered on a
    # first run is written into an object nobody keeps.
    fleet = cfg.get_fleet()
    if fleet is None:
        fleet = {}
    moved, learned, unknown = reconcile(devices, fleet)

    for key, ip in moved.items():
        cfg.set_printer_ip(key, ip)
    for key, board in learned.items():
        slot = cfg.get_printer(key)
        if slot is not None:
            slot["mainboard_id"] = board

    # Who each known printer is, asked again every time: a CC1 can have a
    # CANVAS fitted later, and a printer adopted before identity was recorded
    # was stored under the tail of its MAC with no model at all.
    reidentified: Dict[str, str] = {}
    for key, device in _pair(devices, fleet):
        slot = fleet.get(key)
        if slot is None:
            continue
        changes = {k: v for k, v in _identity(device, slot).items()
                   if slot.get(k) != v}
        if changes:
            slot.update(changes)
            reidentified[key] = display_name(slot, key)

    added: Dict[str, str] = {}
    if add_unknown:
        for device in unknown:
            key = _slot_name(device, set(fleet) | set(added))
            protocol, port = transport_for(device)
            fleet[key] = {
                "name": device.label,
                "brand": device.brand or "Elegoo",
                "model": device.machine,
                "host": device.ip,
                "port": port,
                "protocol": protocol,
                "mainboard_id": device.mainboard_id,
                # Carried from the probe so the eagle can ASK for the code
                # instead of silently failing to drive the printer. This is
                # the whole "mention it only if you hit the access code" flow:
                # a printer answering 401 is found AND locked, and both facts
                # have to reach the fleet or the second one is lost.
                "needs_access_code": bool(device.raw.get("needs_access_code")),
                "mac": device.raw.get("mac", ""),
            }
            fleet[key].update(_identity(device, fleet[key]))
            added[key] = device.ip

    if moved or learned or added or reidentified:
        cfg.save()

    answered = {key for key, _ in _pair(devices, fleet)} | set(added)

    return {
        "found": len(devices),
        "moved": moved,
        "identified": learned,
        "added": added,
        # Which configured printers stayed quiet, and where each was last seen.
        # `found: 1` is true and useless to someone who owns three: it cannot be
        # told apart from owning one. Nothing here is probed for -- these slots
        # are already in the fleet, and saying so costs nothing. The slots are
        # only read; a printer that did not answer is far likelier to be off
        # than gone, so nothing is rewritten or removed.
        "silent": {
            key: str(slot.get("host") or "")
            for key, slot in fleet.items()
            if key not in answered and slot.get("host")
        },
        "reidentified": reidentified,
        "printers": [
            {"key": key, "ip": device.ip,
             "model": display_name(fleet.get(key) or {}, key),
             "canvas": bool((fleet.get(key) or {}).get("canvas")),
             "mainboard_id": device.mainboard_id}
            for key, device in _pair(devices, fleet)
        ],
    }


def canvas_slots(device: Device, access_code: str = "",
                 timeout: float = 8.0) -> Optional[int]:
    """Filament slots on attached CANVAS units: 0 for none, None if unknown.

    Asked only of an ElegooLink printer (method 2005, read-only). Measured on
    a Centauri Carbon 2 with no CANVAS, 2026-09-25: one canvas entry,
    `connected: 0`, four empty trays. A CC1 is never asked: its SDCP command
    for this (324) is unprobed, and pycentauri refuses it because unknown
    commands can crash that firmware. A CC1's CANVAS is recorded when the
    user says it has one (`a3d fleet --canvas`).
    """
    if device.protocol != "elegoolink":
        return None
    if device.raw.get("needs_access_code") and not access_code:
        return None
    import asyncio

    async def ask():
        from pycentauri import CC2Printer
        from aethelark3d.drivers.elegoo import DEFAULT_CC2_ACCESS_CODE
        p = await CC2Printer.connect(device.ip,
                                     access_code=access_code or DEFAULT_CC2_ACCESS_CODE,
                                     enable_control=False)
        try:
            return await p._cc2_request(2005, {})
        finally:
            await p.close()

    try:
        result = asyncio.run(asyncio.wait_for(ask(), timeout))
    except Exception:
        return None
    units = ((result or {}).get("canvas_info") or {}).get("canvas_list") or []
    return sum(len(u.get("tray_list") or []) for u in units if u.get("connected"))


def _identity(device: Device, slot: Dict[str, Any]) -> Dict[str, Any]:
    """The identity fields this answer lets us write, and only those.

    A field the printer did not report is left out rather than blanked: a
    printer that answered its model once and not the next time has not
    stopped being that model.
    """
    out: Dict[str, Any] = {}
    if device.machine:
        out["model"] = device.machine
    if device.firmware:
        out["firmware"] = device.firmware
    slots = canvas_slots(device, str(slot.get("access_code") or ""))
    if slots is not None:
        out["canvas"] = slots > 0
        out["canvas_slots"] = slots
    return out


def _pair(devices: Iterable[Device], fleet: Dict[str, Dict[str, Any]]):
    """Each device beside the config key it now belongs to."""
    by_board = {str(cfg.get("mainboard_id") or "").lower(): key
                for key, cfg in fleet.items() if cfg.get("mainboard_id")}
    for device in devices:
        yield by_board.get(device.mainboard_id.lower(), "?"), device


def find_and_adopt(timeout: float = DEFAULT_TIMEOUT,
                   targets: Optional[Iterable[str]] = None,
                   cfg=None) -> Dict[str, Any]:
    """Discover, then make the fleet agree with what was found."""
    return adopt(discover(timeout=timeout, targets=targets), cfg=cfg)
