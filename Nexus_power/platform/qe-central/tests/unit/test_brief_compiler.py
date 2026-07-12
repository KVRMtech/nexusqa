"""ANSWERS P2 — English→contract compiler (authoring-time, no LLM/DB/network).

The safety-critical property under test: the GROUNDING GATE. An LLM proposal is
UNTRUSTED — only items that re-validate against the app's real captured labels /
value nodes enter the active answer_key; everything else is flagged for human
confirmation. A hallucinated field can NEVER enter an active contract.
"""
from __future__ import annotations

import json

from app.services.brief_compiler import (
    build_prompt, compile_brief, ground_and_assemble, match_label, parse_proposal,
)

KNOWN = ["Age", "Coverage amount ($)", "Term length", "State", "Do you use tobacco?"]
NODES = [{"label": "Monthly premium", "source_hint": ".prem"}]


# ── match_label (grounding primitive) ──────────────────────────────────────

def test_match_label_exact_and_token_and_substring():
    assert match_label("age", KNOWN) == "Age"                       # normalized exact
    assert match_label("coverage amount", KNOWN) == "Coverage amount ($)"  # token subset
    assert match_label("tobacco", KNOWN) == "Do you use tobacco?"   # substring
    assert match_label("premium", KNOWN) is None                    # not a form label


# ── parse_proposal (tolerant) ──────────────────────────────────────────────

def test_parse_proposal_dict_json_fenced_and_garbage():
    assert parse_proposal({"fill": [1]})["fill"] == [1]
    assert parse_proposal('{"outcomes": [{"field": "x"}]}')["outcomes"][0]["field"] == "x"
    fenced = "```json\n{\"rules\": [{\"kind\": \"bound\"}]}\n```"
    assert parse_proposal(fenced)["rules"][0]["kind"] == "bound"
    wrapped = 'Here you go:\n{"fill": [{"label": "Age", "value": "35"}]}\nThanks!'
    assert parse_proposal(wrapped)["fill"][0]["value"] == "35"
    assert parse_proposal("not json at all") == {"fill": [], "outcomes": [], "rules": [], "unmatched": []}


# ── ground_and_assemble (THE moat) ─────────────────────────────────────────

def test_grounded_fill_enters_contract_keyed_by_real_label():
    proposal = {"fill": [{"label": "age", "value": "35"},
                         {"label": "smoker?", "value": "No"}]}  # "smoker?" != tobacco label token-wise
    out = ground_and_assemble(proposal, known_labels=KNOWN)
    assert out["answer_key"]["fill"] == {"Age": "35"}  # keyed by REAL label, not "age"
    # the unmatched one is flagged, NOT silently used
    flagged = [r for r in out["review"] if not r["grounded"]]
    assert any(r["field"] == "smoker?" for r in flagged)


def test_hallucinated_field_never_enters_contract():
    proposal = {"fill": [{"label": "Social Security Number", "value": "999"},
                         {"label": "Secret Admin Flag", "value": "true"}]}
    out = ground_and_assemble(proposal, known_labels=KNOWN)
    assert out["answer_key"]["fill"] == {}       # nothing hallucinated activated
    assert out["ungrounded"] == 2 and out["grounded"] == 0


def test_outcome_grounded_by_source_hint_from_known_node():
    proposal = {"outcomes": [{"field": "monthly_premium", "expected": 75.0,
                              "tolerance": 0.01, "source_hint": ".prem"}]}
    out = ground_and_assemble(proposal, known_labels=KNOWN, known_value_nodes=NODES)
    assert len(out["answer_key"]["outcomes"]) == 1
    assert out["answer_key"]["outcomes"][0]["source_hint"] == ".prem"


def test_outcome_ungrounded_without_source_hint_needs_confirmation():
    # premium is not a form label and there is no source_hint → cannot auto-ground
    proposal = {"outcomes": [{"field": "monthly_premium", "expected": 75.0}]}
    out = ground_and_assemble(proposal, known_labels=KNOWN, known_value_nodes=NODES)
    assert out["answer_key"]["outcomes"] == []
    r = [x for x in out["review"] if x["kind"] == "outcome"][0]
    assert r["needs_confirmation"] is True and "source_hint" in r["reason"]


