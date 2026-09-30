"""Which machine a job is going to decides how to talk to it and how it purges.

Three things were carried per-printer in config that are really properties of
the model:

*The wire.* A Centauri Carbon speaks SDCP on 3030; a Centauri Carbon 2 and a
Centauri 2 speak ElegooLink on 80. Storing the port per entry means every new
printer is a chance to store the wrong one, and a machine that answers on the
wrong port looks offline rather than misconfigured.

*The address.* The shipped default carried a real IP from the author's phone
hotspot. On a buyer's network 172.20.10.3 is either nothing or someone else's
device, and the module reports a printer that was never theirs.

*The purge.* Waste optimisation only means something against the mechanism the
machine actually has. An Elegoo builds a prime tower, a Bambu ejects to a
chute, a Creality wipes. Optimising a tower on a printer that has no tower is
arithmetic about nothing.
"""
from __future__ import annotations

import pytest

from pathlib import Path

from aethelark3d import config as config_mod


# ── the wire ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("model,protocol,port", [
    ("Elegoo Centauri Carbon", "sdcp", 3030),
    ("Elegoo Centauri Carbon 2", "elegoolink", 80),
    ("Elegoo Centauri 2", "elegoolink", 80),
])
def test_the_protocol_follows_from_the_model(model, protocol, port):
    from aethelark3d.drivers.factory import resolve_transport

    assert resolve_transport({"model": model}) == (protocol, port)


def test_an_explicit_port_still_wins():
    """A machine moved behind a proxy is the operator's business, not ours."""
    from aethelark3d.drivers.factory import resolve_transport

    assert resolve_transport(
        {"model": "Elegoo Centauri Carbon", "port": 8080})[1] == 8080


def test_an_unknown_model_gets_the_conservative_default():
    from aethelark3d.drivers.factory import resolve_transport

    protocol, port = resolve_transport({"model": "Some Printer Nobody Owns"})
    assert (protocol, port) == ("sdcp", 3030)


# ── the address ─────────────────────────────────────────────────────────────

def test_the_shipped_fleet_carries_nobody_elses_address():
    """A default host is a claim about the buyer's network that cannot be true.

    Read out of the source rather than from a constructed Config, because
    Config merges whatever is already in ~/.config — which on this machine has
    real addresses in it, and would hide exactly the thing being checked.
    """
    import ast
    import aethelark3d

    source = (Path(aethelark3d.__file__).parent / "config.py").read_text()
    tree = ast.parse(source)
    hosts = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if (isinstance(key, ast.Constant) and key.value == "host"
                    and isinstance(value, ast.Constant)):
                hosts.append(value.value)

    assert hosts, "no default host entries found — has the fleet moved?"
    assert all(h == "" for h in hosts), (
        f"the shipped fleet bakes in addresses: {[h for h in hosts if h]}")


def test_a_printer_with_no_address_is_reported_as_unconfigured():
    from aethelark3d.drivers.factory import resolve_transport, is_addressable

    assert is_addressable({"host": "192.168.1.55"}) is True
    assert is_addressable({"host": ""}) is False
    assert is_addressable({}) is False


# ── the purge ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("brand,model,mechanism", [
    ("Elegoo", "Elegoo Centauri Carbon", "prime_tower"),
    ("Elegoo", "Elegoo Centauri Carbon 2", "prime_tower"),
    ("Bambu Lab", "Bambu Lab X1C", "poop_chute"),
    ("Bambu Lab", "Bambu Lab P1S", "poop_chute"),
    ("Creality", "Creality K1", "wipe_tower"),
])
def test_each_machine_purges_the_way_it_actually_purges(brand, model, mechanism):
    from aethelark3d.slicers.purge import resolve_purge_mechanism

    assert resolve_purge_mechanism({"brand": brand, "model": model}) == mechanism


def test_an_unknown_machine_falls_back_to_the_universal_mechanism():
    """Every FDM printer can build a prime tower; not every one has a chute."""
    from aethelark3d.slicers.purge import resolve_purge_mechanism

    assert resolve_purge_mechanism({"brand": "Nobody", "model": "X"}) == "prime_tower"


def test_a_chute_machine_does_not_get_tower_geometry_advice():
    """The tower optimiser reasons about wall thickness and toppling under
    CoreXY acceleration. On a printer that ejects to a chute there is no tower
    for any of that to be about."""
    from aethelark3d.slicers.purge import purge_plan

    chute = purge_plan({"brand": "Bambu Lab", "model": "Bambu Lab X1C"},
                       required_purge_volume_mm3=900.0)
    tower = purge_plan({"brand": "Elegoo", "model": "Elegoo Centauri Carbon"},
                       required_purge_volume_mm3=900.0)

    assert chute["mechanism"] == "poop_chute"
    assert tower["mechanism"] == "prime_tower"
    assert "tower_geometry" in tower
    assert "tower_geometry" not in chute


def test_the_plan_never_claims_to_save_more_than_was_there():
    """Waste saved is bounded by waste produced. A percentage over 100 means
    the model is describing something other than this print."""
    from aethelark3d.slicers.purge import purge_plan

    for required in (1.0, 250.0, 5000.0):
        for machine in ({"brand": "Elegoo", "model": "Elegoo Centauri Carbon"},
                        {"brand": "Bambu Lab", "model": "Bambu Lab X1C"},
                        {"brand": "Creality", "model": "Creality K1"}):
            plan = purge_plan(machine, required_purge_volume_mm3=required)
            assert 0.0 <= plan["saved_mm3"] <= required + 1e-6
            assert 0.0 <= plan["reduction_percent"] <= 100.0
