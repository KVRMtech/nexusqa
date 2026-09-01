"""O2 NL Case Builder — unit tests (pure, no network, no database).

Pins the grounding gate and verification pipeline that turns a plain-English
test-case request into a structured, honestly-labelled case specification.
"""
from __future__ import annotations

import pytest

from app.services.nl_case_builder import (
    VERIFIED_CONFIRMED,
    VERIFIED_UNVERIFIED,
    assemble_case,
    build_nl_prompt,
    collect_vocabulary,
    compile_nl_case,
    ground_nl_intent,
    match_journey,
    match_journey_by_fields,
    parse_nl_proposal,
    verify_outcomes,
)


# ── fixtures ─────────────────────────────────────────────────────────────────

def _journey(
    journey_id: str = "j-1", name: str = "Term Life Quote",
    controls: list | None = None, outcomes: list | None = None,
) -> dict:
    return {
        "journey_id": journey_id,
        "business_name": name,
        "controls": controls or [],
        "outcomes": outcomes or [],
    }


def _control(name: str, *, ctype: str = "input", options: list | None = None,
             sig: str = "") -> dict:
    return {
        "name": name,
        "type": ctype,
        "options": options or [],
        "signature": sig or f"sig-{name.lower().replace(' ', '_')}",
    }


def _outcome(label: str, *, selector: str = "", value_type: str = "currency") -> dict:
    return {"label": label, "selector": selector or f"#{label.lower().replace(' ', '-')}", "value_type": value_type}


def _rule(field: str, expected, *, match: str = "numeric",
          tolerance: float | None = None, source: str = "rate_table") -> dict:
    r = {"kind": "outcome_rule", "field": field, "expected": expected,
         "match": match, "source": source}
    if tolerance is not None:
        r["tolerance"] = tolerance
    return r


LIFE_JOURNEY = _journey(
    "j-1", "Term Life Quote",
    controls=[
        _control("Age", ctype="input"),
        _control("Gender", ctype="radio", options=["Male", "Female"]),
        _control("Coverage Amount", ctype="select",
                 options=["$250,000", "$500,000", "$1,000,000"]),
        _control("Smoker", ctype="radio", options=["Yes", "No"]),
        _control("State", ctype="select",
                 options=["TX", "CA", "NY", "FL"]),
    ],
    outcomes=[
        _outcome("Monthly Premium"),
        _outcome("Annual Premium"),
    ],
)

WHOLE_LIFE_JOURNEY = _journey(
    "j-2", "Whole Life Quote",
    controls=[
        _control("Age", ctype="input", sig="sig-age-wl"),
        _control("Coverage Amount", ctype="select",
                 options=["$100,000", "$250,000"], sig="sig-coverage-wl"),
    ],
    outcomes=[_outcome("Monthly Premium", selector="#premium-wl")],
)


# ── collect_vocabulary ───────────────────────────────────────────────────────

def test_collect_vocabulary_deduplicates():
    vocab = collect_vocabulary([LIFE_JOURNEY, WHOLE_LIFE_JOURNEY])
    labels = vocab["control_labels"]
    assert labels.count("Age") == 1
    assert labels.count("Coverage Amount") == 1
    assert "Gender" in labels
    assert vocab["outcome_labels"].count("Monthly Premium") == 1
    assert len(vocab["journey_names"]) == 2


def test_collect_vocabulary_empty():
    vocab = collect_vocabulary([])
    assert vocab["control_labels"] == []
    assert vocab["outcome_labels"] == []
    assert vocab["journey_names"] == []


def test_collect_vocabulary_controls_have_options():
    vocab = collect_vocabulary([LIFE_JOURNEY])
    controls = vocab["controls"]
    gender_info = controls.get("gender")
    assert gender_info is not None
    assert gender_info["options"] == ["Male", "Female"]


# ── build_nl_prompt ──────────────────────────────────────────────────────────

def test_prompt_contains_vocabulary():
    vocab = collect_vocabulary([LIFE_JOURNEY])
    prompt = build_nl_prompt(nl_text="a 35yo female", vocabulary=vocab)
    assert "Age" in prompt
    assert "Gender" in prompt
    assert "Monthly Premium" in prompt
    assert "Term Life Quote" in prompt
    assert "Male" in prompt and "Female" in prompt


# ── parse_nl_proposal ────────────────────────────────────────────────────────

def test_parse_valid_json():
    raw = '{"journey_hint": "Term Life", "fields": [{"label": "Age", "value": "35"}], "expected_outcomes": [], "unmatched": []}'
    parsed = parse_nl_proposal(raw)
    assert parsed["journey_hint"] == "Term Life"
    assert len(parsed["fields"]) == 1


