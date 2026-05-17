"""
FastAPI router exposing the orchestration-plane endpoints.

Endpoints:
  POST /api/v1/canonical-workflows                         create + dispatch
  GET  /api/v1/canonical-workflows/{wf}                    inspect
  GET  /api/v1/canonical-workflows/{wf}/history            step audit trail
  POST /api/v1/canonical-workflows/{wf}/heartbeat          worker beat
  POST /api/v1/canonical-workflows/{wf}/steps/{step}/result    worker callback
  POST /api/v1/canonical-workflows/{wf}/cancel             operator cancel

Auth: callers (engine pods and operators) authenticate with the same
JWT bearer the rest of the platform uses. Engines use a service-account
token (short-lived, rotated by the cluster) so a compromised worker
cannot cancel arbitrary workflows.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from nexus_sdk.auth import NexusUser, get_current_user
from nexus_sdk.workflows.admission import (
    IdempotencyCache,
    TenantRateLimiter,
    check_admission,
)
from nexus_sdk.workflows.dispatch import WorkflowDispatcher
from nexus_sdk.workflows.manager import WorkflowManager, WorkflowNotFound
from nexus_sdk.workflows.metrics import classify_tenant
from nexus_sdk.workflows.models import (
    StepKind,
    StepPlan,
    StepResult,
    WorkflowKind,
    WorkflowPlan,
    WorkflowStatus,
)
from nexus_sdk.workflows.plans import build_plan

logger = logging.getLogger(__name__)


# ─── Wire models ────────────────────────────────────────────────


class CreateWorkflowRequest(BaseModel):
    kind: str = Field(..., description="audio.canonicalize | video.canonicalize | ...")
    tenant_id: str
    session_id: str
    profile: str = "fast"
    initial_state: dict = Field(default_factory=dict)
    metadata: dict = Field(default_factory=dict)
    # When set, overrides the canonical plan with a caller-supplied one.
    # Useful for the regression suite and for kind=custom workflows.
    custom_steps: Optional[list[dict]] = None
    deadline_seconds: Optional[int] = None


class CreateWorkflowResponse(BaseModel):
    workflow_id: str
    status: str
    dispatched: bool


class WorkflowStatusResponse(BaseModel):
    workflow_id: str
    kind: str
    status: str
    current_step: Optional[str]
    step_index: int
    attempt: int
    error: Optional[str]
    deadline_at: str
    created_at: str
    completed_at: Optional[str]
    # DAG-derived authoritative state. Use these instead of step_history
    # for status display — step_history can lag or contain stale rows
    # when parallel steps complete close in time (see manager bug fix).
    dag_completed_steps: list[str] = Field(default_factory=list)
    dag_in_flight_steps: list[str] = Field(default_factory=list)
    # Ordered list of (step_name, engine) from the plan, so callers
    # can render the stage rail in the correct order without re-fetching.
    plan_steps: list[dict] = Field(default_factory=list)
    # Top-level artifact_id from the checkpoint (Phase 3). The UI's
    # legacy lookup path read this from
    # `data.stages.artifact_persistence.output.artifact_id` which
    # doesn't exist on Phase 12 DAG workflows — so completed workflows
    # rendered as "RUNNING / finalizing" forever in Track-Job-By-ID.
    # Exposing it at the top level is the single source of truth.
    artifact_id: Optional[str] = None


class StepResultRequest(BaseModel):
    success: bool
    checkpoint: dict = Field(default_factory=dict)
    error: str = ""
    error_context: dict = Field(default_factory=dict)
    duration_ms: int = 0
    fatal: bool = False


# ─── Router builder ─────────────────────────────────────────────


def build_router(
    manager: WorkflowManager,
    dispatcher: WorkflowDispatcher,
    *,
    redis_client=None,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/canonical-workflows", tags=["workflows"])
    rate_limiter = TenantRateLimiter(redis_client)
    idem_cache = IdempotencyCache(redis_client)

    @router.post("", response_model=CreateWorkflowResponse, status_code=201)
    async def create_workflow(
        req: CreateWorkflowRequest,
        user: NexusUser = Depends(get_current_user),
        idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    ):
        # Tenant guard: callers cannot create workflows for tenants they
        # don't belong to. Operators with the platform-admin role bypass.
        if user.tenant_id != req.tenant_id and "platform-admin" not in (user.roles or []):
            raise HTTPException(status_code=403, detail="tenant mismatch")

        # ── Admission control ─────────────────────────────────
        # Idempotency replay + per-tenant rate limit. Both degrade
        # safely when Redis is unreachable.
        tenant_class = classify_tenant(req.tenant_id)
        cached, throttled = await check_admission(
            tenant_id=req.tenant_id,
            tenant_class=tenant_class,
            idempotency_key=idempotency_key,
            rate_limiter=rate_limiter,
            idem_cache=idem_cache,
        )
        if cached is not None:
            return CreateWorkflowResponse(**cached)
        if throttled is not None:
            retry_after, tier = throttled
            raise HTTPException(
                status_code=429,
                detail=(
                    f"rate limit exceeded for tenant_class={tenant_class!r} "
                    f"(limit={tier.rate_per_minute}/min, burst={tier.burst}). "
                    f"retry after {retry_after:.1f}s"
                ),
                headers={
                    "Retry-After": str(int(retry_after) + 1),
                    "X-Tenant-Class": tenant_class,
                },
            )

        if req.custom_steps is not None:
            steps = [
                StepPlan(
                    name=s["name"],
                    engine=s["engine"],
                    kind=StepKind(s.get("kind", "cpu")),
                    deadline_seconds=int(s.get("deadline_seconds", 300)),
                    max_attempts=int(s.get("max_attempts", 3)),
                    params=dict(s.get("params", {})),
                )
                for s in req.custom_steps
            ]
            plan = WorkflowPlan(
                kind=WorkflowKind(req.kind),
                tenant_id=req.tenant_id,
                session_id=req.session_id,
                steps=steps,
                initial_state=req.initial_state,
                metadata=req.metadata,
                deadline_seconds=req.deadline_seconds or 900,
            )
        else:
            try:
                plan = build_plan(
                    req.kind,
                    req.tenant_id,
                    req.session_id,
                    profile=req.profile,
                    metadata=req.metadata,
                )
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
            if req.deadline_seconds:
                plan.deadline_seconds = req.deadline_seconds
            if req.initial_state:
                plan.initial_state = dict(req.initial_state)

        try:
            wf_id = await manager.create(plan)
        except Exception as e:
            logger.error("workflow.create_failed err=%s", e)
            raise HTTPException(status_code=500, detail=f"create failed: {e}")

        dispatched = False
        try:
            dispatched = bool(await dispatcher.dispatch_next(wf_id))
        except Exception as e:
            logger.warning("workflow.dispatch_failed wf=%s err=%s", wf_id, e)

        response = CreateWorkflowResponse(
            workflow_id=wf_id,
            status="pending" if not dispatched else "running",
            dispatched=dispatched,
        )
        # Cache the response so retries with the same Idempotency-Key
        # short-circuit. Success path → 24h TTL; failure path is set
        # inside the exception branch (see HTTPException paths above).
        if idempotency_key:
            await idem_cache.store(
                req.tenant_id, idempotency_key,
                response.model_dump(),
                success=True,
            )
        return response

    @router.get("/{workflow_id}", response_model=WorkflowStatusResponse)
    async def get_workflow(
        workflow_id: str,
        user: NexusUser = Depends(get_current_user),
    ):
        row = await manager.get(workflow_id)
        if row is None:
            raise HTTPException(status_code=404, detail="workflow not found")
        if user.tenant_id != row.tenant_id and "platform-admin" not in (user.roles or []):
            raise HTTPException(status_code=403, detail="tenant mismatch")
        # Project the DAG-tracked sets + plan step order so polling
        # consumers (platform-api Phase A fallback) don't have to
        # reverse-engineer status from step_history (which can have
        # stale rows when parallel steps interleave).
        checkpoint = row.checkpoint or {}
        dag_completed = list(checkpoint.get("__dag_completed_steps__") or [])
        dag_in_flight = list(checkpoint.get("__dag_in_flight_steps__") or [])
        plan_steps = []
        for s in (row.plan or {}).get("steps") or []:
            plan_steps.append({
                "name": s.get("name", ""),
                "engine": s.get("engine", ""),
            })
        # Phase 12 DAG: artifact_id is stamped into the checkpoint by
        # the orchestrator's `/process` endpoint pre-allocation step,
        # and re-stamped by `spine.persist_minimal_artifact`. Pull from
        # either location so the UI always sees the canonical id.
        artifact_id = (
            checkpoint.get("artifact_id")
            or checkpoint.get("canonical_artifact_id")
        )
        # On stale workflows where the completion path didn't clear the
        # earlier per-step error, surface a cleared error so the UI
        # doesn't render a green-status + red-error mix.
        surfaced_error = row.error
        if row.status == WorkflowStatus.COMPLETED.value and surfaced_error:
            surfaced_error = None
        return WorkflowStatusResponse(
            workflow_id=row.workflow_id,
            kind=row.kind,
            status=row.status,
            current_step=row.current_step,
            step_index=row.step_index,
            attempt=row.attempt,
            error=surfaced_error,
            deadline_at=row.deadline_at.isoformat(),
            created_at=row.created_at.isoformat(),
            completed_at=row.completed_at.isoformat() if row.completed_at else None,
            dag_completed_steps=dag_completed,
            dag_in_flight_steps=dag_in_flight,
            plan_steps=plan_steps,
            artifact_id=artifact_id,
        )

    @router.get("/{workflow_id}/history")
    async def get_history(
        workflow_id: str,
        user: NexusUser = Depends(get_current_user),
    ):
        row = await manager.get(workflow_id)
        if row is None:
            raise HTTPException(status_code=404, detail="workflow not found")
        if user.tenant_id != row.tenant_id and "platform-admin" not in (user.roles or []):
            raise HTTPException(status_code=403, detail="tenant mismatch")
        history = await manager.history(workflow_id)
        return {
            "workflow_id": workflow_id,
            "steps": [
                {
                    "step_name": h.step_name,
                    "engine": h.engine,
                    "attempt": h.attempt,
                    "status": h.status,
                    "started_at": h.started_at.isoformat(),
                    "completed_at": h.completed_at.isoformat() if h.completed_at else None,
                    "duration_ms": h.duration_ms,
                    "error": h.error,
                }
                for h in history
            ],
        }

    @router.post("/{workflow_id}/heartbeat", status_code=204)
    async def heartbeat(
        workflow_id: str,
        user: NexusUser = Depends(get_current_user),
    ):
        # Heartbeats come from worker pods. The tenant guard is relaxed
        # here because workers run with a platform-scope token; we still
        # require an authenticated request.
        try:
            await manager.heartbeat(workflow_id)
        except Exception as e:
            logger.warning("workflow.heartbeat_failed wf=%s err=%s", workflow_id, e)
            raise HTTPException(status_code=500, detail="heartbeat failed")
        return None

    @router.post("/{workflow_id}/steps/{step_name}/result", status_code=202)
    async def record_step_result(
        workflow_id: str,
        step_name: str,
        req: StepResultRequest = Body(...),
        user: NexusUser = Depends(get_current_user),
    ):
        result = StepResult(
            workflow_id=workflow_id,
            step_name=step_name,
            success=req.success,
            checkpoint=req.checkpoint,
            error=req.error,
            error_context=req.error_context,
            duration_ms=req.duration_ms,
            fatal=req.fatal,
        )
        try:
            # Phase 12: DAG-aware result handling. Backwards compatible
            # with linear plans — they have a single-step ready set and
            # behave identically to the old `record_result` path.
            await manager.record_result_dag(result)
        except WorkflowNotFound:
            raise HTTPException(status_code=404, detail="workflow not found")
        except Exception as e:
            logger.error("workflow.record_result_failed wf=%s err=%s", workflow_id, e)
            raise HTTPException(status_code=500, detail="record failed")

        # If the step succeeded AND the workflow has more steps, dispatch
        # the next one synchronously. Stateless engines may have already
        # exited by the time the orchestrator processes this callback.
        if req.success:
            try:
                await dispatcher.dispatch_next(workflow_id)
            except Exception as e:
                logger.warning("workflow.dispatch_next_failed wf=%s err=%s",
                               workflow_id, e)
        else:
            # Soft failure: re-dispatch the same step (the retry path).
            row = await manager.get(workflow_id)
            if row is not None and row.status == "pending":
                try:
                    await dispatcher.dispatch_next(workflow_id)
                except Exception as e:
                    logger.warning("workflow.dispatch_retry_failed wf=%s err=%s",
                                   workflow_id, e)
        return {"ok": True}

    @router.post("/{workflow_id}/cancel", status_code=200)
    async def cancel_workflow(
        workflow_id: str,
        reason: str = Body(..., embed=True),
        user: NexusUser = Depends(get_current_user),
    ):
        row = await manager.get(workflow_id)
        if row is None:
            raise HTTPException(status_code=404, detail="workflow not found")
        if user.tenant_id != row.tenant_id and "platform-admin" not in (user.roles or []):
            raise HTTPException(status_code=403, detail="tenant mismatch")
        try:
            await manager.cancel(workflow_id, reason)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"cancel failed: {e}")
        return {"workflow_id": workflow_id, "status": "cancelled"}

    # ─── SSE: stream status updates ────────────────────────────
    # Replaces the polling pattern that 100+ concurrent clients each
    # waiting on /workflows/{id} would otherwise hit. The server polls
    # the DB at a slow cadence (default 2s) and pushes events when the
    # row's (status, current_step, step_index) tuple changes. The
    # client connection naturally closes when the workflow reaches a
    # terminal state.
    @router.get("/{workflow_id}/stream")
    async def stream_workflow(
        workflow_id: str,
        user: NexusUser = Depends(get_current_user),
    ):
        from fastapi.responses import StreamingResponse
        import asyncio
        import json as _json

        row = await manager.get(workflow_id)
        if row is None:
            raise HTTPException(status_code=404, detail="workflow not found")
        if user.tenant_id != row.tenant_id and "platform-admin" not in (user.roles or []):
            raise HTTPException(status_code=403, detail="tenant mismatch")

        # Status vocabulary translation: the workflow_state plane uses
        # `quarantined` for dead-letter; the UI status type only knows
        # `failed`. Map at the edge so SSE consumers don't have to.
        _ui_status_map = {
            WorkflowStatus.PENDING.value: "running",
            WorkflowStatus.RUNNING.value: "running",
            WorkflowStatus.COMPLETED.value: "completed",
            WorkflowStatus.FAILED.value: "failed",
            WorkflowStatus.CANCELLED.value: "cancelled",
            WorkflowStatus.QUARANTINED.value: "failed",
        }

        async def _event_stream():
            last_signature: Optional[tuple] = None
            terminal = {
                WorkflowStatus.COMPLETED.value,
                WorkflowStatus.FAILED.value,
                WorkflowStatus.CANCELLED.value,
                WorkflowStatus.QUARANTINED.value,
            }
            poll_seconds = 2.0
            max_iterations = int(60 * 30 / poll_seconds)  # 30min cap — multimodal videos can be slow
            # Cache the plan step list once so every tick can compute
            # progress + per-stage info without re-parsing JSON.
            from nexus_sdk.workflows.manager import _steps_from_plan
            plan_steps = _steps_from_plan(row.plan)
            step_names_in_order = [s.name for s in plan_steps]

            for _ in range(max_iterations):
                row_now = await manager.get(workflow_id)
                if row_now is None:
                    yield "event: error\ndata: workflow disappeared\n\n"
                    return
                signature = (
                    row_now.status,
                    row_now.current_step,
                    row_now.step_index,
                    row_now.attempt,
                )
                if signature != last_signature:
                    # Build the stages list the client renders as a
                    # progress widget. Order matches the plan; status
                    # comes from the DAG completed-set tracked in the
                    # checkpoint (see manager._set_dag_completed).
                    completed = set(
                        (row_now.checkpoint or {}).get(
                            "__dag_completed_steps__"
                        ) or []
                    )
                    in_flight = set(
                        (row_now.checkpoint or {}).get(
                            "__dag_in_flight_steps__"
                        ) or []
                    )
                    stages_payload = []
                    for name in step_names_in_order:
                        if name in completed:
                            st = "completed"
                        elif name in in_flight or name == row_now.current_step:
                            st = "running"
                        else:
                            st = "pending"
                        stages_payload.append({
                            "stage_id": name.split(".", 1)[-1] if "." in name else name,
                            "step_name": name,
                            "status": st,
                        })
                    completed_n = sum(
                        1 for s in stages_payload if s["status"] == "completed"
                    )
                    total_n = max(len(stages_payload), 1)
                    progress_percent = int(round(100.0 * completed_n / total_n))
                    # current_stage must be non-empty so the client's
                    # `event.current_stage || prev` fallback doesn't
                    # stick at the initial "starting" value. Pick the
                    # first running (in-flight) step; if nothing is
                    # in-flight, pick the first pending step; if the
                    # whole plan is done, say "completed".
                    current_step = row_now.current_step or ""
                    if not current_step:
                        running_step = next(
                            (s["step_name"] for s in stages_payload if s["status"] == "running"),
                            "",
                        )
                        if running_step:
                            current_step = running_step
                        else:
                            pending_step = next(
                                (s["step_name"] for s in stages_payload if s["status"] == "pending"),
                                "",
                            )
                            current_step = pending_step or "completed"
                    current_stage = (
                        current_step.split(".", 1)[-1]
                        if "." in current_step else current_step
                    )
                    payload = {
                        "workflow_id": row_now.workflow_id,
                        # Client's WorkflowProgressStatus only knows
                        # running / completed / failed / cancelled — map
                        # `quarantined` and `pending` accordingly.
                        "status": _ui_status_map.get(
                            row_now.status, row_now.status,
                        ),
                        "current_stage": current_stage,
                        "current_step": row_now.current_step,
                        "step_index": row_now.step_index,
                        "attempt": row_now.attempt,
                        "progress_percent": progress_percent,
                        "stages": stages_payload,
                        "error": row_now.error,
                    }
                    yield f"event: progress\ndata: {_json.dumps(payload)}\n\n"
                    last_signature = signature
                if row_now.status in terminal:
                    # Emit a final `complete` event so the client's
                    # `complete` handler fires — it matches the legacy
                    # orchestrator's event name and toggles the UI to
                    # the terminal state. Use a deterministic non-empty
                    # current_stage so the client's `|| previous` fallback
                    # doesn't stick at "starting".
                    terminal_stage = (
                        "completed" if row_now.status == WorkflowStatus.COMPLETED.value
                        else ("cancelled" if row_now.status == WorkflowStatus.CANCELLED.value
                              else "failed")
                    )
                    if row_now.current_step and "." in row_now.current_step:
                        terminal_stage = row_now.current_step.split(".", 1)[-1]
                    final_payload = {
                        "workflow_id": row_now.workflow_id,
                        "status": _ui_status_map.get(
                            row_now.status, row_now.status,
                        ),
                        "progress_percent": (
                            100 if row_now.status == WorkflowStatus.COMPLETED.value
                            else 0
                        ),
                        "current_stage": terminal_stage,
                        "error": row_now.error,
                    }
                    yield f"event: complete\ndata: {_json.dumps(final_payload)}\n\n"
                    return
                await asyncio.sleep(poll_seconds)
            yield "event: timeout\ndata: stream ceiling reached\n\n"

        return StreamingResponse(
            _event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",  # disable nginx proxy buffering
            },
        )

    return router
