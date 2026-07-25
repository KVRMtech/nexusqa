"""Phase R1 — Execution Evidence Report core: the status state machine, the
D4 count triplet, evidence classes, flow derivation, and the HTML renderer.

Spec: QECentral/docs/EXECUTION_EVIDENCE_REPORT_SPEC.md §1, §2.0-§2.4, §2.15.

The doctrine these pin (never-green-wash, rendered):
  * a `failed` step can NEVER become green, and without a proven attribution it
    goes to Needs Review — never silently blamed on the customer's application;
  * a `skipped` step after a failure is BLOCKED (a consequence), never a pass;
  * OUR script defect is an Execution Error, never an application defect;
  * every rollup carries the FULL 7-bucket breakdown (no lone green badge).

Run from Nexus_power/platform/api:
    python -m pytest tests/test_evidence_report_r1.py -q
"""
from __future__ import annotations

from app.services.test_factory import attribution_engine as ae
from app.services.test_factory import evidence_report as er
from app.services.test_factory import report_html


# ── §1.1 step state machine ──────────────────────────────────────────────────

def test_passed_step_is_passed():
    assert er.derive_step_status("passed", None) == (er.ST_PASSED, "")


def test_failed_without_attribution_is_needs_review_never_app_blame():
    st, badge = er.derive_step_status("failed", None)
    assert st == er.ST_NEEDS_REVIEW and badge == "unattributed"
    st, _ = er.derive_step_status("failed", {"category": ae.CATEGORY_UNKNOWN})
    assert st == er.ST_NEEDS_REVIEW


def test_known_cause_but_unproven_blame_is_distinguished_from_silence():
    """A recognised failure SHAPE with no provable owner is a sharper review
    item than 'nothing matched' — conflating them reads as vagueness."""
    st, badge = er.derive_step_status(
        "failed", {"category": ae.CATEGORY_UNKNOWN, "cause": "action_locator_timeout"})
    assert st == er.ST_NEEDS_REVIEW and badge == "cause_known_blame_unproven"


def test_application_defect_is_the_only_defect_found():
    st, badge = er.derive_step_status("failed", {"category": ae.CATEGORY_APPLICATION})
    assert st == er.ST_DEFECT and badge == "application"


def test_our_script_defect_is_an_execution_error_not_a_product_defect():
    """Standing doctrine: an automation fault is NEVER the customer's defect."""
    st, badge = er.derive_step_status("failed", {"category": ae.CATEGORY_PRODUCT})
    assert st == er.ST_EXEC_ERROR and badge == "script"
    for cat, want in ((ae.CATEGORY_ENVIRONMENT, "environment"),
                      (ae.CATEGORY_CONFIG, "configuration"),
                      (ae.CATEGORY_DATA, "test_data")):
        st, badge = er.derive_step_status("failed", {"category": cat})
        assert st == er.ST_EXEC_ERROR and badge == want


def test_no_failed_status_can_ever_be_green():
    for attribution in (None, {"category": ae.CATEGORY_APPLICATION},
                        {"category": ae.CATEGORY_PRODUCT},
                        {"category": ae.CATEGORY_ENVIRONMENT},
                        {"category": "something-new"}):
        st, _ = er.derive_step_status("failed", attribution)
        assert st != er.ST_PASSED


def test_skipped_after_failure_is_blocked_not_skipped():
    assert er.derive_step_status("skipped", None, after_failure=True)[0] == er.ST_BLOCKED
    assert er.derive_step_status("skipped", None, after_failure=False)[0] == er.ST_SKIPPED


def test_unknown_db_status_fails_closed_to_review():
    assert er.derive_step_status("", None)[0] == er.ST_NEEDS_REVIEW
    assert er.derive_step_status("weird", None)[0] == er.ST_NEEDS_REVIEW


# ── §1.2 case rollup ─────────────────────────────────────────────────────────

