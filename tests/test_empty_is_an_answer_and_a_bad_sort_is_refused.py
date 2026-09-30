"""SHIP_1.0 items B6 and B9.

**B6 — "found nothing" was reported as a failure.** A local file search matching
nothing printed the empty result and then exited 1. The module bus reads a
non-zero exit as the tool having failed, so "there is no keychain file on this
machine" -- a true and useful answer -- reached the eagle as a broken tool.
Empty is a success with an empty list.

**B9 — an invalid ordering was silently dropped.** `--sort=bogus` returned the
default ordering, exit 0, no complaint. Someone who asked for the least material
was shown the most downloaded and told nothing. Measured 2026-09-05: a search
with `--sort=bogus` returned results and exited 0.

Both messages are read by Gemini 2.5 Flash, so the refusal names the four valid
orderings rather than saying the value was invalid -- a model told only that it
was wrong has to guess what right looks like.
"""
from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from aethelark3d.cli import app


@pytest.fixture()
def runner():
    return CliRunner()


# --------------------------------------------------------------------------
# B6 — empty is a success
# --------------------------------------------------------------------------

def test_a_local_search_that_matches_nothing_succeeds(runner, tmp_path):
    result = runner.invoke(app, ["local", "zzzznosuchfilename",
                                 "--dir", str(tmp_path), "--json"])

    assert result.exit_code == 0, (
        "a search that matched nothing exited non-zero, so the module bus reads "
        "a true answer -- 'there is no such file here' -- as a broken tool")


def test_the_empty_answer_is_still_a_readable_payload(runner, tmp_path):
    result = runner.invoke(app, ["local", "zzzznosuchfilename",
                                 "--dir", str(tmp_path), "--json"])
    payload = json.loads(result.stdout)
    assert payload["candidates"] == []
    assert payload["query"] == "zzzznosuchfilename"


# --------------------------------------------------------------------------
# B9 — an ordering nobody offers is refused, and the offer is named
# --------------------------------------------------------------------------

VALID_ORDERINGS = ("downloads", "likes", "least_material", "fastest")


def test_an_ordering_nobody_offers_is_refused(runner):
    result = runner.invoke(app, ["search", "phone stand",
                                 "--sort", "bogus", "--json"])

    assert result.exit_code != 0, (
        "an invalid ordering returned results and exit 0, so someone who asked "
        "for the least material was shown the most downloaded and told nothing")


def test_the_refusal_names_the_orderings_that_do_work(runner):
    """A weak model told only 'invalid' has to guess what valid looks like."""
    result = runner.invoke(app, ["search", "phone stand",
                                 "--sort", "bogus", "--json"])
    said = result.stdout.lower()
    unnamed = [o for o in VALID_ORDERINGS if o not in said]
    assert not unnamed, (
        f"the refusal does not say which orderings do work, so a model cannot "
        f"correct itself: missing {unnamed} from {result.stdout[:200]!r}")


def test_the_refusal_names_the_value_that_was_wrong(runner):
    result = runner.invoke(app, ["search", "phone stand",
                                 "--sort", "bogus", "--json"])
    assert "bogus" in result.stdout.lower()


@pytest.mark.parametrize("ordering", VALID_ORDERINGS)
def test_every_offered_ordering_is_accepted(runner, ordering):
    """The guard must not refuse an ordering the help text advertises.

    Network-dependent, so this asserts only that the ordering was not rejected
    as invalid -- not that results came back.
    """
    result = runner.invoke(app, ["search", "benchy", "--limit", "1",
                                 "--sort", ordering, "--json"])
    assert "not an ordering" not in result.stdout.lower(), (
        f"{ordering!r} is advertised in --help but refused as invalid")
