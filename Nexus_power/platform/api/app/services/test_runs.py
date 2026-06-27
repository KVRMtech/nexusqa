"""Execution Feedback service (P6) — pure logic over test_run + test_run_step.

Public surface:
    extract_scenario_id_from_test_name(name) → str | None
    ingest_run(db, artifact_id, tenant_id, payload) → dict  # run summary
    last_run_summary_by_scenario(db, artifact_id, tenant_id) → dict
    detect_flake(history) → FlakeReport
    detect_drift(step_row, control) → DriftReport
    root_cause_hints(failing_step, last_passing_step, current_control,
                     current_scene) → list[str]

All functions are tenant-scoped; pass tenant_id explicitly.

NO LLM is invoked from this module — root-cause hints are deterministic
diffs between the failing run, the last passing run, and the live visual
graph state.  If we can't produce a hint we say so explicitly instead of
fabricating one.
"""
from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import HTTPException
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from nexus_sdk.db.models import (
    E2ETestRunRow,
    E2ETestRunStepRow,
    E2E_RUN_STATUS_PASSED,
    E2E_RUN_STATUS_FAILED,
    E2E_RUN_STATUS_ERROR,
    E2E_RUN_TERMINAL_STATUSES,
    E2E_STEP_STATUS_PASSED,
    E2E_STEP_STATUS_FAILED,
    E2E_STEP_STATUS_SKIPPED,
    E2E_STEP_STATUS_BROKEN,
    E2E_STEP_STATUS_TIMED_OUT,
)

_logger = logging.getLogger(__name__)


# ─── Scenario-id extraction ──────────────────────────────────────────────
#
# Playwright + Cypress exporters embed the scenario id at the start of test
# titles, e.g.  test('vs_001 — Happy path: ...').  This regex pulls it back
# out of CI's reported test name.  It also accepts the legacy formats
# "[vs_001]" and "vs_001:" so an old report doesn't break ingest.

_SCENARIO_ID_RE = re.compile(
    r"\b(?P<sid>(?:vs|co)_\d{3,6})\b",
)


def extract_scenario_id_from_test_name(name: str | None) -> str | None:
    """Pull a scenario_id (``vs_NNN`` or ``co_NNN``) out of a free-form test
    name.  Returns None when no match is found — the ingest endpoint then
    persists the step with scenario_id="" so the row isn't lost."""
    if not name:
        return None
    m = _SCENARIO_ID_RE.search(name)
    return m.group("sid") if m else None


# ─── Helpers ─────────────────────────────────────────────────────────────


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return str(uuid.uuid4())


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            # Accept ISO 8601 with or without trailing Z
            cleaned = value.rstrip("Z")
            if not cleaned.endswith("+00:00") and "+" not in cleaned[10:]:
                cleaned = cleaned + "+00:00"
            return datetime.fromisoformat(cleaned)
        except ValueError:
            return None
    return None


def _row_to_dict(row: Any) -> dict:
    cols = row.__table__.columns.keys()
    out: dict[str, Any] = {}
    for col in cols:
        val = getattr(row, col)
        if isinstance(val, datetime):
            out[col] = val.isoformat()
        else:
            out[col] = val
    return out


# ─── Ingest ──────────────────────────────────────────────────────────────