def test_parse_json_with_fences():
    raw = '```json\n{"journey_hint": "", "fields": [], "expected_outcomes": [], "unmatched": []}\n```'
    parsed = parse_nl_proposal(raw)
    assert parsed["fields"] == []


def test_parse_malformed_returns_empty():
    parsed = parse_nl_proposal("this is not json at all")
    assert parsed["journey_hint"] == ""
    assert parsed["fields"] == []


def test_parse_dict_input():
    parsed = parse_nl_proposal({"journey_hint": "X", "fields": [{"label": "A", "value": "1"}]})
    assert parsed["journey_hint"] == "X"
    assert len(parsed["fields"]) == 1


# ── match_journey ────────────────────────────────────────────────────────────

def test_match_journey_exact():
    result = match_journey("Term Life Quote", [LIFE_JOURNEY, WHOLE_LIFE_JOURNEY])
    assert result is not None
    assert result["journey_id"] == "j-1"


def test_match_journey_substring():
    result = match_journey("Term Life", [LIFE_JOURNEY, WHOLE_LIFE_JOURNEY])
    assert result is not None
    assert result["journey_id"] == "j-1"


def test_match_journey_case_insensitive():
    result = match_journey("term life quote", [LIFE_JOURNEY, WHOLE_LIFE_JOURNEY])
    assert result is not None
    assert result["journey_id"] == "j-1"


def test_match_journey_no_match():
    result = match_journey("Auto Insurance", [LIFE_JOURNEY, WHOLE_LIFE_JOURNEY])
    assert result is None


def test_match_journey_empty_hint():
    assert match_journey("", [LIFE_JOURNEY]) is None


# ── match_journey_by_fields ──────────────────────────────────────────────────

def test_match_by_fields_picks_higher_overlap():
    fields = [
        {"grounded_label": "Age"},
        {"grounded_label": "Gender"},
        {"grounded_label": "Smoker"},
    ]
    result = match_journey_by_fields(fields, [LIFE_JOURNEY, WHOLE_LIFE_JOURNEY])
    assert result is not None
    assert result["journey_id"] == "j-1"


def test_match_by_fields_empty():
    assert match_journey_by_fields([], [LIFE_JOURNEY]) is None


# ── ground_nl_intent ─────────────────────────────────────────────────────────

def test_ground_fields_enumerable_becomes_override():
    vocab = collect_vocabulary([LIFE_JOURNEY])
    proposal = {
        "journey_hint": "Term Life Quote",
        "fields": [{"label": "Gender", "value": "Female"}],
        "expected_outcomes": [],
        "unmatched": [],
    }
    result = ground_nl_intent(proposal, vocab, [LIFE_JOURNEY])
    assert "sig-gender" in result["choice_overrides"]
    assert result["choice_overrides"]["sig-gender"] == "female"
    assert "Gender" not in result["fill"]


def test_ground_fields_freetext_becomes_fill():
    vocab = collect_vocabulary([LIFE_JOURNEY])
    proposal = {
        "journey_hint": "",
        "fields": [{"label": "Age", "value": "35"}],
        "expected_outcomes": [],
        "unmatched": [],
    }
    result = ground_nl_intent(proposal, vocab, [LIFE_JOURNEY])
    assert result["fill"]["Age"] == "35"
    assert not result["choice_overrides"]


def test_ground_unmatched_field():
    vocab = collect_vocabulary([LIFE_JOURNEY])
    proposal = {
        "journey_hint": "",
        "fields": [{"label": "Favourite Colour", "value": "blue"}],
        "expected_outcomes": [],
        "unmatched": [],
    }
    result = ground_nl_intent(proposal, vocab, [LIFE_JOURNEY])
    assert result["fill"] == {}
    assert result["ungrounded"] >= 1
    assert any(r["field"] == "Favourite Colour" and not r["grounded"]
               for r in result["review"])


def test_ground_outcome_matched():
    vocab = collect_vocabulary([LIFE_JOURNEY])
    proposal = {
        "journey_hint": "",
        "fields": [],
        "expected_outcomes": [{"label": "Monthly Premium", "expected": 40}],
        "unmatched": [],
    }
    result = ground_nl_intent(proposal, vocab, [LIFE_JOURNEY])
    assert len(result["expected_outcomes"]) == 1
    assert result["expected_outcomes"][0]["field"] == "Monthly Premium"


