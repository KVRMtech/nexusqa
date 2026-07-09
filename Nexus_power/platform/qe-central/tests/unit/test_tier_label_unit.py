"""QE-Central S4 — tier labeller unit tests (pure; no DB, no HTTP).

Pins the RENDERS-vs-BEHAVES truth table + the MIN-tier suite roll-up:
  * a grounded navigation OR outcome-region oracle ⇒ BEHAVES;
  * a grounded value/state/a11y-only case ⇒ RENDERS (grounded, but not
    behavioural — proves the page rendered, not that it behaved);
  * an ungrounded/empty/unproven case ⇒ RENDERS with empty evidence;
  * a navigation oracle flagged NOT grounded ⇒ RENDERS (grounded is required);
  * the suite label is the MIN tier — one RENDERS case drags the suite down, so
    a fill-only suite can never be gamed into "behavioral".
"""
from __future__ import annotations

from app.services import tier_label
from app.services.tier_label import (
    TIER_BEHAVES,
    TIER_RENDERS,
    TIER_UNLABELED,
    label_case,
    label_rtm,
    suite_tier,
)


def _case(test_id, assertions):
    return {"test_id": test_id, "name": test_id,
            "rows": [{"step_number": 1, "emitted_assertions": assertions}]}


def test_navigation_oracle_earns_behaves():
    c = _case("t", [{"code": "toHaveURL(/home/)", "oracle_kind": "navigation",
                     "grounded": True}])
    result = label_case(c)
    assert result["tier"] == TIER_BEHAVES
    assert result["evidence"]["oracle_kinds"] == ["navigation"]
    assert len(result["evidence"]["behavioral_assertions"]) == 1


def test_outcome_region_oracle_earns_behaves():
    c = _case("t", [{"code": "getByText('Success').toBeVisible()",
                     "oracle_kind": "outcome-region", "grounded": True}])
    assert label_case(c)["tier"] == TIER_BEHAVES


def test_grounded_but_non_behavioral_kinds_are_renders():
    for kind in ("value-oracle", "value-presence", "state", "a11y"):
        c = _case("t", [{"code": "x", "oracle_kind": kind, "grounded": True}])
        assert label_case(c)["tier"] == TIER_RENDERS, kind


def test_ungrounded_navigation_is_renders():
    # grounded is REQUIRED — a navigation oracle flagged ungrounded cannot behave.
    c = _case("t", [{"code": "toHaveURL(x)", "oracle_kind": "navigation",
                     "grounded": False}])
    assert label_case(c)["tier"] == TIER_RENDERS


def test_empty_and_unproven_case_is_renders_with_empty_evidence():
    c = {"test_id": "t", "name": "t", "rows": [{"step_number": 1,
                                                "emitted_assertions": []}]}
    result = label_case(c)
    assert result["tier"] == TIER_RENDERS
    assert result["evidence"]["behavioral_assertions"] == []
    assert result["evidence"]["oracle_kinds"] == []


def test_suite_tier_is_min_fail_down():
    assert suite_tier([TIER_BEHAVES, TIER_RENDERS]) == TIER_RENDERS
    assert suite_tier([TIER_BEHAVES, TIER_BEHAVES]) == TIER_BEHAVES
    assert suite_tier([TIER_RENDERS]) == TIER_RENDERS
    assert suite_tier([]) == TIER_UNLABELED
    assert suite_tier(["garbage"]) == TIER_UNLABELED


def test_label_rtm_counts_and_min_suite():
    behaves = _case("nav", [{"code": "toHaveURL(x)", "oracle_kind": "navigation",
                             "grounded": True}])
    renders = _case("fill", [{"code": "toHaveValue(x)", "oracle_kind": "value-oracle",
                              "grounded": True}])
    out = label_rtm({"artifact_id": "a1", "tests": [behaves, renders]})
    assert out["artifact_id"] == "a1"
    assert out["counts"] == {TIER_BEHAVES: 1, TIER_RENDERS: 1, "total": 2}
    assert out["suite_tier"] == TIER_RENDERS


def test_label_rtm_all_behaves_suite_behaves():
    tests = [_case(f"t{i}", [{"code": "toHaveURL(x)", "oracle_kind": "navigation",
                              "grounded": True}]) for i in range(3)]
    out = label_rtm({"artifact_id": "a1", "tests": tests})
    assert out["suite_tier"] == TIER_BEHAVES
    assert out["counts"][TIER_BEHAVES] == 3


def test_no_cases_suite_is_unlabeled():
    out = label_rtm({"artifact_id": "a1", "tests": []})
    assert out["suite_tier"] == TIER_UNLABELED
    assert out["counts"]["total"] == 0


def test_multi_row_case_behaves_if_any_row_behaves():
    c = {"test_id": "t", "name": "t", "rows": [
        {"step_number": 1, "emitted_assertions": [
            {"code": "toHaveValue(x)", "oracle_kind": "value-oracle", "grounded": True}]},
        {"step_number": 2, "emitted_assertions": [
            {"code": "toHaveURL(x)", "oracle_kind": "navigation", "grounded": True}]},
    ]}
    assert label_case(c)["tier"] == TIER_BEHAVES


def test_behavioral_oracle_kinds_are_exactly_navigation_and_outcome_region():
    assert tier_label.BEHAVIORAL_ORACLE_KINDS == {"navigation", "outcome-region"}