async def ingest_run(
    db: AsyncSession,
    *,
    artifact_id: str,
    tenant_id: str,
    payload: dict,
) -> dict:
    """Persist a CI test run + its steps. Returns the run summary.

    Expected payload shape:
        {
          "ci_run_id": "<external id>",
          "ci_commit_sha": "...",
          "ci_pipeline_url": "...",
          "environment": "ci",
          "started_at": "ISO",
          "completed_at": "ISO",
          "status": "passed" | "failed" | "error",
          "steps": [
              {
                "test_name": "vs_001 — Happy path: ...",  # OR
                "scenario_id": "vs_001",                  # OR both
                "step_number": 1,
                "status": "passed" | "failed" | "skipped" | "timed_out" | "broken",
                "duration_ms": 1234,
                "expected_selector": "...",
                "resolved_selector": "...",
                "resolved_bbox": {"x": 100, "y": 200, "width": 80, "height": 30},
                "evidence_scene_id": "...",
                "evidence_control_id": "...",
                "evidence_edge_id": "...",
                "error_message": "...",
                "screenshot_url": "..."
              }
          ]
        }
    """
    steps_in = payload.get("steps")
    if not isinstance(steps_in, list) or not steps_in:
        raise HTTPException(422, "payload.steps must be a non-empty list")

    # Aggregates computed from steps; trust the steps over any top-level field
    passed = failed = skipped = total_duration = 0
    for s in steps_in:
        status = (s.get("status") or E2E_STEP_STATUS_PASSED).lower()
        total_duration += int(s.get("duration_ms") or 0)
        if status == E2E_STEP_STATUS_PASSED:
            passed += 1
        elif status == E2E_STEP_STATUS_SKIPPED:
            skipped += 1
        else:
            failed += 1
    total = len(steps_in)

    # Top-level status: caller's value wins if valid; otherwise derive
    declared_status = (payload.get("status") or "").lower()
    if declared_status in E2E_RUN_TERMINAL_STATUSES:
        run_status = declared_status
    elif failed > 0:
        run_status = E2E_RUN_STATUS_FAILED
    elif passed > 0:
        run_status = E2E_RUN_STATUS_PASSED
    else:
        # All-skipped runs are not pass/fail; report as 'error' so the UI
        # surfaces them rather than silently treating as success.
        run_status = E2E_RUN_STATUS_ERROR

    started_at = _parse_iso(payload.get("started_at")) or _utc_now()
    completed_at = _parse_iso(payload.get("completed_at"))

    run = E2ETestRunRow(
        run_id=_new_id(),
        artifact_id=artifact_id,
        tenant_id=tenant_id,
        ci_run_id=str(payload.get("ci_run_id") or "")[:200],
        ci_commit_sha=str(payload.get("ci_commit_sha") or "")[:64],
        ci_pipeline_url=str(payload.get("ci_pipeline_url") or "")[:1000],
        environment=str(payload.get("environment") or "ci")[:64],
        status=run_status,
        total_steps=total,
        passed_steps=passed,
        failed_steps=failed,
        skipped_steps=skipped,
        duration_ms=total_duration,
        started_at=started_at,
        completed_at=completed_at,
        ingested_at=_utc_now(),
        metadata_json=dict(payload.get("metadata") or {}),
    )
    db.add(run)
    await db.flush()  # populate FK

    parse_misses = 0
    for s in steps_in:
        scenario_id = (
            s.get("scenario_id")
            or extract_scenario_id_from_test_name(s.get("test_name"))
            or ""
        )
        if not scenario_id:
            parse_misses += 1
        step_status = (s.get("status") or E2E_STEP_STATUS_PASSED).lower()
        if step_status not in (
            E2E_STEP_STATUS_PASSED, E2E_STEP_STATUS_FAILED, E2E_STEP_STATUS_SKIPPED,
            E2E_STEP_STATUS_TIMED_OUT, E2E_STEP_STATUS_BROKEN,
        ):
            step_status = E2E_STEP_STATUS_BROKEN

        step_row = E2ETestRunStepRow(
            step_run_id=_new_id(),
            run_id=run.run_id,
            artifact_id=artifact_id,
            tenant_id=tenant_id,
            scenario_id=scenario_id,
            step_number=int(s.get("step_number") or 0),
            status=step_status,
            duration_ms=int(s.get("duration_ms") or 0),
            expected_selector=str(s.get("expected_selector") or "")[:1000],
            resolved_selector=str(s.get("resolved_selector") or "")[:1000],
            resolved_bbox_json=dict(s.get("resolved_bbox") or {}),
            evidence_scene_id=str(s.get("evidence_scene_id") or "")[:64],
            evidence_control_id=str(s.get("evidence_control_id") or "")[:64],
            evidence_edge_id=str(s.get("evidence_edge_id") or "")[:64],
            error_message=str(s.get("error_message") or "")[:8000],
            screenshot_url=str(s.get("screenshot_url") or "")[:2000],
            metadata_json=dict(s.get("metadata") or {}),
            created_at=_utc_now(),
        )
        db.add(step_row)

    await db.commit()
    _logger.info(
        "test_runs.ingested artifact=%s run=%s steps=%d (passed=%d, failed=%d, "
        "skipped=%d) unparsed_scenarios=%d",
        artifact_id, run.run_id, total, passed, failed, skipped, parse_misses,
    )

    summary = _row_to_dict(run)
    summary["unparsed_scenarios"] = parse_misses
    return summary


# ─── Last-run aggregation ────────────────────────────────────────────────