def test_case_rollup_precedence():
    P, D, E, R, B, S = (er.ST_PASSED, er.ST_DEFECT, er.ST_EXEC_ERROR,
                        er.ST_NEEDS_REVIEW, er.ST_BLOCKED, er.ST_SKIPPED)
    # defect wins over everything, and a completed run is "Completed with Defects"
    assert er.derive_case_status([P, D, E, R]) == er.ST_COMPLETED_WITH_DEFECTS
    assert er.derive_case_status([P, D], reached_final_step=False) == er.ST_DEFECT_HALTED
    assert er.derive_case_status([P, E, R, B]) == er.ST_EXEC_ERROR
    assert er.derive_case_status([P, R, B]) == er.ST_NEEDS_REVIEW
    assert er.derive_case_status([P, B]) == er.ST_BLOCKED
    assert er.derive_case_status([P, P]) == er.ST_PASSED
    assert er.derive_case_status([S, S]) == er.ST_SKIPPED


def test_all_passed_only_when_every_step_passed():
    assert er.derive_case_status([er.ST_PASSED] * 21) == er.ST_PASSED
    # one skipped step means the case is NOT reported as a clean pass
    assert er.derive_case_status([er.ST_PASSED] * 20 + [er.ST_SKIPPED]) != er.ST_PASSED


def test_unexecuted_case_is_not_executed_never_passed():
    assert er.derive_case_status([], executed=False) == er.ST_NOT_EXECUTED
    assert er.derive_case_status([er.ST_PASSED], executed=False) == er.ST_NOT_EXECUTED


def test_empty_executed_case_is_cancelled():
    assert er.derive_case_status([], executed=True) == er.ST_CANCELLED


# ── D4 count triplet ─────────────────────────────────────────────────────────

def test_triplet_always_carries_every_bucket_including_zeros():
    c = er.count_triplet([er.ST_PASSED] * 3)
    for k in er.TRIPLET_KEYS:
        assert k in c, f"bucket {k} must always be present (D4)"
    assert c[er.ST_PASSED] == 3 and c[er.ST_SKIPPED] == 0
    assert c["total"] == 3


def test_triplet_folds_case_defect_statuses_into_defect_bucket():
    c = er.count_triplet([er.ST_COMPLETED_WITH_DEFECTS, er.ST_DEFECT_HALTED])
    assert c[er.ST_DEFECT] == 2


def test_triplet_counts_not_executed_separately_never_as_passed():
    c = er.count_triplet([er.ST_PASSED, er.ST_NOT_EXECUTED, er.ST_NOT_EXECUTED])
    assert c[er.ST_PASSED] == 1
    assert c[er.ST_NOT_EXECUTED] == 2


# ── D1 evidence class (no fabricated confidence) ─────────────────────────────

def test_evidence_class_from_recorded_provenance_only():
    assert er.evidence_class({"provenance": "demonstrated"}, "passed") == er.EV_PROVEN
    assert er.evidence_class({"provenance": "heuristic"}, "passed") == er.EV_INFERRED
    assert er.evidence_class({}, "passed") == er.EV_UNVERIFIED
    # a failed step proves nothing about its expectation
    assert er.evidence_class({"provenance": "demonstrated"}, "failed") == er.EV_UNVERIFIED


# ── flow derivation (generic across apps) ────────────────────────────────────

def test_flow_from_quoted_token_in_generated_name():
    key, label = er.derive_flow("Verify user can complete the 'apply' flow", None)
    assert key == "apply" and label == "apply"


def test_flow_falls_back_to_recorded_entry_path():
    key, _ = er.derive_flow("Some case with no quotes",
                            {"observed": {"url": "https://x.example/portal/checkout?step=1"}})
    assert key == "checkout"


def test_flow_derivation_carries_no_domain_vocabulary():
    """Works on any app — the signal is the recorded URL/name, not a word list."""
    key, _ = er.derive_flow("", {"observed": {"url": "https://shop.test/cart"}})
    assert key == "cart"
    assert er.derive_flow("", None)[0] == "unassigned"


