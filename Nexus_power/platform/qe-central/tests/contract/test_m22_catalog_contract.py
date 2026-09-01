"""M2.2 / T-BR-01..05 — THE QE-CENTRAL HALF of the catalogue wire contract.

The other half lives in ``engines/qe-explorer/tests/test_m22_catalog_contract.py``
and proves the PRODUCER emits the frozen shape.  This one proves the CONSUMER
reads every field of it and carries each one all the way to the deliverable —
``extract_controls`` → ``build_master_catalog`` → the ``GET /apps/{id}/catalog``
representation — because a field that is read and then dropped one layer later
is indistinguishable, from the producer's side, from a field never sent.

That is not hypothetical.  Before M2.2, ``extract_controls`` read ``depends_on``
correctly and ``build_master_catalog`` did not copy it into the row it built, so
a dependency survived the wire, survived the extractor, and died in the function
that composes the product's actual output.  Every conditional question in the
fleet was catalogued as unconditional.  A per-layer test on either side would
have passed.

The suite is therefore written END TO END through the pure layer, and asserts on
what a client would receive.  Nothing here touches a database: the DB seam is
covered by ``tests/test_catalog_store.py`` and the migration by
``test_migration_roundtrip.py``.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.catalog import (
    LOCATOR_STATE_ABSENT,
    LOCATOR_STATE_UNVERIFIED,
    LOCATOR_STATE_VERIFIED,
    MAX_CATALOG_OPTIONS,
    RULE_STATE_OBSERVED,
    RULE_STATE_UNVERIFIED,
    build_master_catalog,
    extract_controls,
    index_rules_by_field,
    snapshot_catalog,
)


def _contract() -> dict:
    """Load the frozen contract by walking up to the ``Nexus_power`` root."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "contracts" / "m22_catalog_question_v1.json"
        if candidate.is_file():
            return json.loads(candidate.read_text(encoding="utf-8"))
    raise AssertionError(
        "contracts/m22_catalog_question_v1.json not found above %s — the frozen "
        "wire contract is the only thing tying this service's catalogue shape to "
        "qe-explorer's, and it must not be deleted to make a test pass" % here
    )


CONTRACT = _contract()

#: The rule an experiment PROVED, in the shape ``rule_store.fetch_rules`` returns.
GATE_RULE = {
    "key": "rule:57915c15af59a515a06c1fa5",
    "kind": "advance_gate",
    "url_template": "a.test/apply/*/health",
    "blocked_label": "Continue",
    "field_label": "Health Conditions",
    "proof": ("Continue requires an answer to 'Health Conditions' before it is "
              "enabled (proven: the app enabled it when the agent answered)"),
    "schema_version": 1,
}


def _page_state() -> dict:
    """One captured page state carrying every signal the contract names."""
    return {
        "location": "https://a.test/apply/8814/health",
        "form_snapshot_signals": {
            "Health Conditions": {
                "type": "checkbox", "options": [], "options_total": 0,
                "required": True,
                "locator": {"strategy": "dom_id", "value": "health-conditions",
                            "verified": True, "bindable": False,
                            "role": "checkbox"},
            },
            "County": {
                "type": "select", "options": ["Travis", "Harris"],
                "options_total": 254, "required": False, "depends_on": "State",
                "locator": {"strategy": "accessible_name", "value": "County",
                            "verified": True, "bindable": True,
                            "role": "combobox"},
            },
            "Face Amount": {
                "type": "text", "options": [], "options_total": 0,
                "required": True, "min": "50000", "max": "2000000",
                "step": "10000",
                "locator": {"strategy": "testid", "value": "face-amount",
                            "verified": True, "bindable": False},
            },
            "Unlabelled Field": {
                "type": "text", "options": [], "options_total": 0,
                "required": False,
                "locator": {"strategy": "", "value": "", "verified": False,
                            "unverified_reason": "no_handle_declared"},
            },
        },
    }