async def last_run_summary_by_scenario(
    db: AsyncSession,
    *,
    artifact_id: str,
    tenant_id: str,
    flake_window_runs: int = 10,
) -> dict[str, dict]:
    """Build a per-scenario summary of the most-recent run plus a flake
    fingerprint computed from the last ``flake_window_runs`` runs.

    Returns:
        {scenario_id: {
            "last_run_id": str,
            "last_run_status": str,      # passed|failed|...|skipped|broken|timed_out
            "last_run_at": iso,
            "last_duration_ms": int,
            "last_error_message": str,
            "flake_rate_pct": float,     # 0..100
            "is_flaky": bool,
            "consecutive_failures": int,
            "selector_drift_observed": bool,
        }, ...}

    Scenarios with no recorded runs aren't included in the map — callers
    treat missing entries as "never run".
    """
    # Fetch all steps for the artifact, ordered most-recent first.
    # Then group in-memory; this is O(N) and keeps the query simple.
    result = await db.execute(
        select(E2ETestRunStepRow, E2ETestRunRow)
        .join(E2ETestRunRow, E2ETestRunStepRow.run_id == E2ETestRunRow.run_id)
        .where(
            E2ETestRunStepRow.artifact_id == artifact_id,
            E2ETestRunStepRow.tenant_id == tenant_id,
            E2ETestRunStepRow.scenario_id != "",
        )
        .order_by(desc(E2ETestRunRow.started_at))
    )
    pairs = result.all()
    if not pairs:
        return {}

    # Bucket step rows by scenario_id, preserving descending-time order
    by_scenario: dict[str, list[tuple[E2ETestRunStepRow, E2ETestRunRow]]] = {}
    for step_row, run_row in pairs:
        by_scenario.setdefault(step_row.scenario_id, []).append((step_row, run_row))

    summary: dict[str, dict] = {}
    for scenario_id, pairs_list in by_scenario.items():
        # Per-scenario status per RUN (worst step status wins for the run)
        run_statuses_by_run: dict[str, dict] = {}
        for step_row, run_row in pairs_list:
            entry = run_statuses_by_run.setdefault(run_row.run_id, {
                "run": run_row,
                "worst_status": E2E_STEP_STATUS_PASSED,
                "steps": [],
            })
            entry["steps"].append(step_row)
            if _status_severity(step_row.status) > _status_severity(entry["worst_status"]):
                entry["worst_status"] = step_row.status

        # Order runs descending by started_at
        runs_desc = sorted(
            run_statuses_by_run.values(),
            key=lambda r: r["run"].started_at,
            reverse=True,
        )
        latest = runs_desc[0]
        latest_run: E2ETestRunRow = latest["run"]
        latest_steps: list[E2ETestRunStepRow] = latest["steps"]
        latest_error = next(
            (s.error_message for s in latest_steps if s.status == E2E_STEP_STATUS_FAILED),
            "",
        )
        latest_duration = sum(s.duration_ms for s in latest_steps)
        selector_drift = any(
            s.expected_selector and s.resolved_selector
            and s.expected_selector != s.resolved_selector
            for s in latest_steps
        )

        flake_window = runs_desc[:flake_window_runs]
        flake = _flake_from_window(flake_window)

        summary[scenario_id] = {
            "last_run_id": latest_run.run_id,
            "last_run_status": latest["worst_status"],
            "last_run_at": latest_run.started_at.isoformat() if latest_run.started_at else None,
            "last_duration_ms": latest_duration,
            "last_error_message": latest_error or "",
            "ci_commit_sha": latest_run.ci_commit_sha or "",
            "ci_pipeline_url": latest_run.ci_pipeline_url or "",
            "flake_rate_pct": flake["flake_rate_pct"],
            "is_flaky": flake["is_flaky"],
            "consecutive_failures": flake["consecutive_failures"],
            "runs_in_window": len(flake_window),
            "selector_drift_observed": selector_drift,
        }
    return summary


def _status_severity(status: str) -> int:
    """Order step statuses worst-to-best so the run-level rollup picks the
    worst step.  Used by ``last_run_summary_by_scenario``."""
    order = {
        E2E_STEP_STATUS_FAILED: 4,
        E2E_STEP_STATUS_BROKEN: 4,
        E2E_STEP_STATUS_TIMED_OUT: 3,
        E2E_STEP_STATUS_SKIPPED: 1,
        E2E_STEP_STATUS_PASSED: 0,
    }
    return order.get(status, 0)


# ─── Flake detection ─────────────────────────────────────────────────────


@dataclass
class FlakeReport:
    flake_rate_pct: float
    is_flaky: bool
    consecutive_failures: int


def _flake_from_window(runs_desc: list[dict]) -> dict:
    """Compute flake stats from a window of runs (descending by time).

    A scenario is flaky when its status changes pass↔fail at least twice
    in the window.  ``consecutive_failures`` counts the leading failure
    streak (most-recent run first).
    """
    if not runs_desc:
        return {
            "flake_rate_pct": 0.0,
            "is_flaky": False,
            "consecutive_failures": 0,
        }

    statuses = [r["worst_status"] for r in runs_desc]
    # Reduce to pass/fail buckets
    buckets = [
        "fail" if _status_severity(s) >= _status_severity(E2E_STEP_STATUS_TIMED_OUT)
        else "pass"
        for s in statuses
    ]
    transitions = sum(1 for i in range(1, len(buckets)) if buckets[i] != buckets[i - 1])
    fail_count = sum(1 for b in buckets if b == "fail")
    flake_rate_pct = round(100.0 * fail_count / len(buckets), 1) if buckets else 0.0
    is_flaky = transitions >= 2 and fail_count != len(buckets) and fail_count != 0

    consecutive_failures = 0
    for b in buckets:
        if b == "fail":
            consecutive_failures += 1
        else:
            break

    return {
        "flake_rate_pct": flake_rate_pct,
        "is_flaky": is_flaky,
        "consecutive_failures": consecutive_failures,
    }


def detect_flake_from_pass_fail_sequence(sequence: list[str]) -> FlakeReport:
    """Public helper used by the smoke test. ``sequence`` is a list of
    'pass' / 'fail' strings, oldest first."""
    runs_desc = [
        {"worst_status": E2E_STEP_STATUS_PASSED if s == "pass" else E2E_STEP_STATUS_FAILED}
        for s in reversed(sequence)
    ]
    f = _flake_from_window(runs_desc)
    return FlakeReport(
        flake_rate_pct=f["flake_rate_pct"],
        is_flaky=f["is_flaky"],
        consecutive_failures=f["consecutive_failures"],
    )


# ─── Selector drift detection ────────────────────────────────────────────


@dataclass
class DriftReport:
    selector_drifted: bool
    bbox_drifted: bool
    selector_diff: str  # "expected ... | resolved ..." or ""
    bbox_pixel_distance: float | None