def test_ground_outcome_unmatched_still_included():
    vocab = collect_vocabulary([LIFE_JOURNEY])
    proposal = {
        "journey_hint": "",
        "fields": [],
        "expected_outcomes": [{"label": "Total Cost", "expected": 500}],
        "unmatched": [],
    }
    result = ground_nl_intent(proposal, vocab, [LIFE_JOURNEY])
    assert len(result["expected_outcomes"]) == 1
    assert result["expected_outcomes"][0]["field"] == "Total Cost"
    assert result["ungrounded"] >= 1


def test_ground_journey_auto_matched_by_fields():
    vocab = collect_vocabulary([LIFE_JOURNEY, WHOLE_LIFE_JOURNEY])
    proposal = {
        "journey_hint": "",
        "fields": [
            {"label": "Gender", "value": "Male"},
            {"label": "Smoker", "value": "No"},
        ],
        "expected_outcomes": [],
        "unmatched": [],
    }
    result = ground_nl_intent(proposal, vocab, [LIFE_JOURNEY, WHOLE_LIFE_JOURNEY])
    assert result["journey"] is not None
    assert result["journey"]["journey_id"] == "j-1"


def test_ground_option_value_case_insensitive():
    vocab = collect_vocabulary([LIFE_JOURNEY])
    proposal = {
        "journey_hint": "",
        "fields": [{"label": "Gender", "value": "female"}],
        "expected_outcomes": [],
        "unmatched": [],
    }
    result = ground_nl_intent(proposal, vocab, [LIFE_JOURNEY])
    assert "sig-gender" in result["choice_overrides"]


def test_ground_option_value_no_match_becomes_fill():
    vocab = collect_vocabulary([LIFE_JOURNEY])
    proposal = {
        "journey_hint": "",
        "fields": [{"label": "Gender", "value": "Non-binary"}],
        "expected_outcomes": [],
        "unmatched": [],
    }
    result = ground_nl_intent(proposal, vocab, [LIFE_JOURNEY])
    assert "sig-gender" not in result["choice_overrides"]
    assert result["fill"]["Gender"] == "Non-binary"


def test_ground_multiple_overrides():
    vocab = collect_vocabulary([LIFE_JOURNEY])
    proposal = {
        "journey_hint": "",
        "fields": [
            {"label": "Gender", "value": "Male"},
            {"label": "Smoker", "value": "No"},
            {"label": "State", "value": "TX"},
            {"label": "Age", "value": "42"},
        ],
        "expected_outcomes": [],
        "unmatched": [],
    }
    result = ground_nl_intent(proposal, vocab, [LIFE_JOURNEY])
    assert len(result["choice_overrides"]) == 3
    assert result["fill"] == {"Age": "42"}


# ── verify_outcomes ──────────────────────────────────────────────────────────

def test_verify_confirmed_by_numeric_rule():
    outcomes = [{"field": "Monthly Premium", "expected": 40, "match": "numeric"}]
    rules = [_rule("Monthly Premium", 40)]
    verified = verify_outcomes(outcomes, rules)
    assert verified[0]["verification"] == VERIFIED_CONFIRMED
    assert verified[0]["rule_source"] == "rate_table"


def test_verify_confirmed_within_tolerance():
    outcomes = [{"field": "Monthly Premium", "expected": 41, "match": "numeric"}]
    rules = [_rule("Monthly Premium", 40, tolerance=2.0)]
    verified = verify_outcomes(outcomes, rules)
    assert verified[0]["verification"] == VERIFIED_CONFIRMED


def test_verify_unverified_outside_tolerance():
    outcomes = [{"field": "Monthly Premium", "expected": 50, "match": "numeric"}]
    rules = [_rule("Monthly Premium", 40, tolerance=2.0)]
    verified = verify_outcomes(outcomes, rules)
    assert verified[0]["verification"] == VERIFIED_UNVERIFIED


def test_verify_unverified_no_rules():
    outcomes = [{"field": "Monthly Premium", "expected": 40, "match": "numeric"}]
    verified = verify_outcomes(outcomes, [])
    assert verified[0]["verification"] == VERIFIED_UNVERIFIED
    assert verified[0]["rule_source"] is None


def test_verify_confirmed_exact_match():
    outcomes = [{"field": "Status", "expected": "Approved", "match": "exact"}]
    rules = [_rule("Status", "Approved", match="exact", source="eligibility")]
    verified = verify_outcomes(outcomes, rules)
    assert verified[0]["verification"] == VERIFIED_CONFIRMED


def test_verify_unverified_wrong_field():
    outcomes = [{"field": "Annual Premium", "expected": 480, "match": "numeric"}]
    rules = [_rule("Monthly Premium", 40)]
    verified = verify_outcomes(outcomes, rules)
    assert verified[0]["verification"] == VERIFIED_UNVERIFIED


