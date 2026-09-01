"""O1 Rule Oracle — unit tests for client-rule first-crawl validation.

Tests cover rule normalization, evaluation against traversal outcomes,
numeric/exact/contains/range matching, tolerance, edge cases, and the
summary/reporting contract.
"""
from __future__ import annotations

import pytest

from app.services.rule_oracle import (
    MATCH_CONTAINS,
    MATCH_EXACT,
    MATCH_NUMERIC,
    MATCH_RANGE,
    RULE_KIND,
    STATUS_CONFIRMED,
    STATUS_MALFORMED,
    STATUS_NOT_APPLICABLE,
    STATUS_UNCONFIRMED,
    RuleResult,
    RuleSpec,
    evaluate_rule,
    evaluate_rules,
    normalize_rules,
    summarize_evaluation,
)


# ── normalize_rules ─────────────────────────────────────────────────────────

def test_normalize_single_numeric_rule():
    rules = normalize_rules([{
        "kind": "outcome_rule", "field": "monthly_premium",
        "expected": 28.40, "match": "numeric", "tolerance": 0.50,
        "source": "rate_table_v3",
    }])
    assert len(rules) == 1
    r = rules[0]
    assert r.field == "monthly_premium"
    assert r.expected == 28.40
    assert r.match == MATCH_NUMERIC
    assert r.tolerance == 0.50
    assert r.source == "rate_table_v3"


def test_normalize_auto_detects_numeric():
    rules = normalize_rules([{
        "kind": "outcome_rule", "field": "premium", "expected": 50.0,
    }])
    assert rules[0].match == MATCH_NUMERIC
    assert rules[0].expected == 50.0


def test_normalize_auto_detects_exact():
    rules = normalize_rules([{
        "kind": "outcome_rule", "field": "tier", "expected": "Preferred",
    }])
    assert rules[0].match == MATCH_EXACT
    assert rules[0].expected == "Preferred"


def test_normalize_exact_rule():
    rules = normalize_rules([{
        "kind": "outcome_rule", "field": "tier",
        "expected": "Preferred", "match": "exact",
    }])
    assert len(rules) == 1
    assert rules[0].match == MATCH_EXACT
    assert rules[0].expected == "Preferred"


def test_normalize_contains_rule():
    rules = normalize_rules([{
        "kind": "outcome_rule", "field": "decline_codes",
        "expected": "UW-17", "match": "contains",
    }])
    assert len(rules) == 1
    assert rules[0].match == MATCH_CONTAINS
    assert rules[0].expected == "UW-17"


def test_normalize_range_rule():
    rules = normalize_rules([{
        "kind": "outcome_rule", "field": "premium",
        "match": "range", "low": 20.0, "high": 35.0,
    }])
    assert len(rules) == 1
    assert rules[0].match == MATCH_RANGE
    assert rules[0].low == 20.0
    assert rules[0].high == 35.0


def test_normalize_skips_non_outcome_rule_kind():
    rules = normalize_rules([
        {"kind": "monotonic", "field": "premium"},
        {"kind": "outcome_rule", "field": "tier", "expected": "Gold"},
    ])
    assert len(rules) == 1
    assert rules[0].field == "tier"


def test_normalize_skips_missing_field():
    rules = normalize_rules([{"kind": "outcome_rule", "expected": 28.0}])
    assert rules == []


def test_normalize_skips_invalid_match_type():
    rules = normalize_rules([{
        "kind": "outcome_rule", "field": "x", "expected": 1, "match": "fuzzy",
    }])
    assert rules == []


def test_normalize_skips_non_numeric_expected_for_numeric():
    rules = normalize_rules([{
        "kind": "outcome_rule", "field": "x",
        "expected": "not-a-number", "match": "numeric",
    }])
    assert rules == []


def test_normalize_skips_empty_expected_for_exact():
    rules = normalize_rules([{
        "kind": "outcome_rule", "field": "x", "expected": "", "match": "exact",
    }])
    assert rules == []


def test_normalize_skips_bad_range_no_bounds():
    rules = normalize_rules([{
        "kind": "outcome_rule", "field": "x", "match": "range",
    }])
    assert rules == []


def test_normalize_tolerant_on_none():
    assert normalize_rules(None) == []