def detect_drift(
    *,
    expected_selector: str,
    resolved_selector: str,
    expected_bbox: dict | None,
    resolved_bbox: dict | None,
) -> DriftReport:
    """Compare a step's recorded resolved selector + bounding box against
    what the visual evidence graph currently expects.

    Selector drift = expected_selector and resolved_selector both present
    and differ.  bbox drift = both bounding boxes present and their
    centers are more than 50px apart OR more than 50% area change.
    """
    selector_drifted = (
        bool(expected_selector)
        and bool(resolved_selector)
        and expected_selector != resolved_selector
    )
    selector_diff = (
        f"expected={expected_selector!r} | resolved={resolved_selector!r}"
        if selector_drifted else ""
    )

    bbox_drifted = False
    pixel_distance: float | None = None
    if expected_bbox and resolved_bbox:
        try:
            ex = float(expected_bbox.get("x", 0))
            ey = float(expected_bbox.get("y", 0))
            ew = float(expected_bbox.get("width", 0))
            eh = float(expected_bbox.get("height", 0))
            rx = float(resolved_bbox.get("x", 0))
            ry = float(resolved_bbox.get("y", 0))
            rw = float(resolved_bbox.get("width", 0))
            rh = float(resolved_bbox.get("height", 0))
            # Center-to-center distance
            ecx, ecy = ex + ew / 2, ey + eh / 2
            rcx, rcy = rx + rw / 2, ry + rh / 2
            pixel_distance = ((ecx - rcx) ** 2 + (ecy - rcy) ** 2) ** 0.5
            # Area change ratio (guard against zero)
            ea = max(ew * eh, 1.0)
            ra = max(rw * rh, 1.0)
            area_ratio = abs(ra - ea) / ea
            bbox_drifted = pixel_distance > 50.0 or area_ratio > 0.5
        except (TypeError, ValueError):
            pixel_distance = None
            bbox_drifted = False

    return DriftReport(
        selector_drifted=selector_drifted,
        bbox_drifted=bbox_drifted,
        selector_diff=selector_diff,
        bbox_pixel_distance=pixel_distance,
    )


# ─── Root-cause hints ────────────────────────────────────────────────────


def root_cause_hints(
    *,
    failing_step: dict | E2ETestRunStepRow,
    last_passing_step: Optional[dict | E2ETestRunStepRow],
    current_control: dict | None,
    current_scene: dict | None,
) -> list[str]:
    """Generate plain-English hints about why a step might be failing.

    Pure deterministic comparison — no LLM.  Returns an empty list when
    no meaningful difference can be found.  Caller renders these as
    bullet points under the failing scenario.
    """
    def g(obj: Any, key: str, default: Any = "") -> Any:
        if obj is None:
            return default
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)

    hints: list[str] = []

    failing_selector = g(failing_step, "resolved_selector") or g(failing_step, "expected_selector")
    expected_selector = g(failing_step, "expected_selector")

    # 1. Resolved selector ≠ expected selector
    if expected_selector and failing_selector and expected_selector != failing_selector:
        hints.append(
            f"Selector resolved to a different element than expected — "
            f"expected {expected_selector!r}, browser found {failing_selector!r}."
        )

    # 2. Last passing run resolved a different selector than the failing run
    if last_passing_step is not None:
        prev_resolved = g(last_passing_step, "resolved_selector") or g(last_passing_step, "expected_selector")
        if prev_resolved and failing_selector and prev_resolved != failing_selector:
            hints.append(
                f"Selector resolution changed since the last passing run "
                f"({prev_resolved!r} → {failing_selector!r})."
            )

    # 3. Current control's automation_ready / selector_confidence dropped
    if current_control is not None:
        sel_conf = current_control.get("selector_confidence")
        if isinstance(sel_conf, (int, float)) and sel_conf < 0.6:
            hints.append(
                f"Control's selector_confidence is now {sel_conf:.2f} — it dropped "
                "below the production threshold (0.85). The UI likely changed since "
                "the demo was recorded."
            )
        if current_control.get("automation_ready") is False:
            hints.append(
                "The control is no longer marked automation_ready in the visual "
                "graph (its selector lost its OCR backing)."
            )

    # 4. Current scene's OCR no longer contains the expected output snippet
    expected_output = g(failing_step, "expected_output") or ""
    if current_scene is not None and expected_output:
        ocr = (current_scene.get("ocr_text") or "").lower()
        # Compare a meaningful slice — first 30 chars of the assertion
        needle = expected_output[:30].lower().strip()
        if needle and needle not in ocr:
            hints.append(
                f"The destination scene's OCR no longer contains "
                f"{expected_output[:60]!r}. The assertion target text was "
                "removed or renamed in the UI."
            )

    # 5. Bounding-box drift
    expected_bbox: dict | None = None
    if current_control is not None:
        bbox_raw = current_control.get("bounding_box")
        if isinstance(bbox_raw, dict):
            expected_bbox = bbox_raw
    drift = detect_drift(
        expected_selector=expected_selector,
        resolved_selector=g(failing_step, "resolved_selector"),
        expected_bbox=expected_bbox,
        resolved_bbox=g(failing_step, "resolved_bbox_json") or g(failing_step, "resolved_bbox"),
    )
    if drift.bbox_drifted and drift.bbox_pixel_distance is not None:
        hints.append(
            f"The element moved on screen — its center is now "
            f"{drift.bbox_pixel_distance:.0f}px from where it was when the test was "
            "recorded. Likely a layout or redesign change."
        )

    return hints


