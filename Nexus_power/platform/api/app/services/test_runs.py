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