def test_normalize_tolerant_on_non_mapping():
    assert normalize_rules(["bad", 123, None]) == []


def test_normalize_reads_equals_alias():
    rules = normalize_rules([{
        "kind": "outcome_rule", "field": "x", "equals": 42,
    }])
    assert rules[0].expected == 42.0


def test_normalize_reads_value_alias():
    rules = normalize_rules([{
        "kind": "outcome_rule", "field": "x", "value": "Gold",
    }])
    assert rules[0].expected == "Gold"
    assert rules[0].match == MATCH_EXACT


def test_normalize_reads_name_alias_for_field():
    rules = normalize_rules([{
        "kind": "outcome_rule", "name": "premium", "expected": 28.0,
    }])
    assert rules[0].field == "premium"


def test_normalize_reads_label_alias_for_field():
    rules = normalize_rules([{
        "kind": "outcome_rule", "label": "Premium Amount", "expected": 28.0,
    }])
    assert rules[0].field == "Premium Amount"


def test_normalize_currency_string_expected():
    rules = normalize_rules([{
        "kind": "outcome_rule", "field": "premium",
        "expected": "$28.40", "match": "numeric",
    }])
    assert rules[0].expected == 28.40


def test_normalize_multiple_rules():
    rules = normalize_rules([
        {"kind": "outcome_rule", "field": "premium", "expected": 28.40},
        {"kind": "outcome_rule", "field": "tier", "expected": "Gold", "match": "exact"},
        {"kind": "outcome_rule", "field": "range_field", "match": "range", "low": 10, "high": 50},
    ])
    assert len(rules) == 3


# ── evaluate_rule — numeric ─────────────────────────────────────────────────

def test_numeric_exact_match():
    rule = RuleSpec(field="premium", expected=28.40, match=MATCH_NUMERIC)
    result = evaluate_rule(rule, [{"label": "premium", "value": "28.40"}])
    assert result.status == STATUS_CONFIRMED


def test_numeric_match_with_tolerance():
    rule = RuleSpec(field="premium", expected=28.40, match=MATCH_NUMERIC, tolerance=0.50)
    result = evaluate_rule(rule, [{"label": "premium", "value": "28.65"}])
    assert result.status == STATUS_CONFIRMED


def test_numeric_mismatch_outside_tolerance():
    rule = RuleSpec(field="premium", expected=28.40, match=MATCH_NUMERIC, tolerance=0.50)
    result = evaluate_rule(rule, [{"label": "premium", "value": "30.00"}])
    assert result.status == STATUS_UNCONFIRMED
    assert "expected 28.4" in result.reason


def test_numeric_mismatch_no_tolerance():
    rule = RuleSpec(field="premium", expected=28.40, match=MATCH_NUMERIC)
    result = evaluate_rule(rule, [{"label": "premium", "value": "28.50"}])
    assert result.status == STATUS_UNCONFIRMED


def test_numeric_strips_currency():
    rule = RuleSpec(field="premium", expected=28.40, match=MATCH_NUMERIC)
    result = evaluate_rule(rule, [{"label": "premium", "value": "$28.40/mo"}])
    assert result.status == STATUS_CONFIRMED


def test_numeric_non_numeric_observed():
    rule = RuleSpec(field="premium", expected=28.40, match=MATCH_NUMERIC)
    result = evaluate_rule(rule, [{"label": "premium", "value": "N/A"}])
    assert result.status == STATUS_UNCONFIRMED


# ── evaluate_rule — exact ────────────────────────────────────────────────────

def test_exact_match():
    rule = RuleSpec(field="tier", expected="Preferred", match=MATCH_EXACT)
    result = evaluate_rule(rule, [{"label": "tier", "value": "Preferred"}])
    assert result.status == STATUS_CONFIRMED


def test_exact_case_insensitive():
    rule = RuleSpec(field="tier", expected="preferred", match=MATCH_EXACT)
    result = evaluate_rule(rule, [{"label": "tier", "value": "Preferred"}])
    assert result.status == STATUS_CONFIRMED