# ─── Verdict reducer ─────────────────────────────────────────────────────
#
# Rolls the deterministic per-failure signals (drift, flake, outcome
# contradiction, locator resolution) into ONE conservative label per failure,
# so a red build reads "3 real / 9 drift / 2 flake — only 3 need you".
#
# Safety doctrine (non-negotiable): a failure is auto-dismissed (selector_drift
# / visual_change / flake) ONLY when there is POSITIVE evidence it is not a real
# regression — the flow still reaches the recorded outcome. Any outcome
# contradiction, or any unexplained red, is real_regression or needs_review,
# NEVER dimmed. Mis-bucketing a real bug into the "don't need you" stack is the
# one unacceptable error, so the reducer fails toward "needs_review".

VERDICT_PASSED = "passed"
VERDICT_REAL_REGRESSION = "real_regression"
VERDICT_SELECTOR_DRIFT = "selector_drift"
VERDICT_VISUAL_CHANGE = "visual_change"
VERDICT_FLAKE = "flake"
VERDICT_NEEDS_REVIEW = "needs_review"

# Verdicts safe to dim as "don't need a human right now".
VERDICT_DISMISSABLE = frozenset({
    VERDICT_SELECTOR_DRIFT, VERDICT_VISUAL_CHANGE, VERDICT_FLAKE,
})

_LOCATOR_MISSING_RE = re.compile(
    r"not found|resolved to 0|no element|strict mode violation|waiting for (?:locator|element)|"
    r"element is not (?:visible|attached)",
    re.IGNORECASE,
)

# A FAILED navigation-outcome assertion = the recorded next page was NOT reached.
# The compiler emits `toHaveURL(/recorded-path/)` from the OBSERVED next_url, so a
# failure naming `toHaveURL` means the action ran but the app produced a different
# result than the recording — the strongest, highest-precision real-regression
# signal. A locator-not-found / action timeout NEVER contains 'toHaveURL', so this
# stays conservative (it cannot fire on a drift/rename). Grounded, deterministic.
_OUTCOME_ASSERT_RE = re.compile(r"toHaveURL", re.IGNORECASE)

# On-page OUTCOME oracles beyond navigation. The compiler also emits grounded
# value/text assertions (toHaveValue / toHaveText / toContainText) from the
# recorded committed value and the recorded outcome-region text. A FAILED
# value/text assertion means the control RESOLVED but the application produced a
# DIFFERENT value/text than the recording — a real outcome contradiction (a wrong
# computed amount, a changed disclosure, a no-op fill), not a locator drift. This
# widens "proven" past navigation-only, so the verdict is grounded in BEHAVIOUR —
# not just the URL. (Was: toHaveURL only — the single-signal gap flagged in the
# Test-Evidence-SoR strategy.)
_OUTCOME_VALUE_ASSERT_RE = re.compile(
    r"toHaveValue|toHaveText|toContainText", re.IGNORECASE
)

# POSITIVE evidence that the control RESOLVED and the value/text actually DIFFERED
# (i.e. a real outcome contradiction, not a locator-not-found). Playwright prints
# the actual observed value only after the element resolved: "unexpected value
# \"…\"" (toHaveValue) or "Received string/array: …" (toHaveText/toContainText).
# A not-found timeout never reaches the comparison, so it carries NONE of these —
# which keeps a renamed/missing control as heal-able selector drift. NB: the raw
# call log of EVERY assertion contains "waiting for locator", so _LOCATOR_MISSING_RE
# cannot be used as the gate here (it would neuter a genuine value mismatch); the
# positive-evidence marker below is what disambiguates.
_VALUE_MISMATCH_EVIDENCE_RE = re.compile(
    r"unexpected value|Received string|Received array|Received:", re.IGNORECASE
)


def outcome_contradicted_from_error(error_message: str) -> bool:
    """True when the failure is a FAILED recorded-outcome assertion. Conservative
    by construction:

      * a failed navigation oracle (``toHaveURL``) is ALWAYS dispositive — it can
        never be a locator error; and
      * a failed on-page value/text oracle counts ONLY with POSITIVE evidence the
        element resolved and the value differed (an "unexpected value" / "Received"
        marker). A value/text assertion that failed because the control was
        renamed / not found carries no such marker, so it stays heal-able selector
        drift and is NEVER mis-escalated to a real regression.

    Labels real_regression on POSITIVE evidence only; otherwise leaves the reducer
    to fail toward needs_review."""
    msg = error_message or ""
    if _OUTCOME_ASSERT_RE.search(msg):
        return True
    if _OUTCOME_VALUE_ASSERT_RE.search(msg) and _VALUE_MISMATCH_EVIDENCE_RE.search(msg):
        return True
    return False


@dataclass
class Verdict:
    label: str
    confidence: float  # 0.0..1.0
    justification: str


