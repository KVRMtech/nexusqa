"""M2.2 / T-BR-06 — THE QE-CENTRAL HALF of the real-crawl proof.

``engines/qe-explorer/tests/browser/test_m22_catalog_evidence.py`` drives a real
browser over a real application and proves the crawl OBSERVES a conditional
rule, a dependency the page does not declare, locators, validation and a
250-option enumeration.  It cannot prove any of that reaches the catalogue,
because it cannot import this service.

This file closes that half.  It reads the coverage that crawl really produced —
committed at ``fixtures/m22_real_crawl_coverage.json``, re-recorded with
``QEC_M22_RECAPTURE=1`` — and runs it through the PRODUCTION catalogue path:

    build_states_index / build_ledger_by_url   (the fold's readers)
      -> extract_controls                      (per page state)
      -> build_master_catalog                  (joined to the durable rules)
      -> the GET /apps/{id}/catalog representation

Nothing is hand-authored and nothing is mocked.  The input is what a browser
saw; the assertions are about what a client would receive.

WHY NOT JUST TEST THE DATABASE.  The DB seam is covered separately
(``test_catalog_store.py``, ``test_migration_roundtrip.py``) and needs Postgres.
What M2.2's stop condition actually forbids is data that "exists only in an
intermediate capture structure" — and that is a question about the COMPOSITION
path, which is pure and can be proven here without a database, on every machine,
every run.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.catalog import (
    LOCATOR_STATE_UNVERIFIED,
    LOCATOR_STATE_VERIFIED,
    RULE_STATE_OBSERVED,
    RULE_STATE_UNVERIFIED,
    build_ledger_by_url,
    build_master_catalog,
    build_states_index,
    extract_controls,
)

CAPTURED = Path(__file__).resolve().parent / "fixtures" / "m22_real_crawl_coverage.json"

# ── What the crawled application actually does ───────────────────────────────
#
# Read off ``proving-grounds/catalog-evidence/index.html`` by a human.  These are
# the "compare the result directly against the application behaviour" half of
# T-BR-06: the catalogue's claims are checked against the app, not against the
# crawl that produced them.
GATE_FIELD = "I have reviewed the health questionnaire"
GATE_CONTROL = "Continue to review"
DRIVER_FIELD = "State of residence"
DEPENDENT_FIELD = "County"
CLIPPED_FIELD = "Country of citizenship"
CLIPPED_TRUE_TOTAL = 251          # 250 countries + the placeholder row
VALIDATED_FIELD = "Face amount ($)"
UNGATED_FIELD = "Send me product updates"


@pytest.fixture(scope="module")
def coverage() -> dict:
    if not CAPTURED.is_file():
        pytest.skip(
            "%s is missing — it is the real crawl output this half of the M2.2 "
            "proof reads. Produce it with QEC_M22_RECAPTURE=1 pytest "
            "tests/browser/test_m22_catalog_evidence.py in qe-explorer."
            % CAPTURED.name)
    return json.loads(CAPTURED.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def catalog(coverage: dict) -> dict:
    """The Master Catalog, built from the real crawl by the production path."""
    states = build_states_index(coverage)
    ledger_by_url = build_ledger_by_url(coverage)
    nodes = [{
        "node_fp": fingerprint,
        "url": str(state.get("location") or ""),
        "controls": extract_controls(state, ledger_by_url),
    } for fingerprint, state in states.items()]
    # ``coverage.discovered_rules`` is already the shape ``rule_store.fetch_rules``
    # returns — the two sides share one vocabulary rather than a translation
    # nobody owns (see contracts/m17_business_rule_v1.json).
    return build_master_catalog(nodes, rules=coverage.get("discovered_rules") or [])


def _q(catalog: dict, name: str) -> dict:
    matches = [q for q in catalog["questions"] if q["name"] == name]
    assert matches, (
        "the catalogue built from the real crawl has no question %r. It holds: %s"
        % (name, sorted(q["name"] for q in catalog["questions"])))
    return matches[0]


# ── The crawl produced something to catalogue at all ─────────────────────────

def test_the_real_crawl_produced_a_catalogue(catalog):
    assert catalog["questions"], (
        "a real crawl of a real application produced ZERO catalogue questions")


def test_the_application_s_questions_are_all_present(catalog):
    for name in (GATE_FIELD, DRIVER_FIELD, DEPENDENT_FIELD, CLIPPED_FIELD,
                 VALIDATED_FIELD):
        _q(catalog, name)


# ── T-BR-01 · the observed business rule reached the catalogue ───────────────

def test_the_proven_rule_reaches_the_question_it_is_about(catalog):
    """The application disables its forward control and declares nowhere why.

    This assertion is the end of the chain the milestone exists to close: a
    sentence the crawl had to run an EXPERIMENT to learn, carried through the
    completion callback, the durable store and the catalogue builder, and
    readable on the question a client would look at.
    """
    q = _q(catalog, GATE_FIELD)
    assert q["business_rule_state"] == RULE_STATE_OBSERVED
    assert GATE_CONTROL in q["business_rule"], (
        "the rule does not name the control it gates: %r" % q["business_rule"])
    evidence = q["business_rule_evidence"]
    assert evidence["source"] == "crawl_experiment"
    assert evidence["gates"] == GATE_CONTROL
    assert evidence["rule_key"].startswith("rule:")


def test_the_question_that_gates_nothing_is_unverified(catalog):
    """``Send me product updates`` is an ordinary optional checkbox sitting
    beside the gating one.  If it acquired a rule, the join would be matching on
    shape rather than on what the application did — and every other assertion
    here would still pass."""
    q = _q(catalog, UNGATED_FIELD)
    assert q["business_rule_state"] == RULE_STATE_UNVERIFIED
    assert q["business_rule"] == ""


def test_most_of_this_application_declines_to_claim_a_rule(catalog):
    """A catalogue in which everything is rule-bearing is not evidence.

    This application gates exactly one advance, so at most one question may come
    back ``observed``.  Stated as a ceiling rather than an equality because the
    honest failure here is over-claiming, not under-claiming.
    """
    observed = [q for q in catalog["questions"]
                if q["business_rule_state"] == RULE_STATE_OBSERVED]
    assert len(observed) <= 1, (
        "%d questions claim a proven business rule on an application with one "
        "gate: %s" % (len(observed), [q["name"] for q in observed]))


# ── T-BR-02 · the dependency reached the catalogue ───────────────────────────

def test_the_dependency_can_be_queried_from_the_master_catalog(catalog):
    """T-BR-02's acceptance, verbatim: a field dependent on another field can be
    queried from the master catalog with its dependency intact."""
    assert _q(catalog, DEPENDENT_FIELD)["depends_on"] == DRIVER_FIELD


def test_the_driver_is_not_marked_dependent(catalog):
    assert not _q(catalog, DRIVER_FIELD).get("depends_on")


def test_exactly_the_conditional_question_is_conditional(catalog):
    dependent = [q["name"] for q in catalog["questions"] if q.get("depends_on")]
    assert dependent == [DEPENDENT_FIELD], (
        "the application has one conditional question; the catalogue reports %s"
        % dependent)


# ── T-BR-03 · locators reached the catalogue, and belong to their questions ──

def test_each_question_carries_its_own_element_s_handle(catalog):
    for name, strategy in ((VALIDATED_FIELD, "testid"),
                           (DRIVER_FIELD, "dom_id"),
                           (CLIPPED_FIELD, "dom_id")):
        q = _q(catalog, name)
        assert q["locator_state"] == LOCATOR_STATE_VERIFIED
        assert q["locator"]["strategy"] == strategy
        assert q["locator"]["value"]


def test_locators_are_not_shared_between_questions(catalog):
    """The merge in ``build_master_catalog`` keeps "the richest observation".

    Applied carelessly to locators that would put one control's handle on
    another control's row — which reads perfectly plausible and is wrong.
    """
    handles = [(q["locator"]["strategy"], q["locator"]["value"])
               for q in catalog["questions"]
               if q["locator_state"] == LOCATOR_STATE_VERIFIED]
    assert len(set(handles)) == len(handles), (
        "two catalogue questions claim the same handle: %s" % handles)


def test_the_control_the_application_identifies_by_nothing_is_absent_not_invented(catalog):
    """The referral-code input carries no id, no testid, no label and no class.

    WHAT THE REAL CRAWL ACTUALLY DOES WITH IT, which is not what this test first
    asserted: the control never becomes a catalogue question at all.  A field
    with no accessible name has no question TEXT, and a question with no text is
    not a row a client can review — so it is dropped upstream, at the fill and
    the snapshot, long before any locator is considered.

    That is the right outcome and it is worth pinning, because the failure mode
    it rules out is the expensive one: a catalogue that kept the row and gave it
    a positional selector would look complete, would name a question nobody
    asked, and would point a generated script at an element chosen by counting.
    Absent beats invented.

    (The UNVERIFIED locator state itself is exercised where it can be — the unit
    contract on both sides of the wire, which can construct the control directly:
    ``test_m22_catalog_contract.py``.)
    """
    assert not [q for q in catalog["questions"]
                if "referral" in q["name"].lower()], (
        "the nameless control was catalogued as a question; it has no question "
        "text, so whatever text it was given was composed rather than observed")
    for q in catalog["questions"]:
        assert q["name"].strip(), "a catalogue question with no text is not one"
        if q["locator_state"] == LOCATOR_STATE_UNVERIFIED:
            assert q["locator"]["value"] == "", (
                "an unverified locator must carry no handle, not a synthesised one")
            assert q["locator"]["unverified_reason"]


def test_no_question_claims_a_handle_it_does_not_have(catalog):
    """``locator_state`` and the locator itself must agree.

    They are read by different consumers — a badge in the UI and a compiler rung
    respectively — and a row where one says verified and the other is empty
    would show a green badge over nothing.
    """
    for q in catalog["questions"]:
        loc = q.get("locator")
        if q["locator_state"] == LOCATOR_STATE_VERIFIED:
            assert loc and loc.get("verified") and loc.get("value")
        elif q["locator_state"] == "absent":
            assert not loc, (
                "%r is marked as having no locator while carrying one" % q["name"])


# ── T-BR-04 · validation reached the catalogue ───────────────────────────────

def test_the_declared_validation_is_on_the_question(catalog):
    validation = _q(catalog, VALIDATED_FIELD)["validation"]
    assert validation["min"] == "50000"
    assert validation["max"] == "2000000"
    assert validation["step"] == "10000"


# ── T-BR-05 · the 250-option control ─────────────────────────────────────────

def test_the_250_option_control_reports_what_the_application_offers(catalog):
    q = _q(catalog, CLIPPED_FIELD)
    assert q["options_total"] == CLIPPED_TRUE_TOTAL, (
        "the application offers %d answers; the catalogue says %r"
        % (CLIPPED_TRUE_TOTAL, q["options_total"]))
    assert q["options_total"] >= len(q["options"])


def test_a_short_enumeration_is_not_reported_as_clipped(catalog):
    q = _q(catalog, DRIVER_FIELD)
    assert q["options_total"] == len(q["options"])


# ── T-BR-04 · what a client actually receives ────────────────────────────────

def test_the_api_representation_carries_the_complete_record(catalog):
    """``GET /apps/{app_id}/catalog`` returns ``{"app_id": …, **master}``.

    Asserted on the serialised payload rather than the in-memory dict because a
    value that cannot be JSON-encoded is a 500 for the client and a green test
    for us.
    """
    payload = json.loads(json.dumps({"app_id": "app_proving_ground", **catalog}))
    q = [x for x in payload["questions"] if x["name"] == DEPENDENT_FIELD][0]
    for key in ("question_id", "name", "type", "required", "options",
                "options_total", "depends_on", "locator", "locator_state",
                "business_rule", "business_rule_state", "pages"):
        assert key in q, "the catalog API response is missing %r" % key


def test_the_summary_reports_how_much_of_the_catalogue_is_evidence(catalog):
    summary = catalog["summary"]
    assert summary["with_business_rule"] == 1
    assert summary["with_dependency"] == 1
    assert summary["with_verified_locator"] >= 3
    assert summary["with_validation"] >= 1
    assert summary["question_count"] == len(catalog["questions"])


def test_no_answer_anyone_typed_reached_the_catalogue(catalog):
    """The crawl FILLS this application — it must, or there is no experiment and
    no dependency.  What it typed must not appear in the deliverable: the
    catalogue is a record of the questions, and the identity seed's synthesized
    values are still values."""
    blob = json.dumps(catalog)
    for leaked in ("value_committed", "form_snapshot\":"):
        assert leaked not in blob