def test_exact_mismatch():
    rule = RuleSpec(field="tier", expected="Preferred", match=MATCH_EXACT)
    result = evaluate_rule(rule, [{"label": "tier", "value": "Standard"}])
    assert result.status == STATUS_UNCONFIRMED


# ── evaluate_rule — contains ─────────────────────────────────────────────────

def test_contains_match():
    rule = RuleSpec(field="decline_codes", expected="UW-17", match=MATCH_CONTAINS)
    result = evaluate_rule(rule, [{"label": "decline_codes", "value": "Codes: UW-17, UW-22"}])
    assert result.status == STATUS_CONFIRMED


def test_contains_mismatch():
    rule = RuleSpec(field="decline_codes", expected="UW-17", match=MATCH_CONTAINS)
    result = evaluate_rule(rule, [{"label": "decline_codes", "value": "Code: UW-22"}])
    assert result.status == STATUS_UNCONFIRMED


# ── evaluate_rule — range ────────────────────────────────────────────────────

def test_range_within():
    rule = RuleSpec(field="premium", match=MATCH_RANGE, low=20.0, high=35.0)
    result = evaluate_rule(rule, [{"label": "premium", "value": "28.40"}])
    assert result.status == STATUS_CONFIRMED


def test_range_at_boundary():
    rule = RuleSpec(field="premium", match=MATCH_RANGE, low=20.0, high=35.0)
    r_low = evaluate_rule(rule, [{"label": "premium", "value": "20.0"}])
    r_high = evaluate_rule(rule, [{"label": "premium", "value": "35.0"}])
    assert r_low.status == STATUS_CONFIRMED
    assert r_high.status == STATUS_CONFIRMED


def test_range_outside():
    rule = RuleSpec(field="premium", match=MATCH_RANGE, low=20.0, high=35.0)
    result = evaluate_rule(rule, [{"label": "premium", "value": "40.00"}])
    assert result.status == STATUS_UNCONFIRMED


def test_range_lower_only():
    rule = RuleSpec(field="premium", match=MATCH_RANGE, low=20.0, high=None)
    result = evaluate_rule(rule, [{"label": "premium", "value": "100.00"}])
    assert result.status == STATUS_CONFIRMED


def test_range_upper_only():
    rule = RuleSpec(field="premium", match=MATCH_RANGE, low=None, high=50.0)
    result = evaluate_rule(rule, [{"label": "premium", "value": "30.00"}])
    assert result.status == STATUS_CONFIRMED


# ── evaluate_rule — field matching ───────────────────────────────────────────

def test_field_not_in_outcomes():
    rule = RuleSpec(field="premium", expected=28.40, match=MATCH_NUMERIC)
    result = evaluate_rule(rule, [{"label": "tier", "value": "Gold"}])
    assert result.status == STATUS_NOT_APPLICABLE


def test_field_match_case_insensitive():
    rule = RuleSpec(field="Monthly Premium", expected=28.40, match=MATCH_NUMERIC)
    result = evaluate_rule(rule, [{"label": "monthly premium", "value": "28.40"}])
    assert result.status == STATUS_CONFIRMED


def test_field_match_normalizes_whitespace():
    rule = RuleSpec(field="Monthly  Premium", expected=28.40, match=MATCH_NUMERIC)
    result = evaluate_rule(rule, [{"label": "monthly_premium", "value": "28.40"}])
    assert result.status == STATUS_CONFIRMED


def test_empty_outcomes():
    rule = RuleSpec(field="premium", expected=28.40, match=MATCH_NUMERIC)
    result = evaluate_rule(rule, [])
    assert result.status == STATUS_NOT_APPLICABLE


def test_none_outcomes():
    rule = RuleSpec(field="premium", expected=28.40, match=MATCH_NUMERIC)
    result = evaluate_rule(rule, None)
    assert result.status == STATUS_NOT_APPLICABLE


def test_skips_blank_label_outcomes():
    rule = RuleSpec(field="premium", expected=28.40, match=MATCH_NUMERIC)
    result = evaluate_rule(rule, [{"label": "", "value": "28.40"}])
    assert result.status == STATUS_NOT_APPLICABLE


def test_skips_non_mapping_outcomes():
    rule = RuleSpec(field="premium", expected=28.40, match=MATCH_NUMERIC)
    result = evaluate_rule(rule, [None, "bad", 123])
    assert result.status == STATUS_NOT_APPLICABLE


