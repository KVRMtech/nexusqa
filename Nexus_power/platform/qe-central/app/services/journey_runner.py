"""Journey run orchestration (Release D2/D3).

``Run journey`` dispatches the journey's adopted end-to-end case through the
EXISTING factory → runner path (no parallel machinery), records one
``journey_runs`` ledger row, polls the transient runner job to its terminal
status, then resolves the DURABLE ingested run (the evidence/report key) by
matching ``ci_run_id`` and folds the verdict summary back onto the row.

Honesty laws:
  * a run verdict NEVER mutates the crawl-side journey claim — different
    facts, displayed side by side;
  * every dispatch failure is a ledger row too (``blocked`` with the
    factory's reason — quarantined, member-data gate, no active case), never
    a silent swallow;
  * a poll that outlives its budget ends ``timed_out`` honestly;
  * qe-central restarts do not strand rows — read-time reconciliation
    re-polls anything still marked running.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Any, Optional

from sqlalchemy import select

from ..clients import factory
from ..clients.factory import FactoryClientError
from ..config import settings
from ..db import tenant_scoped_qec_session, utc_now
from ..db.journey_run_models import JourneyRunRow

logger = logging.getLogger(__name__)

#: Transient runner-job statuses that mean "still going".
_IN_FLIGHT = frozenset({"dispatched", "running", "idle"})
#: Terminal statuses the transient job can end in.
_TERMINAL = frozenset({"passed", "failed", "timed_out", "error", "blocked"})


def _sid(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:64]


async def dispatch_journey_run(
    *, tenant_id: str, app_id: str, journey_id: str, artifact_id: str,
    test_case_id: str, env_ref: str = "", identity_ref: str = "",
) -> dict[str, Any]:
    """Dispatch ONE journey execution and record its ledger row.

    Returns ``{journey_run_id, status, dispatch_run_id, blocked_reason}``.
    Never raises for factory rejections — they become honest ``blocked``
    rows (quarantined case, member-data gate, no matching active case)."""
    journey_run_id = _sid("jrun", tenant_id, journey_id, artifact_id,
                          test_case_id, utc_now().isoformat())
    dispatch_run_id = ""
    status = "blocked"
    blocked_reason = ""
    try:
        result = await factory.run_cases(
            tenant_id=tenant_id, artifact_id=artifact_id,
            test_ids=[test_case_id])
        r_status = str(result.get("status") or "")
        if r_status == "running" and result.get("run_id"):
            dispatch_run_id = str(result["run_id"])
            status = "running"
        elif r_status == "blocked":
            blocked_reason = (
                f"{str(result.get('blocked_reason') or 'blocked')}: "
                f"{str(result.get('note') or '')}"
            ).strip(" :")[:400]
        else:
            blocked_reason = f"unexpected dispatch status {r_status!r}"[:400]
    except FactoryClientError as exc:
        blocked_reason = f"dispatch rejected ({exc.status_code}): {exc}"[:400]
    except Exception as exc:
        blocked_reason = f"dispatch failed: {exc}"[:400]

    async with tenant_scoped_qec_session(tenant_id) as session:
        session.add(JourneyRunRow(
            journey_run_id=journey_run_id, tenant_id=tenant_id, app_id=app_id,
            journey_id=journey_id, artifact_id=artifact_id,
            test_case_id=test_case_id, dispatch_run_id=dispatch_run_id,
            status=status, blocked_reason=blocked_reason,
            env_ref=env_ref[:200], identity_ref=identity_ref[:200]))

    if status == "running":
        _spawn_poller(tenant_id=tenant_id, journey_run_id=journey_run_id,
                      artifact_id=artifact_id, dispatch_run_id=dispatch_run_id)
    else:
        logger.warning(
            "qec.journey_runner.blocked tenant=%s journey=%s reason=%s",
            tenant_id, journey_id, blocked_reason)
    return {"journey_run_id": journey_run_id, "status": status,
            "dispatch_run_id": dispatch_run_id,
            "blocked_reason": blocked_reason}


def _spawn_poller(*, tenant_id: str, journey_run_id: str, artifact_id: str,
                  dispatch_run_id: str) -> None:
    """Fire-and-forget poll task. Outside an event loop (unit tests) this is
    a no-op — read-time reconciliation covers the row."""
    try:
        asyncio.get_running_loop().create_task(_poll_to_terminal(
            tenant_id=tenant_id, journey_run_id=journey_run_id,
            artifact_id=artifact_id, dispatch_run_id=dispatch_run_id))
    except RuntimeError:
        logger.warning("qec.journey_runner.no_loop journey_run=%s",
                       journey_run_id)


async def _poll_to_terminal(
    *, tenant_id: str, journey_run_id: str, artifact_id: str,
    dispatch_run_id: str,
) -> None:
    """Poll the transient runner job until terminal (bounded), then fold the
    verdict back. Never raises."""
    waited = 0.0
    status = "running"
    job: dict[str, Any] = {}
    try:
        while waited < settings.journey_run_timeout_s:
            await asyncio.sleep(settings.journey_run_poll_s)
            waited += settings.journey_run_poll_s
            try:
                job = await factory.run_status(
                    tenant_id=tenant_id, artifact_id=artifact_id,
                    run_id=dispatch_run_id)
            except Exception as exc:
                logger.warning(
                    "qec.journey_runner.poll_error run=%s err=%s",
                    dispatch_run_id, str(exc)[:160])
                continue
            status = str(job.get("status") or "unknown")
            if status in _TERMINAL:
                break
            if status not in _IN_FLIGHT and status != "unknown":
                break
        else:
            status = "timed_out"
    except Exception as exc:  # never let a poller die silently
        logger.warning("qec.journey_runner.poller_crashed run=%s err=%s",
                       dispatch_run_id, str(exc)[:200])
        status = "error"
    await _fold_back(tenant_id=tenant_id, journey_run_id=journey_run_id,
                     artifact_id=artifact_id, dispatch_run_id=dispatch_run_id,
                     status=status, job=job)


async def _fold_back(
    *, tenant_id: str, journey_run_id: str, artifact_id: str,
    dispatch_run_id: str, status: str, job: dict[str, Any],
) -> None:
    """Write the terminal status + resolve the durable ingested run id
    (ci_run_id correlation) + fold the verdict summary onto the ledger row."""
    ingested_run_id = ""
    verdict: dict[str, Any] = {
        "status": status,
        "exit_code": job.get("exit_code"),
        "steps_completed": job.get("steps_completed"),
        "total_tests": job.get("total_tests"),
    }
    try:
        runs = await factory.list_runs(
            tenant_id=tenant_id, artifact_id=artifact_id, limit=25)
        for r in runs:
            if str(r.get("ci_run_id") or "") == dispatch_run_id:
                ingested_run_id = str(r.get("run_id") or "")
                verdict.update({
                    "run_status_db": r.get("status"),
                    "total_steps": r.get("total_steps"),
                    "passed_steps": r.get("passed_steps"),
                    "failed_steps": r.get("failed_steps"),
                    "environment": r.get("environment"),
                })
                break
    except Exception as exc:
        logger.warning("qec.journey_runner.ingest_resolve_failed run=%s err=%s",
                       dispatch_run_id, str(exc)[:160])

    try:
        async with tenant_scoped_qec_session(tenant_id) as session:
            row = (await session.execute(
                select(JourneyRunRow).where(
                    JourneyRunRow.journey_run_id == journey_run_id,
                ))).scalar_one_or_none()
            if row is None:
                return
            row.status = status[:24]
            row.ingested_run_id = ingested_run_id
            row.verdict_summary = {k: v for k, v in verdict.items()
                                   if v is not None}
            row.finished_at = utc_now()
    except Exception as exc:
        logger.warning("qec.journey_runner.foldback_failed run=%s err=%s",
                       journey_run_id, str(exc)[:200])
        return
    logger.warning(
        "qec.journey_runner.folded journey_run=%s status=%s ingested=%s",
        journey_run_id, status, ingested_run_id or "-")


async def reconcile_stale(*, tenant_id: str, app_id: str) -> int:
    """Read-time safety net: one poll attempt for rows still in flight (a
    qe-central restart kills fire-and-forget pollers; the ledger must not
    strand as 'running' forever). Returns rows reconciled."""
    reconciled = 0
    async with tenant_scoped_qec_session(tenant_id) as session:
        rows = (await session.execute(
            select(JourneyRunRow).where(
                JourneyRunRow.tenant_id == tenant_id,
                JourneyRunRow.app_id == app_id,
                JourneyRunRow.status.in_(sorted(_IN_FLIGHT)),
            ))).scalars().all()
        stale = [(r.journey_run_id, r.artifact_id, r.dispatch_run_id)
                 for r in rows if r.dispatch_run_id]
    for journey_run_id, run_artifact, dispatch_run_id in stale:
        try:
            job = await factory.run_status(
                tenant_id=tenant_id, artifact_id=run_artifact,
                run_id=dispatch_run_id)
        except Exception:
            continue
        status = str(job.get("status") or "")
        if status in _TERMINAL or status == "unknown":
            await _fold_back(
                tenant_id=tenant_id, journey_run_id=journey_run_id,
                artifact_id=run_artifact, dispatch_run_id=dispatch_run_id,
                status=(status if status in _TERMINAL else "error"), job=job)
            reconciled += 1
    return reconciled


async def latest_run(
    session, *, tenant_id: str, app_id: str, journey_id: str,
) -> Optional[JourneyRunRow]:
    """The journey's most recent run ledger row, or ``None``."""
    return (await session.execute(
        select(JourneyRunRow).where(
            JourneyRunRow.tenant_id == tenant_id,
            JourneyRunRow.app_id == app_id,
            JourneyRunRow.journey_id == journey_id,
        ).order_by(JourneyRunRow.started_at.desc())
        .limit(1))).scalar_one_or_none()
