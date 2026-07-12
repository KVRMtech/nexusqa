"""answer_key → explorer fill-contract adapter (the Data-tab silent-drop fix)."""
from __future__ import annotations

from app.services.answer_key import explorer_fill_contract, value_oracle_contract


def test_fill_map_becomes_semantic():
    out = explorer_fill_contract({"fill": {"age": 35, "coverage": 500000}})
    assert out["semantic"] == {"age": "35", "coverage": "500000"}
    assert out["exact"] == {} and out["regex_rules"] == []


def test_explicit_advanced_sections_pass_through():
    out = explorer_fill_contract({
        "exact": {"Username": "member"},
        "semantic": {"zip": "12345"},
        "regex_rules": [{"pattern": "date.*", "value": "1990-01-01"}],
    })
    assert out["exact"] == {"Username": "member"}
    assert out["semantic"] == {"zip": "12345"}
    assert out["regex_rules"] == [{"pattern": "date.*", "value": "1990-01-01"}]


def test_exact_wins_over_fill_same_key():
    out = explorer_fill_contract({"exact": {"age": "18"}, "fill": {"age": "35"}})
    assert out["exact"]["age"] == "18"
    assert "age" not in out["semantic"]  # not downgraded


def test_wizard_legacy_notes_answers_is_empty_not_crash():
    # The historical broken shape: no fill data → empty contract (honest no-op),
    # never a crash. outcomes/notes are NOT projected into the fill contract.
    out = explorer_fill_contract({"notes": "age 35 non-smoker", "answers": {"premium": 28.4}})
    assert out == {"exact": {}, "semantic": {}, "regex_rules": []}


def test_tolerant_on_garbage():
    assert explorer_fill_contract(None) == {"exact": {}, "semantic": {}, "regex_rules": []}
    assert explorer_fill_contract({"fill": "not-a-map", "regex_rules": ["bad"]}) == {
        "exact": {}, "semantic": {}, "regex_rules": []}


# ───────────────────────── value-oracle contract ──────────────────────────

def test_value_oracle_structured_list():
    out = value_oracle_contract({"outcomes": [
        {"when": {"persona": "p1"}, "field": "monthly_premium", "equals": 28.40,
         "tolerance": 0.50, "source_hint": "#premium"},
    ]})
    assert out["outcomes"] == [{
        "field": "monthly_premium", "when": {"persona": "p1"}, "expected": 28.40,
        "tolerance": 0.50, "source_hint": "#premium", "match": "numeric"}]


def test_value_oracle_flat_map_numeric_and_string():
    out = value_oracle_contract({"outcomes": {"monthly_premium": "$28.40", "tier": "Preferred"}})
    by = {o["field"]: o for o in out["outcomes"]}
    assert by["monthly_premium"]["match"] == "numeric" and by["monthly_premium"]["expected"] == 28.40
    assert by["tier"]["match"] == "exact" and by["tier"]["expected"] == "Preferred"


def test_value_oracle_list_expected_becomes_contains():
    out = value_oracle_contract({"outcomes": {"decline_codes": ["UW-17", "UW-22"]}})
    assert out["outcomes"][0] == {"field": "decline_codes", "when": {}, "expected": "UW-17",
                                  "tolerance": None, "source_hint": "", "match": "contains"}


def test_value_oracle_drops_raw_and_blanks():
    # free-text parse-failure sentinel + a blank value are ungroundable → dropped
    out = value_oracle_contract({"outcomes": {"_raw": "age 35 non-smoker ~ $28", "empty": ""}})
    assert out["outcomes"] == []


def test_value_oracle_reads_expected_and_value_aliases():
    out = value_oracle_contract({"outcomes": [
        {"field": "a", "expected": "X"}, {"name": "b", "value": 7}]})
    by = {o["field"]: o for o in out["outcomes"]}
    assert by["a"]["expected"] == "X" and by["a"]["match"] == "exact"
    assert by["b"]["expected"] == 7.0 and by["b"]["match"] == "numeric"


def test_value_oracle_rules_passthrough_only_typed():
    out = value_oracle_contract({"rules": [{"kind": "monotonic", "field": "premium"}, {"junk": 1}]})
    assert out["rules"] == [{"kind": "monotonic", "field": "premium"}]


def test_value_oracle_tolerant_on_garbage():
    assert value_oracle_contract(None) == {"outcomes": [], "rules": []}
    assert value_oracle_contract({"outcomes": "nope", "rules": "nope"}) == {"outcomes": [], "rules": []}
    # fill/exact are NEVER read by the value oracle (separate seam)
    assert value_oracle_contract({"fill": {"age": 35}, "exact": {"x": "y"}}) == {"outcomes": [], "rules": []}