# ── evaluate_rules (batch) ───────────────────────────────────────────────────

def test_evaluate_multiple_rules():
    outcomes = [
        {"label": "premium", "value": "28.40"},
        {"label": "tier", "value": "Gold"},
    ]
    rules = [
        RuleSpec(field="premium", expected=28.40, match=MATCH_NUMERIC),
        RuleSpec(field="tier", expected="Gold", match=MATCH_EXACT),
    ]
    results = evaluate_rules(rules, outcomes)
    assert len(results) == 2
    assert all(r.status == STATUS_CONFIRMED for r in results)


def test_evaluate_mixed_results():
    outcomes = [{"label": "premium", "value": "28.40"}]
    rules = [
        RuleSpec(field="premium", expected=28.40, match=MATCH_NUMERIC),
        RuleSpec(field="tier", expected="Gold", match=MATCH_EXACT),
    ]
    results = evaluate_rules(rules, outcomes)
    assert results[0].status == STATUS_CONFIRMED
    assert results[1].status == STATUS_NOT_APPLICABLE


# ── summarize_evaluation ─────────────────────────────────────────────────────

def test_summary_all_confirmed():
    results = [
        RuleResult(rule=RuleSpec(field="p", expected=28, source="rt"),
                   status=STATUS_CONFIRMED, observed_value="28.0",
                   reason="numeric match"),
    ]
    s = summarize_evaluation(results)
    assert s["total_rules"] == 1
    assert s["confirmed"] == 1
    assert s["unconfirmed"] == 0
    assert s["all_applicable_pass"] is True


def test_summary_with_unconfirmed():
    results = [
        RuleResult(rule=RuleSpec(field="p", expected=28),
                   status=STATUS_CONFIRMED, observed_value="28.0"),
        RuleResult(rule=RuleSpec(field="t", expected="Gold"),
                   status=STATUS_UNCONFIRMED, observed_value="Silver"),
    ]
    s = summarize_evaluation(results)
    assert s["all_applicable_pass"] is False
    assert s["confirmed"] == 1
    assert s["unconfirmed"] == 1


def test_summary_all_not_applicable():
    results = [
        RuleResult(rule=RuleSpec(field="x", expected=1),
                   status=STATUS_NOT_APPLICABLE),
    ]
    s = summarize_evaluation(results)
    assert s["all_applicable_pass"] is False


def test_summary_confirmed_plus_not_applicable():
    results = [
        RuleResult(rule=RuleSpec(field="p", expected=28, source="rt"),
                   status=STATUS_CONFIRMED, observed_value="28.0"),
        RuleResult(rule=RuleSpec(field="x", expected=1),
                   status=STATUS_NOT_APPLICABLE),
    ]
    s = summarize_evaluation(results)
    assert s["all_applicable_pass"] is True


def test_summary_empty():
    s = summarize_evaluation([])
    assert s["all_applicable_pass"] is False
    assert s["total_rules"] == 0


def test_summary_details_include_source():
    results = [
        RuleResult(
            rule=RuleSpec(field="premium", expected=28, source="rate_v3"),
            status=STATUS_CONFIRMED, observed_value="28.0",
            reason="numeric match"),
    ]
    s = summarize_evaluation(results)
    assert s["details"][0]["source"] == "rate_v3"
    assert s["details"][0]["field"] == "premium"
    assert s["details"][0]["status"] == STATUS_CONFIRMED


# ── edge cases ───────────────────────────────────────────────────────────────

def test_observed_value_is_recorded():
    rule = RuleSpec(field="premium", expected=28.40, match=MATCH_NUMERIC)
    result = evaluate_rule(rule, [{"label": "premium", "value": "28.40"}])
    assert result.observed_value == "28.40"


def test_multiple_outcomes_first_label_match_wins():
    rule = RuleSpec(field="premium", expected=28.40, match=MATCH_NUMERIC)
    result = evaluate_rule(rule, [
        {"label": "premium", "value": "28.40"},
        {"label": "premium", "value": "99.99"},
    ])
    assert result.status == STATUS_CONFIRMED
    assert result.observed_value == "28.40"
