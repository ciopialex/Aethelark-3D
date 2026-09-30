"""
Pytest configuration and global test isolation fixtures for Aethelark-3D.
Ensures zero test cross-contamination across digital twin hardware simulations.
"""

import copy

import pytest

# `aethelark3d/__init__` re-exports the `config` singleton as the package
# attribute `aethelark3d.config`, shadowing the submodule — so neither
# `from aethelark3d import config` nor `import aethelark3d.config` reaches the
# module. Import the class and the singleton straight from the submodule, which
# the from-import machinery resolves via sys.modules, not the shadowed attr.
from aethelark3d import config as shared_config
from aethelark3d.config import Config
from aethelark3d.drivers.factory import reset_simulators


@pytest.fixture(autouse=True)
def isolate_config(tmp_path_factory):
    """Give every test the pristine default fleet, on disk nowhere real.

    `config.config` is a module-level singleton that loads
    `~/.config/aethelark3d/config.json` at import — the developer's OWN fleet.
    Two things followed from that, and both are launch/CI defects, not test bugs:

      1. The suite READ real state. Tests assert on printers named CC1/CC2/C2 —
         the default seed in Config.__init__. On a machine where the operator has
         registered a real printer (ELEGOO_7526B5) and no CC1, twenty tests
         failed for a fleet the test author never chose. A fresh clone on a
         configured machine cannot go green.
      2. The suite WROTE real state. `fleet --set-ip`, `spool -p CC1`,
         `set_alias` all call `config.save()`, which serialised straight into the
         operator's live config file. Running the tests mutated the real fleet.

    The fix is isolation, not new assertions: repoint the one shared singleton at
    a throwaway directory with no file (so `load()` keeps the default demo fleet)
    and restore it afterward. Every test now starts from the same known fleet and
    can save all it likes without touching anything real.
    """
    d = tmp_path_factory.mktemp("a3dcfg")
    shared = shared_config
    pristine = Config(config_dir=d)  # empty dir -> default seed, no load
    # The product starts with no printers; the suite is written against the
    # demo line-up, so it asks for it by name rather than inheriting it.
    from aethelark3d.config import DEMO_FLEET
    pristine._data["printers"] = copy.deepcopy(DEMO_FLEET)
    pristine._data["default_printer"] = "CC1"

    saved = (shared.config_dir, shared.config_file, shared._data)
    shared.config_dir = pristine.config_dir
    shared.config_file = pristine.config_file
    shared._data = copy.deepcopy(pristine._data)
    try:
        yield
    finally:
        shared.config_dir, shared.config_file, shared._data = saved


@pytest.fixture(autouse=True)
def no_online_filaments(tmp_path_factory, monkeypatch):
    """Filament profiles come from this machine only: no network, no cache."""
    from aethelark3d.filaments import online
    d = tmp_path_factory.mktemp("a3dfil")
    monkeypatch.setenv("AETHELARK3D_OFFLINE", "1")
    monkeypatch.setattr(online, "cache_dir", lambda: d)


@pytest.fixture(autouse=True)
def clean_digital_twin_state():
    """Reset all digital twin hardware simulator singletons before and after every test."""
    reset_simulators()
    yield
    reset_simulators()