def test_outcome_grounded_via_known_label_getbylabel_fallback():
    proposal = {"outcomes": [{"field": "State", "expected": "TX", "match": "exact"}]}
    out = ground_and_assemble(proposal, known_labels=KNOWN)  # no nodes, but "State" IS a label
    assert len(out["answer_key"]["outcomes"]) == 1


def test_rule_grounds_via_source_hint_or_known_label_else_flagged():
    # explicit source_hint → grounded
    by_hint = ground_and_assemble(
        {"rules": [{"kind": "bound", "field": "risk score", "op": "<=", "limit": 700,
                    "source_hint": "#score"}]}, known_labels=KNOWN)
    assert len(by_hint["answer_key"]["rules"]) == 1
    # "coverage" token-matches the real label "Coverage amount ($)" → grounded via getByLabel
    by_label = ground_and_assemble(
        {"rules": [{"kind": "bound", "field": "coverage", "op": "<=", "limit": 2000000}]},
        known_labels=KNOWN)
    assert len(by_label["answer_key"]["rules"]) == 1
    # a field that matches NOTHING and has no source_hint → cannot ground → flagged
    floating = ground_and_assemble(
        {"rules": [{"kind": "bound", "field": "underwriting margin", "op": "<=", "limit": 5}]},
        known_labels=KNOWN)
    assert floating["answer_key"]["rules"] == [] and floating["ungrounded"] == 1


def test_unmatched_items_are_surfaced():
    out = ground_and_assemble(
        {"unmatched": [{"text": "premium under $50 for young folks", "reason": "vague"}]},
        known_labels=KNOWN)
    assert any(r["kind"] == "unmatched" for r in out["review"])


# ── compile_brief (orchestration + honest degradation) ─────────────────────

def test_compile_brief_with_fake_llm_grounds_end_to_end():
    def fake_llm(prompt: str) -> str:
        assert "KNOWN_LABELS" in prompt and "Age" in prompt  # prompt is grounded
        return json.dumps({
            "fill": [{"label": "Age", "value": "35"},
                     {"label": "Coverage amount", "value": "500000"}],
            "outcomes": [{"field": "monthly_premium", "expected": 75.0,
                          "tolerance": 0.01, "source_hint": ".prem"}],
            "rules": [{"kind": "bound", "field": "coverage", "op": "<=",
                       "limit": 2000000, "source_hint": ".prem"}],
        })
    out = compile_brief(
        notes="35yo non-smoker, $500k, 20yr, TX",
        answers="premium about $75/mo; coverage never over $2M",
        known_labels=KNOWN, known_value_nodes=NODES, propose_fn=fake_llm)
    assert out["answer_key"]["fill"] == {"Age": "35", "Coverage amount ($)": "500000"}
    assert out["answer_key"]["outcomes"][0]["expected"] == 75.0
    assert len(out["answer_key"]["rules"]) == 1
    assert out["llm_error"] == ""


def test_compile_brief_no_llm_degrades_honestly():
    out = compile_brief(notes="x", answers="y", known_labels=KNOWN, propose_fn=None)
    assert out["answer_key"] == {"fill": {}, "outcomes": [], "rules": []}
    assert "no LLM configured" in out["llm_error"]


def test_compile_brief_llm_crash_never_breaks_onboarding():
    def boom(prompt: str):
        raise RuntimeError("provider 503")
    out = compile_brief(notes="x", answers="y", known_labels=KNOWN, propose_fn=boom)
    assert out["answer_key"]["fill"] == {} and "LLM proposal failed" in out["llm_error"]


def test_build_prompt_hard_constrains_to_known_vocabulary():
    p = build_prompt(notes="n", answers="a", known_labels=KNOWN, known_value_nodes=NODES)
    assert "ONLY use field labels" in p or "ONLY the field names" in p or "verbatim" in p
    assert ".prem" in p and "Age" in p