def classify_failure(
    *,
    failed: bool,
    is_flaky: bool,
    selector_drifted: bool,
    bbox_drifted: bool,
    outcome_contradicted: bool,
    error_message: str = "",
) -> Verdict:
    """Deterministic, conservative verdict for one failing scenario.

    Inputs are signals the engine already computes:
      * outcome_contradicted — the recording's expected outcome is no longer on
        the page (root_cause_hints' OCR check) — the strongest real-bug signal.
      * selector_drifted / bbox_drifted — from detect_drift.
      * is_flaky — from detect_flake (>=2 pass<->fail transitions).
      * error_message — used only to detect locator-not-found.
    """
    if not failed:
        return Verdict(VERDICT_PASSED, 1.0, "Step passed.")

    # 1. The page produced something other than what the recording produced.
    if outcome_contradicted:
        return Verdict(
            VERDICT_REAL_REGRESSION, 0.9,
            "The expected outcome from the recording is no longer present on the "
            "page (the assertion target is gone) — a real regression, not a "
            "locator issue.",
        )

    locator_missing = bool(_LOCATOR_MISSING_RE.search(error_message or ""))

    # 2. Selector changed but the flow still reaches the recorded outcome.
    if selector_drifted and not locator_missing:
        return Verdict(
            VERDICT_SELECTOR_DRIFT, 0.85,
            "The control's selector changed since the recording (expected != "
            "resolved) but the flow still reaches the recorded outcome — cosmetic "
            "selector drift, not a broken flow.",
        )

    # 3. The element only moved on screen; still resolved, outcome intact.
    if bbox_drifted and not locator_missing:
        return Verdict(
            VERDICT_VISUAL_CHANGE, 0.7,
            "The element moved on screen but the flow still reaches the recorded "
            "outcome — a visual/layout change, not a broken flow.",
        )

    # 4. Alternates pass<->fail with no deterministic selector/outcome cause.
    if is_flaky and not selector_drifted and not bbox_drifted and not locator_missing:
        return Verdict(
            VERDICT_FLAKE, 0.75,
            "This scenario alternates pass/fail across runs with no selector or "
            "outcome change — environmental flake, not a code defect.",
        )

    # 5. Anything else fails toward a human — never auto-dismissed.
    if locator_missing:
        return Verdict(
            VERDICT_NEEDS_REVIEW, 0.5,
            "The locator could not be found and the flow's outcome cannot be "
            "proven intact — could be a rename or a real removal; needs a human.",
        )
    return Verdict(
        VERDICT_NEEDS_REVIEW, 0.4,
        "Failed with no detectable selector drift, outcome contradiction, or "
        "flake pattern — needs a human.",
    )


def tally_verdicts(verdicts: list[Verdict]) -> dict:
    """Board summary: counts per label + the 'need you / don't need you' split."""
    by_label: dict[str, int] = {}
    for v in verdicts:
        by_label[v.label] = by_label.get(v.label, 0) + 1
    failures = [v for v in verdicts if v.label != VERDICT_PASSED]
    need_you = sum(1 for v in failures if v.label not in VERDICT_DISMISSABLE)
    dont_need_you = sum(1 for v in failures if v.label in VERDICT_DISMISSABLE)
    return {
        "total": len(verdicts),
        "failures": len(failures),
        "need_you": need_you,
        "dont_need_you": dont_need_you,
        "by_label": by_label,
    }


# ─── Single-run per-step timeline (THIS RUN) ─────────────────────────────
#
# Powers the "This run — where it broke" view: for the SINGLE most-recent run,
# every scenario's every executed step with a pass/fail glyph, a human label
# (from the recorded ProductionTestCase action), and the failing step joined to
# its known-good baseline screenshot.  Because the header counts come straight
# off the run row and the scenario rows are that same run's steps, the header
# and the step rollup can never disagree — this is what fixes the "1 vs 6"
# contradiction (cross-run accumulation lives in the separate History view).
# Deterministic, $0 LLM, read-only, no migration.

_FAIL_STEP_STATUSES = frozenset({
    E2E_STEP_STATUS_FAILED, E2E_STEP_STATUS_BROKEN, E2E_STEP_STATUS_TIMED_OUT,
})


def _case_step(tc: Any, step_number: int | None) -> Any:
    """The recorded baseline step with this step_number — for its action label
    and known-good screenshot.  Returns None when there's no matching case step
    (e.g. an unproven step the runner skipped, or no baseline case at all)."""
    if tc is None or step_number is None:
        return None
    for st in (getattr(tc, "steps", None) or []):
        if getattr(st, "step_number", None) == step_number:
            return st
    return None


async def find_run_by_ci_run_id(
    db: AsyncSession, *, artifact_id: str, tenant_id: str, ci_run_id: str,
) -> str | None:
    """Resolve the ingested run for a specific reporter run id (the reporter sets
    ci_run_id = NEXUS_RUN_ID). Returns the run's PK (run_id) or None. Lets the heal
    verifier correlate to THE run it kicked off — never 'whatever ran last', which
    a racing ingest could otherwise make wrong."""
    if not ci_run_id:
        return None
    row = (await db.execute(
        select(E2ETestRunRow.run_id)
        .where(
            E2ETestRunRow.artifact_id == artifact_id,
            E2ETestRunRow.tenant_id == tenant_id,
            E2ETestRunRow.ci_run_id == ci_run_id,
        )
        .order_by(desc(E2ETestRunRow.started_at))
        .limit(1)
    )).scalar_one_or_none()
    return row