def _catalog(rules=(GATE_RULE,)) -> dict:
    controls = extract_controls(_page_state())
    return build_master_catalog(
        [{"node_fp": "fp_health", "url": "https://a.test/apply/8814/health",
          "controls": controls}],
        rules=list(rules))


def _q(catalog: dict, name: str) -> dict:
    matches = [q for q in catalog["questions"] if q["name"] == name]
    assert len(matches) == 1, "expected exactly one %r question" % name
    return matches[0]


# ── T-BR-04 · the complete record reaches the deliverable ────────────────────

def test_every_contract_field_survives_to_the_catalogue_question():
    """The end-to-end assertion this milestone's stop condition names.

    Read through the whole pure path rather than at each layer, because the
    defect class here is a field that arrives and is then not copied forward.
    """
    q = _q(_catalog(), "County")
    for key in ("question_id", "name", "type", "required", "options",
                "options_total", "depends_on", "locator", "locator_state",
                "business_rule", "business_rule_state", "pages"):
        assert key in q, (
            "%r is missing from the catalogue question.  It exists in the "
            "captured page state; a layer between the wire and the deliverable "
            "dropped it, which is exactly how depends_on was lost before." % key)


def test_a_question_with_no_evidence_still_answers_every_question_about_itself():
    """The honesty markers are written on EVERY row, not just the rich ones.

    A blank ``business_rule`` is ambiguous between "this question gates nothing"
    and "no build has looked".  Removing that ambiguity is the entire reason
    ``business_rule_state`` is a field rather than an inference.
    """
    q = _q(_catalog(rules=()), "Face Amount")
    assert q["business_rule"] == ""
    assert q["business_rule_state"] == RULE_STATE_UNVERIFIED
    assert q["locator_state"] in CONTRACT["locator_states"]


def test_the_honesty_markers_use_the_frozen_vocabulary():
    for q in _catalog()["questions"]:
        assert q["business_rule_state"] in CONTRACT["business_rule_states"]
        assert q["locator_state"] in CONTRACT["locator_states"]


# ── T-BR-01 · business rules ─────────────────────────────────────────────────

def test_an_observed_rule_reaches_the_question_it_is_about():
    q = _q(_catalog(), "Health Conditions")
    assert q["business_rule"] == GATE_RULE["proof"]
    assert q["business_rule_state"] == RULE_STATE_OBSERVED
    evidence = q["business_rule_evidence"]
    assert evidence["source"] == "crawl_experiment"
    assert evidence["rule_key"] == GATE_RULE["key"]
    assert evidence["gates"] == "Continue"


def test_the_rule_lands_on_the_field_not_on_the_control_it_gates():
    """``field_label`` is the question; ``blocked_label`` is a Continue button.

    Joining on the wrong one would attach the rule to a control that is not a
    catalogue question at all — and on an application where the button happens
    to share a label with a field, would attach it to the wrong question and
    look entirely plausible.
    """
    catalog = _catalog()
    assert not [q for q in catalog["questions"] if q["name"] == "Continue"]
    assert _q(catalog, "County")["business_rule_state"] == RULE_STATE_UNVERIFIED


def test_no_rule_is_invented_for_a_question_that_merely_looks_gating():
    """T-BR-01's central prohibition.

    ``Health Conditions`` is required, is a checkbox, and sits on a wizard step
    with a Continue button — every surface signal of a gate.  With no rule in the
    store it must still come back UNVERIFIED.  A catalogue that inferred the
    sentence from the shape would be indistinguishable from one that proved it,
    and the whole artifact would stop being evidence.
    """
    q = _q(_catalog(rules=()), "Health Conditions")
    assert q["business_rule"] == ""
    assert q["business_rule_state"] == RULE_STATE_UNVERIFIED
    assert "business_rule_evidence" not in q


def test_a_rule_with_no_sentence_is_not_a_rule():
    """A row that proves nothing a reader can act on must not inflate the count
    of questions carrying business rules."""
    empty = {**GATE_RULE, "proof": ""}
    assert index_rules_by_field([empty]) == {}
    assert _catalog(rules=(empty,))["summary"]["with_business_rule"] == 0


