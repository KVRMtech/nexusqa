"""SCENARIOS ARE DERIVED, NOT INVENTED.

Once the catalogue holds a question's real shape, the interesting cases follow
from it: a required field must reject empty, a declared maximum must be accepted
and one past it rejected, a choice must accept the answers it offers.

The whole value of this module rests on one rule, and most of these tests exist
to defend it:

    A scenario is derived ONLY from a rule the crawl actually OBSERVED.

An invented rule produces a test that asserts something the application never
promised. That is worse than no test: a red is our error and costs a real
investigation, and a green means nothing at all. So a field that declared nothing
yields nothing, and the shortfall is REPORTED rather than padded away.

Two subtler properties also pinned here:

  * a CLIPPED answer set cannot support a scenario claiming to cover the answers
    — the completeness marker earned in the catalogue is what makes that
    detectable, and this is where it pays for itself;
  * a ``maxlength`` input TRUNCATES rather than rejecting, so the "one character
    too many" case is deliberately NOT generated. Generating it would fail
    against a correct application, which is the most expensive kind of wrong.
"""
from __future__ import annotations

from app.services.catalog_scenarios import (
    BASIS_MAX,
    BASIS_MAX_LENGTH,
    BASIS_MIN,
    BASIS_MIN_LENGTH,
    BASIS_OPTIONS,
    BASIS_PATTERN,
    BASIS_REQUIRED,
    KIND_BOUNDARY,
    KIND_NEGATIVE,
    KIND_POSITIVE,
    derive_catalog_scenarios,
    derive_field_scenarios,
)

_STATES = ["Alabama", "Alaska", "Arizona", "Wyoming"]


def _q(name="Field", **over):
    q = {"question_id": f"q::{name}", "name": name, "type": "text",
         "required": False, "options": [], "options_total": 0}
    q.update(over)
    return q


def _kinds(scenarios):
    return sorted({s["kind"] for s in scenarios})


def _bases(scenarios):
    return sorted({s["basis"] for s in scenarios})


# ── THE RULE: nothing observed, nothing derived ─────────────────────────────

def test_a_question_that_declared_nothing_yields_nothing():
    """THE LOAD-BEARING TEST. A plain text input with no required flag and no
    declared constraint supports no assertion. Emitting a nominal case would
    inflate the count with a test that checks nothing."""
    assert derive_field_scenarios(_q("Middle name")) == []


def test_the_shortfall_is_reported_rather_than_padded():
    """A catalogue of mostly-undeclared questions is a FINDING about the
    application — it says the app asserts very little about itself. Hiding that
    behind a large-looking total is the green-wash version of this feature."""
    cat = {"questions": [_q("A"), _q("B"), _q("C"),
                         _q("State", type="select", options=_STATES,
                            options_total=4)]}
    out = derive_catalog_scenarios(cat)
    assert out["summary"]["questions"] == 4
    assert out["summary"]["questions_without_rules"] == 3
    assert out["summary"]["scenarios"] == len(out["scenarios"]) == 1


def test_nothing_is_derived_from_junk():
    for bad in (None, {}, {"questions": None}, {"questions": "nope"},
                {"questions": [None, 3, "x"]}):
        out = derive_catalog_scenarios(bad)
        assert out["scenarios"] == []


# ── required ────────────────────────────────────────────────────────────────

def test_a_required_field_must_reject_empty():
    s = derive_field_scenarios(_q("Last name", required=True))
    assert len(s) == 1
    assert s[0]["kind"] == KIND_NEGATIVE and s[0]["basis"] == BASIS_REQUIRED
    assert s[0]["value"] == {"strategy": "empty"}
    assert s[0]["expect"] == "reject"


def test_an_optional_field_gets_no_required_case():
    assert not [x for x in derive_field_scenarios(_q("Nickname"))
                if x["basis"] == BASIS_REQUIRED]


# ── the answers a choice offers ─────────────────────────────────────────────

def test_a_complete_answer_set_yields_a_case_over_every_answer():
    """The requirement's example: every state, Alabama through Wyoming."""
    s = derive_field_scenarios(
        _q("What state do you live in?", type="select",
           options=_STATES, options_total=4))
    opt = [x for x in s if x["basis"] == BASIS_OPTIONS][0]
    assert opt["kind"] == KIND_POSITIVE
    assert opt["value"]["strategy"] == "each_option"
    assert opt["value"]["options"] == _STATES
    assert "incomplete" not in opt["value"]


def test_a_clipped_answer_set_never_claims_to_cover_the_answers():
    """WHERE THE COMPLETENESS MARKER PAYS FOR ITSELF. The catalogue holds four
    of two hundred and fifty answers. A case built from that prefix is still
    worth running — it just may not say it covers the question."""
    s = derive_field_scenarios(
        _q("Country", type="select", options=_STATES, options_total=250))
    opt = [x for x in s if x["basis"] == BASIS_OPTIONS][0]
    assert opt["value"]["incomplete"] is True
    assert "incomplete" in opt["intent"]
    assert "250" in opt["intent"]


def test_clipped_answer_sets_are_counted_in_the_summary():
    cat = {"questions": [_q("Country", type="select", options=_STATES,
                            options_total=250)]}
    assert derive_catalog_scenarios(cat)["summary"][
        "questions_with_incomplete_options"] == 1


# ── declared numeric bounds ─────────────────────────────────────────────────