async def recent_runs(
    db: AsyncSession, *, artifact_id: str, tenant_id: str, limit: int = 25,
) -> list[dict]:
    """Recent run headers for an artifact, newest first — drives the run picker
    so any historical run can be re-inspected step-by-step. Read-only, $0 LLM."""
    limit = max(1, min(int(limit or 25), 200))
    rows = (await db.execute(
        select(E2ETestRunRow)
        .where(
            E2ETestRunRow.artifact_id == artifact_id,
            E2ETestRunRow.tenant_id == tenant_id,
        )
        .order_by(desc(E2ETestRunRow.started_at))
        .limit(limit)
    )).scalars().all()
    return [_row_to_dict(r) for r in rows]


async def build_latest_run_timeline(
    db: AsyncSession,
    *,
    artifact_id: str,
    tenant_id: str,
) -> dict:
    """Per-scenario, per-step pass/fail timeline for the single most-recent run.

    Returns::

        {
          "run_header": {... the E2ETestRunRow as a dict — passed_steps,
                         failed_steps, skipped_steps, total_steps, duration_ms,
                         started_at, status ...} | None,
          "scenarios": [{
            "scenario_id", "name", "type", "verdict", "confidence",
            "justification", "status", "baseline_available",
            "failed_step_number",
            "steps": [{"step_number", "label", "status", "duration_ms",
                       "error_message", "screenshot_url",
                       "baseline_screenshot", "expected"}],
          }],
        }

    Empty (run_header=None, scenarios=[]) when no runs exist.
    """
    # (a) the newest run for this artifact
    run = (await db.execute(
        select(E2ETestRunRow)
        .where(
            E2ETestRunRow.artifact_id == artifact_id,
            E2ETestRunRow.tenant_id == tenant_id,
        )
        .order_by(desc(E2ETestRunRow.started_at))
        .limit(1)
    )).scalars().first()
    if run is None:
        return {"run_header": None, "scenarios": []}
    return await _timeline_for_run(db, run, artifact_id=artifact_id, tenant_id=tenant_id)


async def build_run_timeline_by_id(
    db: AsyncSession,
    *,
    artifact_id: str,
    tenant_id: str,
    run_id: str,
) -> dict:
    """Per-step timeline for a SPECIFIC run (durable history re-inspection).

    Same shape as ``build_latest_run_timeline`` but for an arbitrary historical
    run, scoped to artifact+tenant. Empty (run_header=None) when the run_id is
    not found for this artifact. Deterministic, $0 LLM, read-only, no migration.
    """
    run = (await db.execute(
        select(E2ETestRunRow)
        .where(
            E2ETestRunRow.run_id == run_id,
            E2ETestRunRow.artifact_id == artifact_id,
            E2ETestRunRow.tenant_id == tenant_id,
        )
        .limit(1)
    )).scalars().first()
    if run is None:
        return {"run_header": None, "scenarios": []}
    return await _timeline_for_run(db, run, artifact_id=artifact_id, tenant_id=tenant_id)


async def _timeline_for_run(
    db: AsyncSession, run: E2ETestRunRow, *, artifact_id: str, tenant_id: str,
) -> dict:
    """Build the per-scenario, per-step timeline for an already-resolved run row.
    Shared by ``build_latest_run_timeline`` and ``build_run_timeline_by_id``."""
    # (b) every step of THAT run, grouped by scenario, ordered by step_number
    step_rows = (await db.execute(
        select(E2ETestRunStepRow)
        .where(
            E2ETestRunStepRow.run_id == run.run_id,
            E2ETestRunStepRow.tenant_id == tenant_id,
        )
        .order_by(E2ETestRunStepRow.scenario_id, E2ETestRunStepRow.step_number)
    )).scalars().all()

    # (c) recorded baseline cases — for human step labels + known-good shots.
    #     Imported locally to avoid a module-level import cycle (triage.py and
    #     test_factory.service both import from this module).
    from .test_factory import service as factory_service
    cases = await factory_service.load_active_production_cases(
        db, artifact_id=artifact_id,
    )
    cases_by_id = {getattr(c, "test_id", "") or "": c for c in cases}

    by_scenario: dict[str, list[E2ETestRunStepRow]] = {}
    for s in step_rows:
        by_scenario.setdefault(s.scenario_id or "", []).append(s)

    scenarios: list[dict] = []
    for sid, srows in by_scenario.items():
        tc = cases_by_id.get(sid)
        worst = E2E_STEP_STATUS_PASSED
        failing_error = ""
        failed_step_number: int | None = None
        out_steps: list[dict] = []
        for st in srows:
            if _status_severity(st.status) > _status_severity(worst):
                worst = st.status
            if st.status in _FAIL_STEP_STATUSES and failed_step_number is None:
                failed_step_number = st.step_number
            if st.status == E2E_STEP_STATUS_FAILED and not failing_error:
                failing_error = st.error_message or ""
            bs = _case_step(tc, st.step_number)
            label = (getattr(bs, "action", "") if bs is not None else "") or f"Step {st.step_number}"
            expected = ""
            if bs is not None:
                expected = (getattr(bs, "expected_result", "")
                            or getattr(bs, "expected", "") or "")
            out_steps.append({
                "step_number": st.step_number,
                "label": label,
                "status": st.status,
                "duration_ms": st.duration_ms,
                "error_message": st.error_message or "",
                "screenshot_url": st.screenshot_url or "",
                "video_url": (getattr(st, "metadata_json", None) or {}).get("video_url", ""),
                "baseline_screenshot": (getattr(bs, "screenshot", "") if bs is not None else "") or "",
                "expected": expected,
            })

        failed = worst in _FAIL_STEP_STATUSES
        selector_drifted = any(
            r.expected_selector and r.resolved_selector
            and r.expected_selector != r.resolved_selector
            for r in srows
        )
        # Per-run verdict (flake is a History concern, not a this-run concern).
        verdict = classify_failure(
            failed=failed,
            is_flaky=False,
            selector_drifted=selector_drifted,
            bbox_drifted=False,
            outcome_contradicted=outcome_contradicted_from_error(failing_error),
            error_message=failing_error,
        )
        scenarios.append({
            "scenario_id": sid,
            "name": (getattr(tc, "name", "") if tc is not None else "") or sid or "unnamed",
            "type": (getattr(tc, "type", "") if tc is not None else ""),
            "verdict": verdict.label,
            "confidence": verdict.confidence,
            "justification": verdict.justification,
            "status": worst,
            "baseline_available": tc is not None,
            "failed_step_number": failed_step_number,
            "steps": out_steps,
        })

    # Failures first, then alphabetical for a stable order.
    scenarios.sort(key=lambda c: (0 if c["status"] in _FAIL_STEP_STATUSES else 1, c["name"]))
    return {"run_header": _row_to_dict(run), "scenarios": scenarios}


