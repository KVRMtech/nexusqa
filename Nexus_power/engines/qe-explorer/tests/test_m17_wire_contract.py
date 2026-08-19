"""M1.7 / T-GW-04 — THE EXPLORER HALF of the cross-service rule contract.

THE HOLE THIS CLOSES.  ``test_greenwash_holes.py`` proves the explorer discovers
a rule, dedupes it, emits it, and reuses one it is handed.  ``qe-central``'s
``test_greenwash_recovery.py`` proves its store validates, bounds and persists a
rule and hands it back on dispatch.  Both suites are green with the loop CUT:
rename ``field_label`` on either side and the producer emits a key the consumer
drops, so every rule is silently re-derived by the experiment it was meant to
replace.  Nothing fails, because re-running the experiment still yields a correct
crawl — the reuse is an optimisation whose absence is invisible.

The seam cannot be tested by importing both sides: qe-explorer and qe-central
each ship a top-level ``app`` package and collide in one interpreter.  So the
shape is frozen as DATA in ``contracts/m17_business_rule_v1.json`` and each side
asserts against it in its own process.  Together the two files are one proof.

See ``platform/qe-central/tests/contract/test_m17_business_rule_contract.py`` for
the other half.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import rules


def _contract() -> dict:
    """Load the frozen contract by walking up to the ``Nexus_power`` root.

    Walked rather than hard-coded because this suite is collected from the
    SERVICE root in CI and from the repository root by some local runners; a
    relative literal would pass in one and vanish in the other.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "contracts" / "m17_business_rule_v1.json"
        if candidate.is_file():
            return json.loads(candidate.read_text(encoding="utf-8"))
    raise AssertionError(
        "contracts/m17_business_rule_v1.json not found above %s — the frozen "
        "wire contract is the only thing tying this service's rule shape to "
        "qe-central's, and it must not be deleted to make a test pass" % here
    )


CONTRACT = _contract()


def test_the_producer_emits_exactly_the_contract_fields():
    """T-GW-04. ``DiscoveredRule.as_dict`` is what crosses the wire on completion.

    Exact equality, not a subset: a field DROPPED silently stops being persisted,
    and a field ADDED without updating the contract is one qe-central will not
    store — either way the two sides have stopped agreeing.
    """
    emitted = rules.discover(
        url="https://a.test/x/123/step",
        blocked_label="Continue",
        field_label="Health Conditions",
        proof="proven",
    ).as_dict()

    assert set(emitted) == set(CONTRACT["required_fields"]), (
        "app/rules.DiscoveredRule.as_dict() no longer matches the frozen wire "
        "contract — qe-central's rule_store._clean() reads the contract's names"
    )


def test_the_consumer_accepts_a_rule_in_the_contract_shape():
    """T-GW-04. The dispatch hands back exactly ``contracts`` sample shape; the
    explorer must parse it into a usable rule rather than fail closed on it."""
    parsed = rules.DiscoveredRule.from_mapping(CONTRACT["sample"])

    assert parsed is not None, (
        "the explorer refused a rule in the frozen contract shape — qe-central's "
        "fetch_rules() emits exactly this, so every reuse would fall back to the "
        "experiment and the T-GW-04 learning loop would be silently severed"
    )
    for name in CONTRACT["required_fields"]:
        expected = CONTRACT["sample"][name]
        actual = getattr(parsed, "key" if name == "key" else name)
        assert actual == expected, "field %r did not survive the parse" % name


def test_a_round_trip_through_the_contract_is_lossless():
    """T-GW-04. produce -> (wire) -> consume -> produce is a fixed point.

    This is the property the loop actually depends on: what qe-central stores and
    hands back must re-enter the explorer as the same rule, or reuse matches
    nothing and every crawl re-experiments.
    """
    original = rules.discover(
        url="https://a.test/x/123/step",
        blocked_label="Continue",
        field_label="Health Conditions",
        proof="proven",
    )
    round_tripped = rules.DiscoveredRule.from_mapping(original.as_dict())

    assert round_tripped is not None
    assert round_tripped.as_dict() == original.as_dict()


def test_a_reused_rule_actually_matches_what_the_producer_minted():
    """T-GW-04 ACCEPTANCE, explorer side. Identity survives the wire.

    ``rule_key`` is derived from the URL TEMPLATE, so the crawl that REUSES the
    rule looks it up under a different concrete URL than the crawl that PROVED
    it.  If the key were derived from the raw URL the lookup would miss on every
    record id and the store would grow one row per record while reusing nothing.
    """
    proved = rules.discover(
        url="https://a.test/x/123/step",       # proved on record 123
        blocked_label="Continue",
        field_label="Health Conditions",
        proof="proven",
    )
    known = rules.KnownRules([proved.as_dict()])

    hit = known.lookup(url="https://a.test/x/999/step",   # reused on record 999
                       blocked_label="Continue")

    assert hit is not None, (
        "a rule proved on one record did not match the same page on another — "
        "the store would accumulate a row per record and reuse none of them"
    )
    assert hit.field_label == "Health Conditions"


def test_string_bounds_agree_with_the_columns_that_store_them():
    """T-GW-04. The explorer truncates to the same widths qe-central's columns
    declare, so a long label is bounded ONCE and identically on both sides.

    If the explorer emitted longer than the column, the value would be either
    truncated a second time by the store (making the stored rule differ from the
    proved one) or rejected by the database (losing the rule entirely).
    """
    bounds = CONTRACT["column_bounds"]
    long_rule = rules.discover(
        url="https://a.test/" + "p" * 900,
        blocked_label="C" * 400,
        field_label="F" * 400,
        proof="P" * 900,
    ).as_dict()

    for name, limit in bounds.items():
        assert len(str(long_rule[name])) <= limit, (
            "%s exceeds the %d-char column qe-central stores it in" % (name, limit)
        )


@pytest.mark.parametrize("missing", ["key", "blocked_label", "field_label"])
def test_a_fragment_is_refused_on_this_side_too(missing):
    """T-GW-04. Both sides refuse the same fragments.

    qe-central refuses a rule missing any of these because an un-lookup-able row
    defeats the table's only purpose.  The explorer must refuse the identical set
    — a rule one side stores and the other ignores is a loop that reports reuse
    it is not performing.
    """
    assert missing in CONTRACT["must_be_present_or_the_rule_is_a_fragment"]
    fragment = dict(CONTRACT["sample"])
    fragment[missing] = ""

    assert rules.DiscoveredRule.from_mapping(fragment) is None


def test_a_future_schema_version_is_ignored_not_guessed_at():
    """T-GW-04. Fail-closed on version is what makes bumping the contract safe:
    an old reader ignores a new rule (costing one repeated experiment) rather
    than misapplying a shape whose meaning it does not know."""
    future = dict(CONTRACT["sample"])
    future["schema_version"] = int(CONTRACT["schema_version"]) + 1

    assert rules.DiscoveredRule.from_mapping(future) is None
