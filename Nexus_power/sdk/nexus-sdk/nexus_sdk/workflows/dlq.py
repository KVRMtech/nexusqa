"""
DLQ inspection + replay surface.

Two streams of dead-lettering converge here:

  1. Quarantined workflows — workflow-level terminal failure or deadline.
     Stored in `workflow_state` with status=quarantined; the queue
     message that triggered the failure is already acked.

  2. Permanently failed envelopes — JobQueue's _move_to_dlq() pushed
     them to `nexus:queue:<lane>:dlq`. These carry the original payload
     and the failure reason, but no workflow-level state.

The admin router below exposes both via a uniform shape so a single UI
or CLI can triage. Replay is supported for both: workflow replay
re-dispatches from the current step_index; queue replay re-enqueues the
original message onto its main stream.

All endpoints require the `platform-admin` role.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from nexus_sdk.auth import NexusUser, get_current_user
from nexus_sdk.workflows.dispatch import WorkflowDispatcher
from nexus_sdk.workflows.manager import WorkflowManager
from nexus_sdk.workflows.models import WorkflowStatus

logger = logging.getLogger(__name__)


def _require_admin(user: NexusUser) -> None:
    if "platform-admin" not in (user.roles or []):
        raise HTTPException(status_code=403, detail="platform-admin role required")


# Phase 2: DLQ reason classification. Maps raw error strings to one of
# six operator-friendly categories so dashboards/alerts can route by
# failure mode (e.g. page on-call for `worker_crash` but auto-retry
# `timeout`). The regex-style matching is intentionally loose — any
# new failure mode just needs one more pattern below.
_REASON_PATTERNS: list[tuple[str, str]] = [
    # Reason key, substring/regex (case-insensitive substring match).
    ("envelope_expired",   "envelope expired"),
    ("step_timeout",       "step exceeded deadline"),
    ("ocr_frame_timeout",  "ocr_frame_timeout"),
    ("ocr_failed",         "ocr_failed"),
    ("vision_model_failed", "vision_model_failed"),
    ("vision_model_failed", "ollama"),
    ("vision_model_failed", "llava"),
    ("vision_model_failed", "moondream"),
    ("workflow_deadline",  "workflow-level deadline exceeded"),
    ("schema_validation",  "validation error"),
    ("schema_validation",  "pydantic"),
    ("input_missing",      "artifact_key missing"),
    ("input_missing",      "missing required"),
    ("input_missing",      "frames_manifest_key missing"),
    ("input_missing",      "scenes_manifest_key missing"),
    ("callback_failure",   "401 unauthorized"),
    ("callback_failure",   "result http_error"),
    ("worker_crash",       "name or service not known"),
    ("worker_crash",       "connection refused"),
    ("worker_crash",       "broken pipe"),
    ("downstream_unavailable", "503"),
    ("downstream_unavailable", "service unavailable"),
    ("downstream_unavailable", "milvus"),
    ("downstream_unavailable", "neo4j"),
]


def classify_dlq_reason(error_text: str | None) -> str:
    """Map a raw error message to an operator-friendly category.

    Returns one of: envelope_expired, step_timeout, ocr_frame_timeout,
    ocr_failed, vision_model_failed, workflow_deadline, schema_validation,
    input_missing, callback_failure, worker_crash, downstream_unavailable,
    or 'unknown' for messages that don't match any pattern.
    """
    if not error_text:
        return "unknown"
    lower = error_text.lower()
    for reason, pattern in _REASON_PATTERNS:
        if pattern.lower() in lower:
            return reason
    return "unknown"


def build_dlq_router(
    manager: WorkflowManager,
    dispatcher: WorkflowDispatcher,
    queue_factory,
    known_lanes: list[str],
) -> APIRouter:
    """
    `queue_factory(lane_name)` returns a connected JobQueue for the
    given lane (e.g. "eyes.cpu", "eyes.gpu"). `known_lanes` is the
    universe of lanes the platform recognises — used for cross-lane
    DLQ inventory.
    """
    # Mounted at /api/v1/canonical-admin/dlq to live in the same
    # namespace as the canonical-workflows surface (Phase 1).
    router = APIRouter(
        prefix="/api/v1/canonical-admin/dlq",
        tags=["dlq"],
    )

    # ── Workflow-level quarantine ──────────────────────────────

    @router.get("/workflows")
    async def list_quarantined_workflows(
        limit: int = Query(default=100, le=500),
        user: NexusUser = Depends(get_current_user),
    ):
        _require_admin(user)
        # Reuse the manager helper; the same query filters by status.
        from sqlalchemy import select

        from nexus_sdk.workflows.db_models import WorkflowStateRow

        async with manager._db.session() as s:
            stmt = (
                select(WorkflowStateRow)
                .where(WorkflowStateRow.status == WorkflowStatus.QUARANTINED.value)
                .order_by(WorkflowStateRow.updated_at.desc())
                .limit(limit)
            )
            rows = (await s.execute(stmt)).scalars().all()
        return {
            "count": len(rows),
            "workflows": [
                {
                    "workflow_id": r.workflow_id,
                    "kind": r.kind,
                    "tenant_id": r.tenant_id,
                    "session_id": r.session_id,
                    "current_step": r.current_step,
                    "step_index": r.step_index,
                    "attempt": r.attempt,
                    "error": r.error,
                    "reason": classify_dlq_reason(r.error),
                    "deadline_at": r.deadline_at.isoformat(),
                    "updated_at": r.updated_at.isoformat(),
                }
                for r in rows
            ],
        }

    @router.post("/workflows/{workflow_id}/replay", status_code=202)
    async def replay_workflow(
        workflow_id: str,
        user: NexusUser = Depends(get_current_user),
    ):
        _require_admin(user)
        row = await manager.get(workflow_id)
        if row is None:
            raise HTTPException(status_code=404, detail="workflow not found")
        if row.status != WorkflowStatus.QUARANTINED.value:
            raise HTTPException(
                status_code=409,
                detail=f"only quarantined workflows can be replayed (status={row.status})",
            )
        # Reset status + extend deadline. The original deadline was set
        # at create-time (now + plan.deadline_seconds); if the workflow
        # sat quarantined past that point, the next dispatch would
        # produce envelopes born expired (envelope_deadline =
        # min(step_deadline, row.deadline_at) — and row.deadline_at is
        # already in the past). Refresh deadline_at to now + the plan's
        # original budget so the replay has a fair shot. Also clear the
        # DAG in-flight set in case the prior worker died mid-step
        # without releasing it.
        from datetime import datetime, timedelta, timezone

        from sqlalchemy import update

        from nexus_sdk.workflows.db_models import WorkflowStateRow

        plan_deadline_s = int(
            (row.plan or {}).get("deadline_seconds", 900)
        )
        new_deadline = datetime.now(timezone.utc) + timedelta(
            seconds=plan_deadline_s,
        )
        # Clear DAG in-flight set on the checkpoint so the next dispatch
        # is free to redispatch the step the worker died on.
        new_ckpt = dict(row.checkpoint or {})
        if new_ckpt.get("__dag_in_flight_steps__"):
            new_ckpt["__dag_in_flight_steps__"] = []
        async with manager._db.session() as s:
            await s.execute(
                update(WorkflowStateRow)
                .where(WorkflowStateRow.workflow_id == workflow_id)
                .values(
                    status=WorkflowStatus.PENDING.value,
                    attempt=0,
                    error=None,
                    error_context=None,
                    deadline_at=new_deadline,
                    checkpoint=new_ckpt,
                )
            )
        msg_id = await dispatcher.dispatch_next(workflow_id)
        return {
            "workflow_id": workflow_id,
            "dispatched": msg_id is not None,
            "msg_id": msg_id,
            "new_deadline_at": new_deadline.isoformat(),
        }

    # ── Queue-level DLQ ────────────────────────────────────────

    @router.get("/queues")
    async def list_queue_dlqs(
        lane: Optional[str] = Query(default=None, description="restrict to one lane"),
        limit: int = Query(default=100, le=500),
        user: NexusUser = Depends(get_current_user),
    ):
        _require_admin(user)
        target_lanes = [lane] if lane else list(known_lanes)
        out: dict[str, list] = {}
        for ln in target_lanes:
            q = await queue_factory(ln)
            try:
                entries = await q.get_dlq_jobs(count=limit)
            except Exception as e:
                logger.warning("dlq.list_failed lane=%s err=%s", ln, e)
                entries = []
            out[ln] = entries
        return {"lanes": out}

    @router.post("/queues/{lane}/replay/{dlq_msg_id}", status_code=202)
    async def replay_queue_message(
        lane: str,
        dlq_msg_id: str,
        user: NexusUser = Depends(get_current_user),
    ):
        _require_admin(user)
        if lane not in known_lanes:
            raise HTTPException(status_code=400, detail=f"unknown lane {lane!r}")
        q = await queue_factory(lane)
        ok = await q.retry_dlq_job(dlq_msg_id)
        if not ok:
            raise HTTPException(status_code=404, detail="DLQ entry not found")
        return {"lane": lane, "dlq_msg_id": dlq_msg_id, "replayed": True}

    @router.delete("/queues/{lane}/{dlq_msg_id}", status_code=204)
    async def purge_queue_message(
        lane: str,
        dlq_msg_id: str,
        user: NexusUser = Depends(get_current_user),
    ):
        _require_admin(user)
        if lane not in known_lanes:
            raise HTTPException(status_code=400, detail=f"unknown lane {lane!r}")
        q = await queue_factory(lane)
        # Purge via XDEL on the dlq stream.
        if not q._connected or not q._redis:
            raise HTTPException(status_code=503, detail="queue not connected")
        deleted = await q._redis.xdel(q.dlq_key, dlq_msg_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="DLQ entry not found")
        return None

    # ── Phase 1: Operator-visibility endpoints ─────────────────
    #
    # Phase 1 of the architect's reliability plan calls for "queue
    # health showing Redis stream length, pending count, oldest
    # pending age, and consumer count per lane". Without this an
    # operator running `XPENDING` by hand to find stuck messages
    # cannot scale beyond one person.

    @router.get("/health")
    async def queue_health(
        user: NexusUser = Depends(get_current_user),
    ):
        """Per-lane queue health for operator dashboards.

        For each known lane returns: stream depth, consumer-group
        pending count, oldest pending age in seconds (the most
        useful "stuck" signal), DLQ depth, and consumer count.
        """
        _require_admin(user)
        out: list[dict] = []
        for ln in known_lanes:
            try:
                q = await queue_factory(ln)
                if not q._connected or not q._redis:
                    out.append({"lane": ln, "error": "not_connected"})
                    continue
                try:
                    stream_len = await q._redis.xlen(q.stream_key)
                except Exception:
                    stream_len = 0
                try:
                    dlq_len = await q._redis.xlen(q.dlq_key)
                except Exception:
                    dlq_len = 0
                # XINFO GROUPS for pending + lag + consumers
                try:
                    groups = await q._redis.xinfo_groups(q.stream_key)
                    grp = next(
                        (g for g in groups
                         if g.get("name") == q._config.consumer_group),
                        None,
                    )
                    pending = int(grp.get("pending", 0)) if grp else 0
                    consumer_count = int(grp.get("consumers", 0)) if grp else 0
                    lag = int(grp.get("lag", 0)) if grp else 0
                except Exception:
                    pending = consumer_count = lag = 0
                # Oldest pending message age — the most actionable
                # alert signal. If this exceeds the orphan threshold,
                # something is wedged.
                oldest_pending_age_ms: int = 0
                try:
                    pel_summary = await q._redis.xpending(
                        q.stream_key, q._config.consumer_group,
                    )
                    if isinstance(pel_summary, dict):
                        oldest_pending_age_ms = int(pel_summary.get("min", 0) or 0)
                    elif isinstance(pel_summary, list) and len(pel_summary) >= 2:
                        oldest_pending_age_ms = int(pel_summary[1] or 0)
                except Exception:
                    oldest_pending_age_ms = 0
                out.append({
                    "lane": ln,
                    "stream_length": stream_len,
                    "dlq_length": dlq_len,
                    "consumer_group": q._config.consumer_group,
                    "consumers": consumer_count,
                    "pending": pending,
                    "lag": lag,
                    "oldest_pending_age_seconds": (
                        round(oldest_pending_age_ms / 1000.0, 1)
                        if oldest_pending_age_ms else 0.0
                    ),
                })
            except Exception as e:
                out.append({"lane": ln, "error": str(e)})
        return {"lanes": out}

    @router.post("/workflows/{workflow_id}/force-unstick", status_code=202)
    async def force_unstick_workflow(
        workflow_id: str,
        user: NexusUser = Depends(get_current_user),
    ):
        """Unstick a RUNNING workflow whose step is wedged in PEL.

        Unlike `/replay` (which requires status=quarantined), this
        endpoint works on a running workflow whose in_flight step
        has stopped making progress (no heartbeat update, PEL
        entry orphaned, etc.). It clears `__dag_in_flight_steps__`
        and bumps last_heartbeat so the orchestrator's sweeper
        re-dispatches the in-flight step. The matching PEL entries
        get XACK'd separately by the queue layer's PEL replay on
        next consumer iteration.

        Use sparingly. The right long-term fix is to make the
        per-step deadline + worker heartbeat catch hangs.
        """
        _require_admin(user)
        row = await manager.get(workflow_id)
        if row is None:
            raise HTTPException(status_code=404, detail="workflow not found")
        if row.status in {
            WorkflowStatus.COMPLETED.value, WorkflowStatus.CANCELLED.value,
        }:
            raise HTTPException(
                status_code=409,
                detail=f"workflow already terminal (status={row.status})",
            )
        from datetime import datetime, timedelta, timezone
        from sqlalchemy import update

        from nexus_sdk.workflows.db_models import WorkflowStateRow

        async with manager._db.session() as s:
            ck = dict(row.checkpoint or {})
            ck["__dag_in_flight_steps__"] = []
            await s.execute(
                update(WorkflowStateRow)
                .where(WorkflowStateRow.workflow_id == workflow_id)
                .values(
                    status=WorkflowStatus.RUNNING.value,
                    checkpoint=ck,
                    # Stale heartbeat so the sweeper detects + re-dispatches.
                    last_heartbeat=datetime.now(timezone.utc) - timedelta(minutes=5),
                    error=None,
                    error_context=None,
                )
            )
        # Try dispatch immediately too (don't wait for the sweeper tick).
        try:
            msg_id = await dispatcher.dispatch_next(workflow_id)
        except Exception as e:
            logger.warning("force_unstick.dispatch_failed wf=%s err=%s", workflow_id, e)
            msg_id = None
        return {
            "workflow_id": workflow_id,
            "unstuck": True,
            "dispatched": msg_id is not None,
            "msg_id": msg_id,
        }

    @router.post("/workflows/replay-all-quarantined", status_code=202)
    async def replay_all_quarantined(
        step: Optional[str] = Query(
            default=None,
            description="restrict to workflows quarantined on this step name (e.g. 'eyes.ocr_frames')",
        ),
        kind: Optional[str] = Query(
            default=None,
            description="restrict to workflows of this kind (audio.canonicalize | video.canonicalize | multimodal.canonicalize)",
        ),
        limit: int = Query(default=200, le=1000),
        user: NexusUser = Depends(get_current_user),
    ):
        """Bulk replay all quarantined workflows, optionally filtered
        by step or kind. Idempotent — workflows that aren't quarantined
        are skipped silently.

        Use case: after deploying a bug fix that quarantined N
        workflows on the same step, run this to retry them all in one
        call instead of N curl commands.
        """
        _require_admin(user)
        from sqlalchemy import select

        from nexus_sdk.workflows.db_models import WorkflowStateRow

        async with manager._db.session() as s:
            stmt = (
                select(WorkflowStateRow)
                .where(WorkflowStateRow.status == WorkflowStatus.QUARANTINED.value)
            )
            if kind:
                stmt = stmt.where(WorkflowStateRow.kind == kind)
            if step:
                stmt = stmt.where(WorkflowStateRow.current_step == step)
            stmt = stmt.order_by(WorkflowStateRow.updated_at.desc()).limit(limit)
            rows = (await s.execute(stmt)).scalars().all()

        from datetime import datetime, timedelta, timezone

        results = []
        for r in rows:
            try:
                from sqlalchemy import update
                plan_deadline_s = int(
                    (r.plan or {}).get("deadline_seconds", 900)
                )
                new_deadline = datetime.now(timezone.utc) + timedelta(
                    seconds=plan_deadline_s,
                )
                new_ckpt = dict(r.checkpoint or {})
                if new_ckpt.get("__dag_in_flight_steps__"):
                    new_ckpt["__dag_in_flight_steps__"] = []
                async with manager._db.session() as s2:
                    await s2.execute(
                        update(WorkflowStateRow)
                        .where(WorkflowStateRow.workflow_id == r.workflow_id)
                        .values(
                            status=WorkflowStatus.PENDING.value,
                            attempt=0,
                            error=None,
                            error_context=None,
                            deadline_at=new_deadline,
                            checkpoint=new_ckpt,
                        )
                    )
                msg_id = await dispatcher.dispatch_next(r.workflow_id)
                results.append({
                    "workflow_id": r.workflow_id,
                    "previous_step": r.current_step,
                    "dispatched": msg_id is not None,
                })
            except Exception as e:
                results.append({
                    "workflow_id": r.workflow_id,
                    "dispatched": False,
                    "error": str(e),
                })
        return {
            "selected": len(rows),
            "replayed": sum(1 for r in results if r.get("dispatched")),
            "details": results,
        }

    @router.post("/workflows/replay-all-stuck", status_code=202)
    async def replay_all_stuck(
        min_seconds: int = Query(
            default=600,
            description="min stale-heartbeat seconds to consider a workflow stuck",
        ),
        limit: int = Query(default=200, le=1000),
        user: NexusUser = Depends(get_current_user),
    ):
        """Bulk force-unstick all RUNNING workflows whose heartbeat
        has been stale longer than `min_seconds`. Equivalent to
        calling `/force-unstick` on each one. Default threshold of
        600s catches workflows that have been silently stuck for
        10+ minutes — well past any legitimate step duration on a
        healthy stack.
        """
        _require_admin(user)
        from datetime import datetime, timedelta, timezone
        from sqlalchemy import select, update

        from nexus_sdk.workflows.db_models import WorkflowStateRow

        cutoff = datetime.now(timezone.utc) - timedelta(seconds=min_seconds)
        async with manager._db.session() as s:
            stmt = (
                select(WorkflowStateRow)
                .where(WorkflowStateRow.status == WorkflowStatus.RUNNING.value)
                .where(WorkflowStateRow.last_heartbeat < cutoff)
                .order_by(WorkflowStateRow.last_heartbeat.asc())
                .limit(limit)
            )
            rows = (await s.execute(stmt)).scalars().all()

        results = []
        for r in rows:
            try:
                async with manager._db.session() as s2:
                    ck = dict(r.checkpoint or {})
                    ck["__dag_in_flight_steps__"] = []
                    await s2.execute(
                        update(WorkflowStateRow)
                        .where(WorkflowStateRow.workflow_id == r.workflow_id)
                        .values(
                            checkpoint=ck,
                            last_heartbeat=datetime.now(timezone.utc) - timedelta(minutes=5),
                            error=None,
                            error_context=None,
                        )
                    )
                msg_id = await dispatcher.dispatch_next(r.workflow_id)
                results.append({
                    "workflow_id": r.workflow_id,
                    "previous_step": r.current_step,
                    "dispatched": msg_id is not None,
                })
            except Exception as e:
                results.append({
                    "workflow_id": r.workflow_id,
                    "dispatched": False,
                    "error": str(e),
                })
        return {
            "selected": len(rows),
            "unstuck": sum(1 for r in results if r.get("dispatched")),
            "min_seconds": min_seconds,
            "details": results,
        }

    @router.get("/workflows/stuck")
    async def list_stuck_workflows(
        min_seconds: int = Query(
            default=300,
            description="minimum age of last_heartbeat in seconds to be considered stuck",
        ),
        user: NexusUser = Depends(get_current_user),
    ):
        """List RUNNING workflows whose heartbeat is stale beyond
        `min_seconds` — they're likely stuck in a worker that's
        not reporting back."""
        _require_admin(user)
        from datetime import datetime, timedelta, timezone
        from sqlalchemy import select

        from nexus_sdk.workflows.db_models import WorkflowStateRow

        cutoff = datetime.now(timezone.utc) - timedelta(seconds=min_seconds)
        async with manager._db.session() as s:
            stmt = (
                select(WorkflowStateRow)
                .where(WorkflowStateRow.status == WorkflowStatus.RUNNING.value)
                .where(WorkflowStateRow.last_heartbeat < cutoff)
                .order_by(WorkflowStateRow.last_heartbeat.asc())
                .limit(200)
            )
            rows = (await s.execute(stmt)).scalars().all()
        return {
            "count": len(rows),
            "min_seconds": min_seconds,
            "workflows": [
                {
                    "workflow_id": r.workflow_id,
                    "kind": r.kind,
                    "tenant_id": r.tenant_id,
                    "current_step": r.current_step,
                    "in_flight": (r.checkpoint or {}).get("__dag_in_flight_steps__") or [],
                    "step_attempts": (r.checkpoint or {}).get("__dag_step_attempts__") or {},
                    "last_heartbeat": r.last_heartbeat.isoformat(),
                    "stale_seconds": int(
                        (datetime.now(timezone.utc) - r.last_heartbeat).total_seconds()
                    ),
                    "created_at": r.created_at.isoformat(),
                }
                for r in rows
            ],
        }

    return router