def test_rules_join_on_the_normalised_label():
    """The store holds the label as the page rendered it; the catalogue holds
    the accessible name.  Casing and spacing differences between the two must not
    silently drop a rule that was genuinely proved."""
    loud = {**GATE_RULE, "field_label": "  HEALTH   CONDITIONS "}
    assert _q(_catalog(rules=(loud,)),
              "Health Conditions")["business_rule_state"] == RULE_STATE_OBSERVED


# ── T-BR-02 · dependencies ───────────────────────────────────────────────────

def test_the_dependency_survives_every_transformation_layer():
    state = _page_state()
    control = [c for c in extract_controls(state) if c["name"] == "County"][0]
    assert control["depends_on"] == "State", "lost between the wire and extract"
    assert _q(_catalog(), "County")["depends_on"] == "State", (
        "lost between extract and the master catalogue — the exact layer that "
        "dropped it before M2.2")


def test_a_dependency_is_added_by_a_later_sighting_and_never_cleared():
    """A dependent select is empty until its driver is answered, so the sighting
    that PROVES the dependency is usually not the first one.  Keeping the first
    observation would hold the emptiest view of exactly the questions whose
    conditionality matters most."""
    without = {"name": "County", "type": "select", "options": [],
               "question_id": "q_county"}
    with_dep = {**without, "depends_on": "State",
                "options": ["Travis"], "options_total": 254}
    for order in ((without, with_dep), (with_dep, without)):
        catalog = build_master_catalog(
            [{"node_fp": "a", "url": "https://a.test/1", "controls": [order[0]]},
             {"node_fp": "b", "url": "https://a.test/2", "controls": [order[1]]}])
        assert _q(catalog, "County")["depends_on"] == "State"


# ── T-BR-03 · locators ───────────────────────────────────────────────────────

def test_the_locator_survives_and_belongs_to_its_own_question():
    catalog = _catalog()
    assert _q(catalog, "Face Amount")["locator"]["value"] == "face-amount"
    assert _q(catalog, "County")["locator"]["value"] == "County"
    assert _q(catalog, "Health Conditions")["locator"]["value"] == "health-conditions"


def test_an_unverified_locator_is_carried_as_a_finding_not_dropped():
    q = _q(_catalog(), "Unlabelled Field")
    assert q["locator_state"] == LOCATOR_STATE_UNVERIFIED
    assert q["locator"]["unverified_reason"] == "no_handle_declared"


def test_a_branch_question_has_no_locator_and_is_not_accused_of_lacking_one():
    """A questionnaire question folded in from a BRANCH row is a control
    signature and an option label — it was never an element, so ``absent`` is
    the correct state and ``UNVERIFIED`` would be a false accusation against the
    application."""
    catalog = build_master_catalog([], branches=[{
        "node_fp": "fp1", "control_signature": "sig_tobacco",
        "control_label_norm": "Do you use tobacco?", "option_label_norm": "No"}])
    assert catalog["questions"][0]["locator_state"] == LOCATOR_STATE_ABSENT


def test_a_verified_locator_is_never_replaced_by_an_unverified_one():
    """The same question met on two pages, identified on one of them.

    Taking the newest unconditionally would lose the only handle the catalogue
    had, and would do it silently.
    """
    good = {"name": "County", "question_id": "q_c", "type": "select",
            "locator": {"strategy": "dom_id", "value": "county",
                        "verified": True}}
    bad = {"name": "County", "question_id": "q_c", "type": "select",
           "locator": {"strategy": "", "value": "", "verified": False}}
    for order in ((good, bad), (bad, good)):
        catalog = build_master_catalog(
            [{"node_fp": "a", "url": "https://a.test/1", "controls": [order[0]]},
             {"node_fp": "b", "url": "https://a.test/2", "controls": [order[1]]}])
        q = _q(catalog, "County")
        assert q["locator_state"] == LOCATOR_STATE_VERIFIED
        assert q["locator"]["value"] == "county"


