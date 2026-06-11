"""Phase A — Grounded Oracle, MEASURED.

A READ-ONLY, per-artifact scorecard that makes the (already-shipped) grounded
oracle *visible*: how much of the board rests on positive proof we VERIFIED vs
inference we ASSUMED, the oracle's design-confidence rollup, and heal INTEGRITY
(the engine never green-washes an unproven fix; plus a best-effort, honestly
flagged false-heal proxy).

It re-uses the existing verdict reducer verbatim
(``test_runs.classify_failure`` / ``last_run_summary_by_scenario`` /
``tally_verdicts``) — no new verdict logic, no LLM, no migration. Honest by
construction: empty / insufficient denominators are surfaced as ``None`` +
``insufficient_data``, never as a misleading ``0``.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nexus_sdk.db.models import (
    E2ETestRunRow,
    E2ETestRunStepRow,
    E2E_STEP_STATUS_BROKEN,
    E2E_STEP_STATUS_FAILED,
    E2E_STEP_STATUS_TIMED_OUT,
)

from .test_runs import (
    VERDICT_FLAKE,
    VERDICT_NEEDS_REVIEW,
    VERDICT_PASSED,
    VERDICT_REAL_REGRESSION,
    VERDICT_SELECTOR_DRIFT,
    VERDICT_VISUAL_CHANGE,
    classify_failure,
    last_run_summary_by_scenario,
    outcome_contradicted_from_error,
    tally_verdicts,
)
from .script_factory import versions as script_versions
from .test_factory import runner_jobs

_FAIL_STATUSES = frozenset({
    E2E_STEP_STATUS_FAILED, E2E_STEP_STATUS_BROKEN, E2E_STEP_STATUS_TIMED_OUT,
})

# Grounding doctrine (the whole point of this scorecard — DO NOT overclaim):
# only a real_regression is POSITIVELY PROVEN — it is a FAILED `toHaveURL`
# against the recorded next page, i.e. direct evidence the recorded outcome was
# not reached (classify_failure emits it only when outcome_contradicted is True).
# A bare `passed` is NOT counted as outcome-verified: the frozen compiler emits
# `toHaveURL` only for a step that has a recorded next page, so a green run can
# carry zero outcome assertions and only proves "nothing threw" — it is reported
# as a separate GREEN count, never as verified. selector-drift / flake /
# needs-review are inference. visual_change is unreachable on this path
# (bbox_drifted=False on the assemble path) and is reported not-measured, never
# invented.

_MACHINE_HEAL_AUTHORS = frozenset({"nexus-truefix", "nexus-autoheal"})


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


async def _verdicts_for_artifact(
    db: AsyncSession, *, artifact_id: str, tenant_id: str,
) -> list:
    """Re-derive the conservative verdict for the latest run of each scenario,
    using EXACTLY the inputs assemble_triage feeds (no duplicated logic)."""
    summary = await last_run_summary_by_scenario(
        db, artifact_id=artifact_id, tenant_id=tenant_id,
    )
    verdicts = []
    for _sid, s in summary.items():
        err = s.get("last_error_message", "") or ""
        verdicts.append(classify_failure(
            failed=(s["last_run_status"] in _FAIL_STATUSES),
            is_flaky=bool(s.get("is_flaky")),
            selector_drifted=bool(s.get("selector_drift_observed")),
            bbox_drifted=False,
            outcome_contradicted=outcome_contradicted_from_error(err),
            error_message=err,
        ))
    return verdicts


def _grounding_split(verdicts: list) -> dict:
    by: dict[str, int] = {}
    for v in verdicts:
        by[v.label] = by.get(v.label, 0) + 1
    green = by.get(VERDICT_PASSED, 0)
    proven = by.get(VERDICT_REAL_REGRESSION, 0)  # FAILED toHaveURL = positive proof
    inferred = (by.get(VERDICT_SELECTOR_DRIFT, 0)
                + by.get(VERDICT_FLAKE, 0)
                + by.get(VERDICT_NEEDS_REVIEW, 0))
    not_measured = by.get(VERDICT_VISUAL_CHANGE, 0)
    failures = proven + inferred + not_measured
    return {
        "green": green,
        "failures": failures,
        "proven": proven,
        "inferred": inferred,
        "not_measured": not_measured,
        "proven_pct": (round(100.0 * proven / failures, 1) if failures else None),
        "notes": [
            "Proven = a real regression caught by a FAILED toHaveURL outcome "
            "assertion — direct evidence the recorded outcome was not reached.",
            "Inferred = selector-drift, flake, or needs-review — the failure cause "
            "is inferred, not positively proven.",
            "Green = the scenario ran without error; its outcome oracle is NOT "
            "separately re-proven here (a step with no recorded next page emits no "
            "toHaveURL), so green is not counted as outcome-verified.",
            "Visual change is not measured at the artifact level (per-step signal) "
            "— never counted here.",
        ],
    }


def _confidence_rollup(verdicts: list) -> dict:
    # Scored over FAILURES only — passed is a fixed 1.0 and would inflate the mean
    # and the high-confidence bucket toward a misleading "perfect" on a green board.
    fail_confs = [v.confidence for v in verdicts if v.label != VERDICT_PASSED]

    def _avg(xs: list) -> float | None:
        return round(sum(xs) / len(xs), 3) if xs else None

    return {
        "avg_confidence_failures_only": _avg(fail_confs),
        "failures_scored": len(fail_confs),
        "distribution": {
            "high": sum(1 for c in fail_confs if c >= 0.8),
            "medium": sum(1 for c in fail_confs if 0.5 <= c < 0.8),
            "low": sum(1 for c in fail_confs if c < 0.5),
        },
        "note": (
            "Design confidence (heuristic) over FAILURES only — fixed per-verdict "
            "precision beliefs, NOT a learned or measured accuracy rate."
        ),
    }


async def _scenario_contradicted_after(
    db: AsyncSession, *, artifact_id: str, tenant_id: str,
    scenario_id: str, after: datetime,
) -> dict:
    """For a scenario, look at runs that started AFTER `after`: did any later run
    produce a real-regression (a failed toHaveURL outcome assertion)? Used by the
    approved-then-contradicted false-heal proxy."""
    rows = (await db.execute(
        select(E2ETestRunStepRow, E2ETestRunRow)
        .join(E2ETestRunRow, E2ETestRunStepRow.run_id == E2ETestRunRow.run_id)
        .where(
            E2ETestRunStepRow.artifact_id == artifact_id,
            E2ETestRunStepRow.tenant_id == tenant_id,
            E2ETestRunStepRow.scenario_id == scenario_id,
            E2ETestRunRow.started_at > after,
        )
    )).all()
    if not rows:
        return {"has_later_run": False, "contradicted": False}
    contradicted = any(
        step.status in _FAIL_STATUSES
        and outcome_contradicted_from_error(step.error_message or "")
        for step, _run in rows
    )
    return {"has_later_run": True, "contradicted": contradicted}


async def _heal_integrity(
    db: AsyncSession, *, artifact_id: str, tenant_id: str,
) -> dict:
    """Heal-integrity proxies from EXISTING data (machine heals only):
      * lifecycle counts (proposed / approved) from script_versions,
      * approved-then-contradicted = best-effort false-heal PROXY (flagged),
      * heal-attempt outcomes from the durable runner-job registry."""
    # 1. Machine-heal version lifecycle.
    rows = (await db.execute(
        select(script_versions.ScriptVersionRow).where(
            script_versions.ScriptVersionRow.artifact_id == artifact_id,
        )
    )).scalars().all()
    machine = [
        r for r in rows
        if (r.author in _MACHINE_HEAL_AUTHORS)
        or bool((r.data_json or {}).get("auto_healed"))
    ]
    proposed_pending = sum(1 for r in machine if (r.data_json or {}).get("pending_approval"))

    # 2. Approved-then-contradicted false-heal PROXY — de-duplicated to ONE per
    #    scenario (its latest approval), so two approved versions on the same
    #    scenario can't double-count the denominator or the numerator.
    latest_approved: dict[str, datetime] = {}
    for r in machine:
        appr_at = _parse_iso((r.data_json or {}).get("approved_at"))
        if appr_at is None:
            continue
        cur = latest_approved.get(r.test_case_id)
        if cur is None or appr_at > cur:
            latest_approved[r.test_case_id] = appr_at

    approved_with_later_run = 0
    approved_then_contradicted = 0
    for scenario_id, appr_at in latest_approved.items():
        later = await _scenario_contradicted_after(
            db, artifact_id=artifact_id, tenant_id=tenant_id,
            scenario_id=scenario_id, after=appr_at,
        )
        if later["has_later_run"]:
            approved_with_later_run += 1
            if later["contradicted"]:
                approved_then_contradicted += 1

    # 3. Heal-attempt outcomes from the durable runner-job registry. Two shapes:
    #      * single-step TrueFix (kind 'heal'): healed True/False/None.
    #      * multi-test Auto-Heal loop (kind 'auto-heal'): terminal_state
    #        ('clean_run_v1' = verified green; 'needs_human'/'error' = stopped).
    #    Both are classified so the ledger reconciles and real successes show.
    jobs = await runner_jobs.list_heal_jobs(artifact_id=artifact_id, tenant_id=tenant_id)
    attempted = len(jobs)
    applied_proposed = 0  # verified GREEN, saved as a PROPOSED (human-gated) version
    not_promoted = 0      # ran but did NOT yield a promoted fix (no green-wash)
    for j in jobs:
        kind = (j.get("kind") or "").lower()
        if kind == "auto-heal":
            if j.get("terminal_state") == "clean_run_v1" and (j.get("healed_count") or 0) > 0:
                applied_proposed += 1
            elif j.get("terminal_state") in ("needs_human", "error"):
                not_promoted += 1
        else:  # single-step TrueFix 'heal'
            if j.get("healed") is True:
                applied_proposed += 1
            elif j.get("healed") is False:
                not_promoted += 1
    in_progress = max(0, attempted - applied_proposed - not_promoted)

    return {
        "proposed_pending_approval": proposed_pending,
        "approved": len(latest_approved),
        "approved_with_later_run": approved_with_later_run,
        "approved_then_contradicted": approved_then_contradicted,
        "approved_then_contradicted_pct": (
            round(100.0 * approved_then_contradicted / approved_with_later_run, 1)
            if approved_with_later_run else None
        ),
        "insufficient_data": approved_with_later_run == 0,
        "attempts": {
            "attempted": attempted,
            "applied_proposed": applied_proposed,
            "not_promoted": not_promoted,
            "in_progress": in_progress,
        },
        "population": (
            "machine heals only (nexus-truefix / nexus-autoheal); human-edited "
            "versions excluded"
        ),
        "notes": [
            "The engine never green-washes: a fix is saved only after it re-runs "
            "GREEN, and even then as a PROPOSED version a human must approve before "
            "it becomes the active source for runs.",
            "'Not promoted' = a heal attempt that did not yield a promoted fix (the "
            "re-run did not prove green, often an environment / bot-block) — the "
            "prior version is left untouched. It is refusal-to-promote, NOT a "
            "fix-quality judgment.",
            "Approved-then-contradicted is a best-effort PROXY for false-heal rate, "
            "not a true rate: it only catches regressions that produce a toHaveURL "
            "contradiction, cannot prove the heal (vs an unrelated product change) "
            "caused it, and is de-duplicated to one per scenario.",
        ],
    }


# Below this denominator the proxy is too noisy to PUBLISH as a rate — we surface
# it as insufficient_data rather than a misleading small-sample percentage.
_MIN_N_FOR_PUBLISH = 5


def _false_heal_rate(heal: dict) -> dict:
    """Phase-1 'publish a false-heal rate' SCAFFOLD — the one honest top-line number,
    built from the existing heal-integrity proxy. Surfaces rate + denominator + an
    explicit publishable/insufficient status, and never invents a number on thin
    data. It is a PROXY (approved-then-contradicted), NOT the calibrated rate — that
    needs many human-confirmed heal outcomes (the flywheel's labeled corrections),
    deferred until that data exists. Per-artifact here; cross-artifact aggregation is
    a later step."""
    n = int(heal.get("approved_with_later_run", 0) or 0)
    contradicted = int(heal.get("approved_then_contradicted", 0) or 0)
    publishable = n >= _MIN_N_FOR_PUBLISH
    return {
        "rate_pct": (round(100.0 * contradicted / n, 1) if n else None),
        "numerator_contradicted": contradicted,
        "denominator_evaluated": n,
        "min_n_to_publish": _MIN_N_FOR_PUBLISH,
        "status": "measured_proxy" if publishable else "insufficient_data",
        "is_proxy": True,
        "definition": (
            "false-heal proxy = machine-approved fixes a LATER run contradicted "
            "(failed toHaveURL) ÷ machine-approved fixes with any later run."
        ),
        "to_calibrate": (
            "A publishable calibrated rate needs many human-confirmed heal outcomes "
            "(the flywheel's labeled corrections) — deferred until that data exists."
        ),
    }


async def compute_artifact_scorecard(
    db: AsyncSession, *, artifact_id: str, tenant_id: str,
) -> dict:
    """Assemble the per-artifact Grounded-Oracle scorecard. Read-only, $0 LLM,
    no migration. Reuses the verdict reducer; surfaces honesty caveats inline."""
    verdicts = await _verdicts_for_artifact(
        db, artifact_id=artifact_id, tenant_id=tenant_id,
    )
    heal = await _heal_integrity(db, artifact_id=artifact_id, tenant_id=tenant_id)
    return {
        "artifact_id": artifact_id,
        "as_of": _utc_now_iso(),
        "has_runs": bool(verdicts),
        "total_scenarios": len(verdicts),
        "grounding": _grounding_split(verdicts),
        "oracle_confidence": {
            **_confidence_rollup(verdicts),
            "board": tally_verdicts(verdicts),
        },
        "heal": heal,
        "false_heal_rate": _false_heal_rate(heal),
    }


__all__ = ["compute_artifact_scorecard"]