def test_a_declared_range_yields_both_edges_and_both_verdicts():
    s = derive_field_scenarios(_q("Coverage amount", type="number",
                                  validation={"min": "10000", "max": "500000"}))
    assert _kinds(s) == [KIND_BOUNDARY]
    accepts = [x for x in s if x["expect"] == "accept"]
    rejects = [x for x in s if x["expect"] == "reject"]
    assert len(accepts) == 2 and len(rejects) == 2
    assert sorted(BASIS_MIN in x["basis"] or BASIS_MAX in x["basis"] for x in s)
    assert any("10000" in x["intent"] for x in s)
    assert any("500000" in x["intent"] for x in s)


def test_a_bound_on_a_non_numeric_field_is_not_read_as_a_number():
    """``min``/``max`` on a text input are not numeric bounds. Treating them as
    such would generate a case asserting a rule the field does not have."""
    s = derive_field_scenarios(_q("Notes", type="text",
                                  validation={"min": "1", "max": "9"}))
    assert not [x for x in s if x["basis"] in (BASIS_MIN, BASIS_MAX)]


def test_an_unparseable_bound_is_ignored_rather_than_guessed():
    for bad in ("", "  ", "abc", None, "NaN", "Infinity", True):
        s = derive_field_scenarios(_q("Amount", type="number",
                                      validation={"min": bad}))
        assert not [x for x in s if x["basis"] == BASIS_MIN], bad


# ── declared lengths ────────────────────────────────────────────────────────

def test_a_minimum_length_yields_the_edge_and_one_below():
    s = derive_field_scenarios(_q("Password", validation={"minlength": "8"}))
    edge = [x for x in s if x["expect"] == "accept"][0]
    under = [x for x in s if x["expect"] == "reject"][0]
    assert edge["value"] == {"strategy": "length", "chars": 8}
    assert under["value"] == {"strategy": "length", "chars": 7}
    assert _bases(s) == [BASIS_MIN_LENGTH]


def test_a_maximum_length_yields_the_edge_but_NOT_one_over():
    """DELIBERATE OMISSION. A maxlength input truncates rather than rejecting, so
    the over-long value never reaches the application. Asserting rejection would
    fail against a CORRECT app — the most expensive kind of wrong test."""
    s = derive_field_scenarios(_q("Nickname", validation={"maxlength": "50"}))
    assert [x["expect"] for x in s] == ["accept"]
    assert s[0]["value"] == {"strategy": "length", "chars": 50}


def test_a_zero_or_negative_length_is_ignored():
    for bad in ("0", "-1", "1.5", "abc", None):
        s = derive_field_scenarios(_q("X", validation={"minlength": bad}))
        assert not [x for x in s if x["basis"] == BASIS_MIN_LENGTH], bad


# ── declared format ─────────────────────────────────────────────────────────

def test_a_declared_pattern_yields_a_format_rejection():
    s = derive_field_scenarios(
        _q("Email", validation={"pattern": r"^[^@]+@[^@]+$"}))
    assert len(s) == 1 and s[0]["kind"] == KIND_NEGATIVE
    assert s[0]["basis"] == BASIS_PATTERN
    assert s[0]["value"]["pattern"] == r"^[^@]+@[^@]+$"


def test_a_blank_pattern_is_not_a_rule():
    assert derive_field_scenarios(_q("X", validation={"pattern": "   "})) == []


# ── what a scenario asserts ─────────────────────────────────────────────────

def test_a_scenario_never_asserts_a_message_the_crawl_did_not_observe():
    """``expect`` is accept/reject only. The crawl never read the app's error
    COPY, so asserting it would be inventing a promise — and a suite pinned to
    wording breaks on a copy edit that changed no behaviour."""
    cat = {"questions": [
        _q("A", required=True),
        _q("B", type="number", validation={"min": "1", "max": "9"}),
        _q("C", validation={"pattern": "x"}),
        _q("D", type="select", options=_STATES, options_total=4),
    ]}
    for s in derive_catalog_scenarios(cat)["scenarios"]:
        assert s["expect"] in ("accept", "reject")


def test_every_scenario_traces_back_to_the_rule_that_justifies_it():
    """``basis`` is what lets a reader check an assertion against the page fact
    behind it — and lets a scenario be retired when that fact disappears."""
    cat = {"questions": [_q("A", required=True,
                            validation={"minlength": "3", "pattern": "x"})]}
    scenarios = derive_catalog_scenarios(cat)["scenarios"]
    assert scenarios
    for s in scenarios:
        assert s["basis"] in (BASIS_REQUIRED, BASIS_MIN_LENGTH, BASIS_PATTERN)
        assert s["question_id"] == "q::A" and s["field"] == "A"


def test_one_question_can_justify_several_kinds_at_once():
    s = derive_field_scenarios(
        _q("Coverage", type="number", required=True,
           options=["Basic", "Plus"], options_total=2,
           validation={"min": "1", "max": "5"}))
    assert _kinds(s) == [KIND_BOUNDARY, KIND_NEGATIVE, KIND_POSITIVE]


def test_the_summary_counts_by_kind():
    cat = {"questions": [
        _q("A", required=True),
        _q("B", type="select", options=_STATES, options_total=4),
        _q("C", type="number", validation={"min": "1"}),
    ]}
    by_kind = derive_catalog_scenarios(cat)["summary"]["by_kind"]
    assert by_kind[KIND_NEGATIVE] == 1
    assert by_kind[KIND_POSITIVE] == 1
    assert by_kind[KIND_BOUNDARY] == 2