# ── T-BR-05 · options_total ──────────────────────────────────────────────────

def test_a_clipped_enumeration_reports_both_numbers():
    """The 250-option case, stated as the milestone states it: a control must
    not appear to have only as many answers as capture happened to keep."""
    control = {"name": "Country", "question_id": "q_country", "type": "select",
               "options": [f"Country {i}" for i in range(MAX_CATALOG_OPTIONS)],
               "options_total": 400}
    q = _q(build_master_catalog(
        [{"node_fp": "a", "url": "https://a.test/1", "controls": [control]}]),
        "Country")
    assert q["options_total"] == 400
    assert len(q["options"]) == MAX_CATALOG_OPTIONS
    assert q["options_total"] > len(q["options"])


def test_the_catalogue_never_claims_fewer_answers_than_it_stores():
    control = {"name": "Term", "question_id": "q_term", "type": "select",
               "options": ["10", "20", "30"], "options_total": 1}
    q = _q(build_master_catalog(
        [{"node_fp": "a", "url": "https://a.test/1", "controls": [control]}]),
        "Term")
    assert q["options_total"] == 3


def test_options_total_is_present_on_every_question_including_unclipped_ones():
    """Present-on-some / absent-on-others is the inconsistency a consumer reads
    as a bug, and it is what makes ``options_total > len(options)`` unsafe to
    evaluate without a guard on every call site."""
    for q in _catalog()["questions"]:
        assert isinstance(q["options_total"], int)


def test_the_summary_counts_the_clipping_rather_than_hiding_it():
    control = {"name": "Country", "question_id": "q_country", "type": "select",
               "options": ["A", "B"], "options_total": 250}
    summary = build_master_catalog(
        [{"node_fp": "a", "url": "https://a.test/1",
          "controls": [control]}])["summary"]
    assert summary["options_clipped"] == 1


# ── The summary is a count of evidence, not of rows ──────────────────────────

def test_the_summary_counts_only_what_is_proven():
    summary = _catalog()["summary"]
    assert summary["question_count"] == 4
    assert summary["with_business_rule"] == 1
    assert summary["with_dependency"] == 1
    assert summary["with_verified_locator"] == 3
    assert summary["with_validation"] == 1
    assert summary["options_clipped"] == 1


# ── P6 · the regression diff can see the new shape ───────────────────────────

@pytest.mark.parametrize("mutation", [
    {"depends_on": "Country"},
    {"options_total": 999},
])
def test_a_change_to_the_new_shape_changes_the_snapshot_hash(mutation):
    """A question that became conditional, and an answer set that grew behind a
    clip, are both changes to the APPLICATION.  A regression diff that could not
    see them would report "no change" on a release that altered the form.
    """
    base = _catalog()
    before = snapshot_catalog(base)["snapshot_hash"]
    after_questions = [dict(q) for q in base["questions"]]
    after_questions[0].update(mutation)
    after = snapshot_catalog({"questions": after_questions})["snapshot_hash"]
    assert before != after


def test_an_unchanged_catalogue_reproduces_its_hash_exactly():
    assert (snapshot_catalog(_catalog())["snapshot_hash"]
            == snapshot_catalog(_catalog())["snapshot_hash"])


# ── Tolerance: bad input degrades, never crashes ─────────────────────────────

@pytest.mark.parametrize("rules", [None, [], [{}], ["not a mapping"],
                                   [{"field_label": "x"}]])
def test_malformed_rules_yield_an_unverified_catalogue_not_an_exception(rules):
    catalog = _catalog(rules=rules or ())
    assert catalog["summary"]["with_business_rule"] == 0
    assert all(q["business_rule_state"] == RULE_STATE_UNVERIFIED
               for q in catalog["questions"])


def test_a_malformed_options_total_does_not_poison_the_row():
    state = _page_state()
    state["form_snapshot_signals"]["County"]["options_total"] = "many"
    controls = extract_controls(state)
    county = [c for c in controls if c["name"] == "County"][0]
    assert county["options_total"] == 2