# ── HTML renderer ────────────────────────────────────────────────────────────

def _mini_report() -> dict:
    steps = [{"step_number": 1, "status": er.ST_PASSED, "status_badge": "",
              "action": "Open /apply", "target": "url=/apply",
              "expected": "The apply page is displayed", "actual": "as expected",
              "duration_ms": 12, "evidence_class": er.EV_PROVEN,
              "oracle_provenance": {"scene_id": "s1", "recorded_provenance": "demonstrated"},
              "evidence": {"screenshot_url": ""}, "analysis": None}]
    case = {"test_case_id": "tc1", "name": "Verify user can complete the 'apply' flow",
            "description": "d", "test_type": "functional", "priority": "P0_critical",
            "status": er.ST_PASSED, "executed": True, "steps_declared": 1,
            "steps_executed": 1, "counts": er.count_triplet([er.ST_PASSED]),
            "duration_ms": 12, "tags": ["demonstrated"],
            "reproducibility": {"generator_version": "v1"}, "steps": steps}
    flow = {"flow_key": "apply", "flow_label": "apply", "cases": [case],
            "case_count": 1, "duration_ms": 12, "pass_percentage": 100.0,
            "defect_count": 0, "counts": er.count_triplet([er.ST_PASSED])}
    return {"report_version": "1.0", "generated_at": "2026-07-25T00:00:00+00:00",
            "run": {"run_id": "r1", "environment": "certification", "duration_ms": 12,
                    "started_at": "2026-07-25T00:00:00+00:00"},
            "trust": {"statement": "certified first", "certified": True,
                      "certification_run": {"run_id": "c1", "total_steps": 1,
                                            "passed_steps": 1, "failed_steps": 0,
                                            "skipped_steps": 0},
                      "quarantined": [], "quarantined_count": 0,
                      "uncertified_exploratory": [], "uncertified_exploratory_count": 0,
                      "suite_size": 1, "oracle_scorecard": None},
            "summary": {"artifact_id": "a1", "total_flows": 1,
                        "total_cases_generated": 1, "total_cases_executed": 1,
                        "total_steps_executed": 1,
                        "case_counts": er.count_triplet([er.ST_PASSED]),
                        "step_counts": er.count_triplet([er.ST_PASSED])},
            "flows": [flow],
            "coverage": {"note": "n", "cases_not_executed": [],
                         "cases_not_executed_count": 0, "quarantined_count": 0,
                         "uncertified_exploratory_count": 0}}


def test_html_is_self_contained_and_offline():
    out = report_html.render_html(_mini_report())
    assert out.startswith("<!doctype html>")
    for forbidden in ("http://", "https://cdn", "<script src", "@import"):
        assert forbidden not in out, f"report must not fetch {forbidden}"


def test_html_renders_trust_block_first_then_summary():
    out = report_html.render_html(_mini_report())
    assert out.index("Trust Block") < out.index("Execution Summary")


def test_html_always_prints_every_bucket_including_zeros():
    out = report_html.render_html(_mini_report())
    for label in ("Passed:", "Defect Found:", "Execution Error:", "Blocked:",
                  "Needs Review:", "Skipped:", "Cancelled:"):
        assert label in out, f"D4 requires the {label} bucket to be visible"


def test_html_marks_ai_analysis_as_suggested():
    rep = _mini_report()
    rep["flows"][0]["cases"][0]["steps"][0].update(
        status=er.ST_DEFECT,
        analysis={"category": ae.CATEGORY_APPLICATION, "tier": "candidate",
                  "cause": "validation_missing", "detail": "d",
                  "evidence_quoted": ["expect(received).toBe(...)"], "suggested": True})
    out = report_html.render_html(rep)
    assert "AI-suggested analysis" in out and "pending human confirmation" in out
