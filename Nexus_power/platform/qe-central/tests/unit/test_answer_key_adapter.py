"""answer_key → explorer fill-contract adapter (the Data-tab silent-drop fix)."""
from __future__ import annotations

from app.services.answer_key import explorer_fill_contract


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
