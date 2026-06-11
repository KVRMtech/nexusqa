"""Grounded triage assembler — turns a run's failures into the triage board.

For each scenario in the latest run it joins:
  * the run's failing step (status, error, actual screenshot) — from E2ETestRunStep,
  * the captured BASELINE step (known-good screenshot + expected outcome) — from
    the grounded ProductionTestCase,
  * the conservative verdict — from test_runs.classify_failure,
and rolls them into the board tally (test_runs.tally_verdicts).

Deterministic, $0 LLM. Returns an empty board honestly when no runs exist.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nexus_sdk.db.models import (
    E2ETestRunStepRow,
    E2E_STEP_STATUS_BROKEN,
    E2E_STEP_STATUS_FAILED,
    E2E_STEP_STATUS_TIMED_OUT,
)

from ..test_factory import service as factory_service
from ..test_runs import (
    classify_failure,
    last_run_summary_by_scenario,
    outcome_contradicted_from_error,
    tally_verdicts,
)

_FAIL_STATUSES = frozenset({
    E2E_STEP_STATUS_FAILED, E2E_STEP_STATUS_BROKEN, E2E_STEP_STATUS_TIMED_OUT,
})

_EMPTY_BOARD = {
    "total": 0, "failures": 0, "need_you": 0, "dont_need_you": 0, "by_label": {},
}


def _baseline_step(tc, step_number):
    for st in (getattr(tc, "steps", None) or []):
        if getattr(st, "step_number", None) == step_number:
            return st
    return None


async def assemble_triage(
    db: AsyncSession, *, artifact_id: str, tenant_id: str,
) -> dict:
    """Return {board, scenarios[]} for the latest run of each scenario."""
    summary = await last_run_summary_by_scenario(
        db, artifact_id=artifact_id, tenant_id=tenant_id,
    )
    if not summary:
        return {"board": dict(_EMPTY_BOARD), "scenarios": []}

    cases = await factory_service.load_active_production_cases(
        db, artifact_id=artifact_id,
    )
    cases_by_id = {getattr(c, "test_id", "") or "": c for c in cases}

    # The failing step per scenario in its latest run (lowest-numbered failure).
    last_run_ids = {s["last_run_id"] for s in summary.values() if s.get("last_run_id")}
    fail_step_by_scenario: dict[str, E2ETestRunStepRow] = {}
    if last_run_ids:
        rows = (
            await db.execute(
                select(E2ETestRunStepRow).where(
                    E2ETestRunStepRow.artifact_id == artifact_id,
                    E2ETestRunStepRow.tenant_id == tenant_id,
                    E2ETestRunStepRow.run_id.in_(last_run_ids),
                )
            )
        ).scalars().all()
        for r in rows:
            if r.status in _FAIL_STATUSES:
                cur = fail_step_by_scenario.get(r.scenario_id)
                if cur is None or r.step_number < cur.step_number:
                    fail_step_by_scenario[r.scenario_id] = r

    scenarios: list[dict] = []
    verdicts = []
    for sid, s in summary.items():
        failed = s["last_run_status"] in _FAIL_STATUSES
        tc = cases_by_id.get(sid)
        fail_step = fail_step_by_scenario.get(sid)
        step_no = fail_step.step_number if fail_step is not None else None

        _err = s.get("last_error_message", "") or ""
        verdict = classify_failure(
            failed=failed,
            is_flaky=bool(s.get("is_flaky")),
            selector_drifted=bool(s.get("selector_drift_observed")),
            bbox_drifted=False,
            outcome_contradicted=outcome_contradicted_from_error(_err),
            error_message=_err,
        )
        verdicts.append(verdict)

        baseline_screenshot = ""
        expected = ""
        if tc is not None and step_no is not None:
            bs = _baseline_step(tc, step_no)
            if bs is not None:
                baseline_screenshot = getattr(bs, "screenshot", "") or ""
                expected = (getattr(bs, "expected_result", "")
                            or getattr(bs, "expected", "") or "")

        scenarios.append({
            "scenario_id": sid,
            "name": (getattr(tc, "name", "") if tc is not None else sid),
            "type": (getattr(tc, "type", "") if tc is not None else ""),
            "status": s["last_run_status"],
            "verdict": verdict.label,
            "confidence": verdict.confidence,
            "justification": verdict.justification,
            "step_number": step_no,
            "baseline_screenshot": baseline_screenshot,
            "actual_screenshot": (fail_step.screenshot_url if fail_step is not None else ""),
            "expected": expected,
            "error_message": s.get("last_error_message", "") or "",
            "is_flaky": bool(s.get("is_flaky")),
            "flake_rate_pct": s.get("flake_rate_pct", 0.0),
            "last_run_at": s.get("last_run_at"),
        })

    # Real bugs / needs-review first, dismissable last — the board's priority order.
    _ORDER = {
        "real_regression": 0, "needs_review": 1,
        "selector_drift": 2, "visual_change": 3, "flake": 4, "passed": 5,
    }
    scenarios.sort(key=lambda c: _ORDER.get(c["verdict"], 9))
    return {"board": tally_verdicts(verdicts), "scenarios": scenarios}
