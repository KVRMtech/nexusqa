"""ANSWERS P1.C — business-value oracle assertion compiler (pure, no DB/network).

Locks the never-green-wash doctrine: grounded → HARD assertion (PROVEN-capable via
the frozen verdict reducer); ungroundable → honest UNVERIFIED comment, never a pass.
"""
from __future__ import annotations

from app.services.test_factory.value_oracle import (
    value_assertion_lines, rule_assertion_lines, NXNUM_JS, NXCMP_JS,
)


def test_numeric_with_source_hint_emits_hard_nxnum():
    lines, uses = value_assertion_lines([
        {"field": "monthly_premium", "expected": 28.40, "tolerance": 0.50,
         "source_hint": "#premium", "match": "numeric"},
    ])
    assert uses is True
    assert len(lines) == 1
    assert "await __nxNum(page.locator('#premium'), 28.4, 0.5);" in lines[0]
    assert "PROVEN value oracle" in lines[0]


def test_exact_with_source_hint_emits_hard_containstext():
    lines, uses = value_assertion_lines([
        {"field": "tier", "expected": "Preferred", "source_hint": ".tier-badge", "match": "exact"},
    ])
    assert uses is False
    assert "await expect(page.locator('.tier-badge')).toContainText(/Preferred/i);" in lines[0]
    assert "PROVEN value oracle" in lines[0]


def test_contains_uses_first_scalar_token():
    lines, _ = value_assertion_lines([
        {"field": "decline", "expected": "UW-17", "source_hint": "#code", "match": "contains"},
    ])
    assert "toContainText(/UW/i)" in lines[0]  # first alnum token, regex-safe


def test_no_source_hint_no_capture_is_unverified_never_pass():
    lines, uses = value_assertion_lines([
        {"field": "monthly_premium", "expected": 28.40, "match": "numeric"},
    ])
    assert uses is False
    assert lines[0].strip().startswith("// UNVERIFIED value oracle: 'monthly_premium'")
    assert "await" not in lines[0]  # NOT a silent assertion — an honest comment


def test_no_source_hint_but_captured_label_grounds_via_getbylabel():
    lines, _ = value_assertion_lines(
        [{"field": "Coverage", "expected": "500000", "match": "exact"}],
        field_meta={"labels": ["Coverage", "Age"]},
    )
    assert "page.getByLabel('Coverage')" in lines[0]
    assert "toContainText(/500000/i)" in lines[0]


def test_numeric_expected_not_a_number_is_unverified():
    lines, uses = value_assertion_lines([
        {"field": "x", "expected": "not-a-number", "source_hint": "#x", "match": "numeric"},
    ])
    assert uses is False
    assert "UNVERIFIED" in lines[0] and "not numeric" in lines[0]


def test_source_hint_is_js_escaped():
    lines, _ = value_assertion_lines([
        {"field": "x", "expected": "y", "source_hint": "[data-x='a']", "match": "exact"},
    ])
    # single quote inside the hint must be backslash-escaped, never break the literal
    assert r"page.locator('[data-x=\'a\']')" in lines[0]


def test_tolerant_on_garbage_and_empty():
    assert value_assertion_lines([]) == ([], False)
    assert value_assertion_lines([None, "nope", 5]) == ([], False)


def test_nxnum_js_carries_unexpected_value_phrase_for_proven_classification():
    # The frozen verdict reducer keys PROVEN on "unexpected value" in the error —
    # the injected numeric comparator MUST emit that phrase on a real miss.
    assert "unexpected value" in NXNUM_JS
    assert "innerText()" in NXNUM_JS  # grounded read of the live node


# ─────────────────────── P3 rule oracles (invariants) ──────────────────────

def test_bound_invariant_emits_hard_nxcmp():
    lines, uses = rule_assertion_lines([
        {"kind": "bound", "field": "coverage", "op": "<=", "limit": 2000000, "source_hint": ".cov"},
    ])
    assert uses is True
    assert "await __nxCmp(page.locator('.cov'), '<=', 2000000.0);" in lines[0]
    assert "PROVEN invariant" in lines[0]


def test_bound_op_alias_max_min():
    lines, _ = rule_assertion_lines([
        {"kind": "bound", "field": "premium", "max": 999, "source_hint": "#p"},
        {"kind": "bound", "field": "premium", "min": 1, "source_hint": "#p"},
    ])
    assert "'<=', 999.0" in lines[0]
    assert "'>=', 1.0" in lines[1]


def test_positive_premium_greater_than_zero():
    lines, uses = rule_assertion_lines([
        {"kind": "bound", "field": "premium", "op": "gt", "limit": 0, "source_hint": ".prem"},
    ])
    assert uses and "await __nxCmp(page.locator('.prem'), '>', 0.0);" in lines[0]


def test_relational_kind_is_unverified_not_silent():
    lines, uses = rule_assertion_lines([
        {"kind": "monotonic", "field": "premium", "statement": "premium rises with coverage"},
    ])
    assert uses is False
    assert lines[0].strip().startswith("// UNVERIFIED invariant:")
    assert "multi-persona" in lines[0]
    assert "await" not in lines[0]  # never a silent assertion


def test_bound_without_grounding_is_unverified():
    lines, uses = rule_assertion_lines([{"kind": "bound", "field": "coverage", "op": "<=", "limit": 2000000}])
    assert uses is False
    assert "UNVERIFIED invariant" in lines[0] and "no source_hint" in lines[0]


def test_bound_without_operator_or_numeric_is_unverified():
    l1, _ = rule_assertion_lines([{"kind": "bound", "field": "x", "limit": 5, "source_hint": "#x"}])
    assert "no comparison operator" in l1[0]
    l2, _ = rule_assertion_lines([{"kind": "bound", "field": "x", "op": "<=", "limit": "abc", "source_hint": "#x"}])
    assert "no numeric bound" in l2[0]
    # "NaN" parses to a float via float() but is a nonsensical bound → must be UNVERIFIED
    l3, u3 = rule_assertion_lines([{"kind": "bound", "field": "x", "op": "<=", "limit": "NaN", "source_hint": "#x"}])
    assert u3 is False and "no numeric bound" in l3[0]


def test_rule_tolerant_on_garbage():
    assert rule_assertion_lines([]) == ([], False)
    assert rule_assertion_lines([None, "x", 7]) == ([], False)


def test_nxcmp_js_carries_unexpected_value_phrase():
    assert "unexpected value" in NXCMP_JS and "innerText()" in NXCMP_JS
