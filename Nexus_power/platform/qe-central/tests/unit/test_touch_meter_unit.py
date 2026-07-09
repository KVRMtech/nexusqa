"""QE-Central S4 — human-touch meter unit tests (pure; no DB).

Pins the honesty-critical logic:
  * the deterministic audit→touch classifier SKIPS the QE-Central service actor
    (a machine mutation is never a human touch) and non-sign-off actions (a run
    trigger like /auto-heal/run-config, a /generate), and MAPS the real human
    sign-off endpoints (case review, version/heal approval);
  * the per-band autonomy math: no denominator ⇒ None (never a misleading 0);
    touches clamped so autonomy is never negative; the exact %;
  * the touch-type vocabulary is the nine typed touches;
  * a bad touch_type is a typed 422 error BEFORE any write.
"""
from __future__ import annotations

import asyncio

import pytest

from app.services import touch_meter
from app.services.touch_meter import (
    SERVICE_ACTORS,
    TOUCH_CASE_REVIEW,
    TOUCH_HEAL_APPROVE,
    TOUCH_TYPES,
    InvalidTouchTypeError,
    _autonomy_pct,
    _validate_touch_type,
    classify_audit_action,
)


# ── The audit → touch classifier (marker table) ──────────────────────────

def test_service_actor_is_never_a_human_touch():
    # The factory calls S4 itself makes must not deflate autonomy.
    for actor in SERVICE_ACTORS:
        assert classify_audit_action(
            "POST /api/v1/test-factory/a/test-cases/c/review", actor) is None


def test_case_review_maps():
    assert classify_audit_action(
        "POST /api/v1/test-factory/a1/test-cases/c1/review", "user-42",
    ) == TOUCH_CASE_REVIEW


def test_version_approve_maps_to_heal_approve():
    assert classify_audit_action(
        "POST /api/v1/test-factory/a1/scripts/t1/versions/3/approve", "user-42",
    ) == TOUCH_HEAL_APPROVE


def test_manual_step_heal_maps_to_heal_approve():
    assert classify_audit_action(
        "POST /api/v1/test-factory/a1/steps/s1/2/heal", "user-42",
    ) == TOUCH_HEAL_APPROVE


def test_run_config_trigger_is_not_a_touch():
    # A run trigger is autonomous work, NOT a heal approval.
    assert classify_audit_action(
        "POST /api/v1/test-factory/a1/auto-heal/run-config", "user-42") is None


def test_generate_is_not_a_touch():
    assert classify_audit_action(
        "POST /api/v1/test-factory/a1/generate", "user-42") is None


def test_unrelated_action_is_skipped():
    assert classify_audit_action("GET /api/v1/test-factory/a1/rtm", "user-42") is None
    assert classify_audit_action("", "user-42") is None


# ── The per-band autonomy math ────────────────────────────────────────────

def test_autonomy_pct_none_without_denominator():
    # No governed scenarios ⇒ not computable (honest None, never a fake 0).
    assert _autonomy_pct(0, 0) is None
    assert _autonomy_pct(0, 5) is None


def test_autonomy_pct_exact():
    assert _autonomy_pct(4, 1) == 75.0
    assert _autonomy_pct(4, 0) == 100.0
    assert _autonomy_pct(2, 2) == 0.0


def test_autonomy_pct_clamps_touches_to_governed():
    # More touches than governed units ⇒ clamped to 0, never negative.
    assert _autonomy_pct(2, 5) == 0.0


# ── Vocabulary + validation ───────────────────────────────────────────────

def test_touch_type_vocabulary_is_the_nine():
    assert TOUCH_TYPES == {
        "scenario_approve", "invariant_author", "gap_adjudicate", "waiver_create",
        "credential_provision", "answer_key_edit", "heal_approve", "case_review",
        "defect_confirm",
    }


def test_validate_touch_type_accepts_known():
    assert _validate_touch_type("scenario_approve") == "scenario_approve"


def test_validate_touch_type_rejects_unknown():
    with pytest.raises(InvalidTouchTypeError) as ei:
        _validate_touch_type("not_a_touch")
    assert getattr(ei.value, "http_status", None) == 422


def test_record_touch_rejects_bad_type_before_any_db_write():
    # The type gate fires before the DB is touched — no event loop DB needed.
    with pytest.raises(InvalidTouchTypeError):
        asyncio.run(touch_meter.record_touch(
            tenant_id="t1", touch_type="bogus", band="P0"))