def test_verify_numeric_with_dollar_signs():
    outcomes = [{"field": "Monthly Premium", "expected": "$40.00", "match": "numeric"}]
    rules = [_rule("Monthly Premium", 40)]
    verified = verify_outcomes(outcomes, rules)
    assert verified[0]["verification"] == VERIFIED_CONFIRMED


def test_verify_contains_match():
    outcomes = [{"field": "Result", "expected": "eligible", "match": "exact"}]
    rules = [_rule("Result", "eligible", match="contains", source="rules_v2")]
    verified = verify_outcomes(outcomes, rules)
    assert verified[0]["verification"] == VERIFIED_CONFIRMED


# ── assemble_case ────────────────────────────────────────────────────────────

def test_assemble_case_shape():
    grounded = {
        "journey": {"journey_id": "j-1", "business_name": "Term Life Quote"},
        "choice_overrides": {"sig-gender": "female"},
        "fill": {"Age": "35"},
        "review": [{"kind": "field", "field": "Age", "grounded": True,
                     "needs_confirmation": False, "reason": "matched"}],
        "grounded": 2,
        "ungrounded": 0,
    }
    verified = [
        {"field": "Monthly Premium", "expected": 40, "match": "numeric",
         "tolerance": None, "verification": "confirmed", "rule_source": "rate_table"},
    ]
    case = assemble_case(nl_text="a 35yo female $40 premium",
                         grounded=grounded, verified_outcomes=verified)
    assert case["journey_id"] == "j-1"
    assert case["journey_name"] == "Term Life Quote"
    assert case["choice_overrides"] == {"sig-gender": "female"}
    assert case["fill"] == {"Age": "35"}
    assert case["outcomes_confirmed"] == 1
    assert case["outcomes_unverified"] == 0


def test_assemble_case_no_journey():
    grounded = {
        "journey": None,
        "choice_overrides": {},
        "fill": {"Age": "35"},
        "review": [],
        "grounded": 1,
        "ungrounded": 0,
    }
    case = assemble_case(nl_text="test", grounded=grounded, verified_outcomes=[])
    assert case["journey_id"] == ""
    assert case["journey_name"] == ""


# ── compile_nl_case (end-to-end with fake LLM) ──────────────────────────────

def test_compile_nl_case_end_to_end():
    fake_response = {
        "journey_hint": "Term Life Quote",
        "fields": [
            {"label": "Age", "value": "35"},
            {"label": "Gender", "value": "Female"},
        ],
        "expected_outcomes": [
            {"label": "Monthly Premium", "expected": 40, "match": "numeric"},
        ],
        "unmatched": [],
    }
    rules = [_rule("Monthly Premium", 40, tolerance=1.0)]
    result = compile_nl_case(
        nl_text="35yo female, premium $40",
        journey_summaries=[LIFE_JOURNEY],
        rules=rules,
        propose_fn=lambda _prompt: fake_response,
    )
    assert result["journey_id"] == "j-1"
    assert result["choice_overrides"]["sig-gender"] == "female"
    assert result["fill"]["Age"] == "35"
    assert result["outcomes_confirmed"] == 1
    assert result["outcomes_unverified"] == 0
    assert result["llm_error"] == ""


def test_compile_nl_case_llm_failure_degrades_honestly():
    result = compile_nl_case(
        nl_text="35yo female",
        journey_summaries=[LIFE_JOURNEY],
        propose_fn=lambda _: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert result["llm_error"].startswith("LLM proposal failed")
    assert result["grounded"] == 0


def test_compile_nl_case_no_llm():
    result = compile_nl_case(
        nl_text="35yo female",
        journey_summaries=[LIFE_JOURNEY],
        propose_fn=None,
    )
    assert "no LLM configured" in result["llm_error"]
    assert result["grounded"] == 0


def test_compile_nl_case_unverified_outcome():
    fake_response = {
        "journey_hint": "Term Life Quote",
        "fields": [],
        "expected_outcomes": [
            {"label": "Monthly Premium", "expected": 99, "match": "numeric"},
        ],
        "unmatched": [],
    }
    result = compile_nl_case(
        nl_text="premium $99",
        journey_summaries=[LIFE_JOURNEY],
        rules=[_rule("Monthly Premium", 40)],
        propose_fn=lambda _: fake_response,
    )
    assert result["outcomes_unverified"] == 1
    assert result["outcomes_confirmed"] == 0
    verified = result["expected_outcomes"]
    assert verified[0]["verification"] == VERIFIED_UNVERIFIED