async def scenario_verdict_history(
    db: AsyncSession, *, artifact_id: str, scenario_id: str, tenant_id: str,
    limit: int = 20,
) -> list[dict]:
    """GROUNDED VERDICT HISTORY for ONE scenario across recent runs (newest first).

    Per run: the worst-step status + the SAME deterministic ``classify_failure``
    verdict the live timeline uses (reused, never reimplemented — proven-green vs
    real-regression / selector-drift / flake), the duration, the final-frame success
    screenshot (last step with one), and any heal event that landed on that run. One
    runs query + one steps query (grouped in-process) + one heal-events read. No
    migration, read-only, $0 LLM."""
    limit = max(1, min(int(limit or 20), 100))
    runs = (await db.execute(
        select(E2ETestRunRow)
        .where(E2ETestRunRow.artifact_id == artifact_id,
               E2ETestRunRow.tenant_id == tenant_id)
        .order_by(desc(E2ETestRunRow.started_at)).limit(limit)
    )).scalars().all()
    if not runs:
        return []
    run_ids = [r.run_id for r in runs]
    step_rows = (await db.execute(
        select(E2ETestRunStepRow)
        .where(E2ETestRunStepRow.tenant_id == tenant_id,
               E2ETestRunStepRow.run_id.in_(run_ids),
               E2ETestRunStepRow.scenario_id == scenario_id)
        .order_by(E2ETestRunStepRow.run_id, E2ETestRunStepRow.step_number)
    )).scalars().all()
    by_run: dict[str, list] = {}
    for s in step_rows:
        by_run.setdefault(s.run_id, []).append(s)

    # Heal events keyed by run (best-effort; the strongest "proven green" provenance).
    heal_by_run: dict[str, str] = {}
    try:
        from . import heal_evidence as _he
        for e in (await _he.list_heal_events(db, tenant_id=tenant_id, artifact_id=artifact_id) or []):
            rid = (e.get("run_id") if isinstance(e, dict) else getattr(e, "run_id", "")) or ""
            et = (e.get("event_type") if isinstance(e, dict) else getattr(e, "event_type", "")) or ""
            if rid and et and rid not in heal_by_run:
                heal_by_run[rid] = et
    except Exception:
        pass

    out: list[dict] = []
    for run in runs:
        srows = by_run.get(run.run_id, [])
        worst = E2E_STEP_STATUS_PASSED
        failed_step: int | None = None
        failing_error = ""
        shot = ""
        vid = ""
        for st in srows:
            if _status_severity(st.status) > _status_severity(worst):
                worst = st.status
            if st.status in _FAIL_STEP_STATUSES and failed_step is None:
                failed_step = st.step_number
            if st.status == E2E_STEP_STATUS_FAILED and not failing_error:
                failing_error = st.error_message or ""
            if getattr(st, "screenshot_url", ""):
                shot = st.screenshot_url
            _md = getattr(st, "metadata_json", None) or {}
            if _md.get("video_url"):
                vid = _md["video_url"]
        ran = len(srows) > 0
        failed = worst in _FAIL_STEP_STATUSES
        selector_drifted = any(
            r.expected_selector and r.resolved_selector
            and r.expected_selector != r.resolved_selector
            for r in srows
        )
        verdict = classify_failure(
            failed=failed, is_flaky=False, selector_drifted=selector_drifted,
            bbox_drifted=False,
            outcome_contradicted=outcome_contradicted_from_error(failing_error),
            error_message=failing_error,
        )
        out.append({
            "run_id": run.run_id,
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "duration_ms": int(getattr(run, "duration_ms", 0) or 0),
            "ran": ran,
            "status": worst if ran else "not_run",
            "verdict": verdict.label if ran else "",
            "confidence": verdict.confidence if ran else 0.0,
            "failed_step_number": failed_step,
            "error_snippet": (failing_error or "")[:300],
            "screenshot_url": shot,
            "video_url": vid,
            "heal_event": heal_by_run.get(run.run_id, ""),
        })
    return out
