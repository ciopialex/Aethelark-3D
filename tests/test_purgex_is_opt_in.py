"""PurgeX is optional, mentioned, and never applied to a plain print.

The operator's rule, verbatim: "purgex is optional and must be mentioned, it is
a separate tool not included in the basic pipeline without mentioning it."

Before this, `find_and_prepare_print(enable_purgex=True)` and the CLI's
`--purgex/--default-purge` both defaulted ON, so every print — including one the
eagle triggered by voice — silently applied PurgeX, and the eagle had no way to
turn it off or even know it was on. That is the opposite of optional.

PurgeX only changes a multi-colour print (it optimises the filament purged on a
colour change); on a single-colour test cube it does nothing. So the basic
pipeline uses standard purge, and PurgeX is opt-in.
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aethelark3d import api, cli


def _default(func, param):
    return inspect.signature(func).parameters[param].default


def test_the_pipeline_defaults_to_standard_purge():
    assert _default(api.find_and_prepare_print, "enable_purgex") is False, (
        "find_and_prepare_print still defaults enable_purgex=True, so a plain "
        "print silently applies PurgeX")


def test_the_cli_defaults_to_standard_purge():
    # The --purgex/--default-purge flag pair. Its typer default carries the value.
    default = _default(cli.cli_print, "purgex")
    val = getattr(default, "default", default)   # unwrap typer.OptionInfo
    assert val is False, "a3d print still defaults to PurgeX instead of standard purge"


def test_there_is_still_a_way_to_opt_in():
    """Opt-in must exist, or the feature is unreachable rather than optional."""
    params = inspect.signature(cli.cli_print).parameters
    assert "want_purgex" in params or "purgex" in params, (
        "no way to enable PurgeX at all — it must be optional, not absent")
