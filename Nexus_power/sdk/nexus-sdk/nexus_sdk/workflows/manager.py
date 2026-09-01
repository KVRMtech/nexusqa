"""
WorkflowManager — orchestrator-side controller for canonical workflows.

Owns:
  - Plan persistence
  - Checkpoint commits (transactional alongside the step transition)
  - Deadline / heartbeat enforcement (via the sweeper module)
  - Retry / quarantine policy

Workers never touch this class directly. The orchestrator calls
WorkflowManager methods between dispatching steps.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from nexus_sdk.workflows.db_models import (
    WorkflowStateRow,
    WorkflowStepHistoryRow,
)
from nexus_sdk.workflows.models import (
    JobEnvelope,
    StepPlan,
    StepKind,
    StepResult,
    WorkflowPlan,
    WorkflowStatus,
)

logger = logging.getLogger(__name__)


class WorkflowNotFound(Exception):
    pass


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _plan_to_dict(plan: WorkflowPlan) -> dict[str, Any]:
    return {
        "kind": plan.kind.value,
        "tenant_id": plan.tenant_id,
        "session_id": plan.session_id,
        "deadline_seconds": plan.deadline_seconds,
        "version": plan.version,
        "metadata": dict(plan.metadata),
        "steps": [
            {
                "name": s.name,
                "engine": s.engine,
                "kind": s.kind.value,
                "deadline_seconds": s.deadline_seconds,
                "max_attempts": s.max_attempts,
                "params": dict(s.params),
                "depends_on": list(s.depends_on),
            }
            for s in plan.steps
        ],
    }


def _steps_from_plan(d: dict[str, Any]) -> list[StepPlan]:
    return [
        StepPlan(
            name=s["name"],
            engine=s["engine"],
            kind=StepKind(s.get("kind", "cpu")),
            deadline_seconds=int(s.get("deadline_seconds", 300)),
            max_attempts=int(s.get("max_attempts", 3)),
            params=dict(s.get("params", {})),
            depends_on=list(s.get("depends_on") or []),
        )
        for s in d.get("steps", [])
    ]


# Special checkpoint key the DAG dispatcher uses to track which steps
# have already completed. The handlers don't touch it; the dispatcher
# manages it. Using a checkpoint key (vs a new DB column) means no
# migration is needed for Phase 12. The key is namespaced to avoid
# collision with any plausible handler-defined key.
_DAG_COMPLETED_KEY = "__dag_completed_steps__"
_DAG_IN_FLIGHT_KEY = "__dag_in_flight_steps__"
# Per-step attempt counter. Stored as {step_name: int}. Tracks how many
# times this *specific* step has been dispatched, which is what the
# retry-budget check needs — the legacy `row.attempt` field is a
# workflow-global counter that inflates across parallel branches in
# DAG plans and was causing single-failure quarantines. See
# `record_result_dag` for the gate that consults this dict.
_DAG_STEP_ATTEMPTS_KEY = "__dag_step_attempts__"


def _dag_completed(row: WorkflowStateRow) -> set[str]:
    val = (row.checkpoint or {}).get(_DAG_COMPLETED_KEY) or []
    return set(val) if isinstance(val, list) else set()


def _dag_in_flight(row: WorkflowStateRow) -> set[str]:
    val = (row.checkpoint or {}).get(_DAG_IN_FLIGHT_KEY) or []
    return set(val) if isinstance(val, list) else set()


def _dag_step_attempts(row: WorkflowStateRow) -> dict[str, int]:
    """Return a mutable copy of the per-step attempt map."""
    val = (row.checkpoint or {}).get(_DAG_STEP_ATTEMPTS_KEY) or {}
    if not isinstance(val, dict):
        return {}
    # Defensive: coerce values to int in case the DB rehydrate produced strings.
    return {str(k): int(v) for k, v in val.items()}


def _set_dag_completed(row: WorkflowStateRow, names: set[str]) -> None:
    ck = dict(row.checkpoint or {})
    ck[_DAG_COMPLETED_KEY] = sorted(names)
    row.checkpoint = ck


def _set_dag_in_flight(row: WorkflowStateRow, names: set[str]) -> None:
    ck = dict(row.checkpoint or {})
    ck[_DAG_IN_FLIGHT_KEY] = sorted(names)
    row.checkpoint = ck


def _set_dag_step_attempts(row: WorkflowStateRow, attempts: dict[str, int]) -> None:
    ck = dict(row.checkpoint or {})
    ck[_DAG_STEP_ATTEMPTS_KEY] = {k: int(v) for k, v in attempts.items()}
    row.checkpoint = ck


class _LightweightPlan:
    """Minimal duck-type that nexus_sdk.workflows.dag's helpers accept.

    The full WorkflowPlan does cycle-checking + post-init validation
    that's already-passed at submission time. When we rehydrate a plan
    from the persisted DB row, we just need an object with `.steps`.
    """

    __slots__ = ("steps",)

    def __init__(self, steps: list[StepPlan]) -> None:
        self.steps = steps


class WorkflowManager:
    """High-level API the orchestrator uses to drive workflows."""

    def __init__(self, db) -> None:
        self._db = db

    # ─── Creation ─────────────────────────────────────────────────

    async def create(self, plan: WorkflowPlan) -> str:
        """Persist a new workflow in PENDING. Returns workflow_id."""
        wf_id = str(uuid.uuid4())
        now = _utc_now()
        deadline = now + timedelta(seconds=plan.deadline_seconds)

        max_attempts = max((s.max_attempts for s in plan.steps), default=3)

        # Phase 2: plan versioning. Persist the plan version + a
        # content hash in metadata so operators can tell at a glance
        # whether a workflow's plan matches the current code or is
        # frozen at an older shape. Replay logic already uses the
        # persisted plan dict (not the current factory) so old
        # workflows are always replayed with their original plan —
        # this just makes that visible.
        import hashlib, json as _json
        plan_dict = _plan_to_dict(plan)
        plan_hash = hashlib.sha256(
            _json.dumps(plan_dict, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]
        meta = dict(plan.metadata) if plan.metadata else {}
        meta["plan_version"] = plan.version
        meta["plan_hash"] = plan_hash
        meta["plan_steps_count"] = len(plan.steps)

        async with self._db.session() as s:
            row = WorkflowStateRow(
                workflow_id=wf_id,
                tenant_id=plan.tenant_id,
                session_id=plan.session_id,
                kind=plan.kind.value,
                status=WorkflowStatus.PENDING.value,
                plan=plan_dict,
                checkpoint=dict(plan.initial_state),
                current_step=plan.steps[0].name,
                step_index=0,
                attempt=0,
                max_attempts=max_attempts,
                deadline_at=deadline,
                last_heartbeat=now,
                metadata_json=meta or None,
            )
            s.add(row)
        logger.info(
            "workflow.created wf=%s kind=%s tenant=%s steps=%d deadline_s=%d",
            wf_id, plan.kind.value, plan.tenant_id, len(plan.steps),
            plan.deadline_seconds,
        )
        # Prometheus: track every accepted workflow by kind + tenant class.
        try:
            from nexus_sdk.workflows.metrics import get_workflow_metrics

            get_workflow_metrics().record_created(
                plan.kind.value, plan.tenant_id,
            )
        except Exception:
            pass
        return wf_id

    # ─── Step dispatch (orchestrator side) ────────────────────────

    async def next_envelope(self, workflow_id: str) -> Optional[JobEnvelope]:
        """
        Build a JobEnvelope for the workflow's current step. Returns None
        when the workflow has no more runnable steps (completed, failed,
        cancelled, or quarantined).
        """
        async with self._db.session() as s:
            row = await self._lock_row(s, workflow_id)
            if row is None:
                raise WorkflowNotFound(workflow_id)
            if row.status not in {WorkflowStatus.PENDING.value, WorkflowStatus.RUNNING.value}:
                return None
            steps = _steps_from_plan(row.plan)
            if row.step_index >= len(steps):
                return None
            current = steps[row.step_index]

            # Bump attempt counter + flip to running.
            row.status = WorkflowStatus.RUNNING.value
            row.attempt += 1
            row.current_step = current.name
            row.last_heartbeat = _utc_now()
            # Per-step deadline is min(now + step_deadline, workflow_deadline)
            step_deadline = _utc_now() + timedelta(seconds=current.deadline_seconds)
            envelope_deadline = min(step_deadline, row.deadline_at)

            # Append a started history row.
            s.add(WorkflowStepHistoryRow(
                workflow_id=workflow_id,
                step_name=current.name,
                engine=current.engine,
                attempt=row.attempt,
                status="started",
                started_at=_utc_now(),
                payload={"checkpoint_keys": sorted(row.checkpoint.keys())},
            ))

            return JobEnvelope(
                workflow_id=row.workflow_id,
                step_name=current.name,
                step_index=row.step_index,
                attempt=row.attempt,
                tenant_id=row.tenant_id,
                session_id=row.session_id,
                deadline_at_epoch=envelope_deadline.timestamp(),
                heartbeat_seconds=30,
                params=dict(current.params),
                checkpoint=dict(row.checkpoint),
            )

    # ─── Result handling ──────────────────────────────────────────

    async def record_result(self, result: StepResult) -> None:
        """
        Persist a worker's StepResult and advance / fail the workflow.

        Transactional: the checkpoint update, status transition, and
        step-history close-out all commit together. A crash between
        steps therefore never produces a partial transition.
        """
        async with self._db.session() as s:
            row = await self._lock_row(s, result.workflow_id)
            if row is None:
                raise WorkflowNotFound(result.workflow_id)
            steps = _steps_from_plan(row.plan)
            if row.step_index >= len(steps):
                logger.warning(
                    "workflow.result_for_completed_step wf=%s step=%s",
                    result.workflow_id, result.step_name,
                )
                return
            current = steps[row.step_index]
            if current.name != result.step_name:
                logger.warning(
                    "workflow.step_mismatch wf=%s expected=%s got=%s",
                    result.workflow_id, current.name, result.step_name,
                )
                return

            # Close out the latest history row for this attempt.
            last_history = await self._latest_history_row(
                s, row.workflow_id, step_name=result.step_name,
            )
            if last_history is not None:
                last_history.completed_at = _utc_now()
                last_history.duration_ms = result.duration_ms
                last_history.status = "completed" if result.success else "failed"
                last_history.error = None if result.success else result.error

            now = _utc_now()
            row.last_heartbeat = now

            # Capture metric values BEFORE returning so each branch
            # observes a single point. Cardinality bounded by engine ×
            # step name (small, fixed by canonical plans).
            try:
                from nexus_sdk.workflows.metrics import get_workflow_metrics

                _wfm = get_workflow_metrics()
            except Exception:
                _wfm = None

            if result.success:
                row.checkpoint = dict(result.checkpoint or {})
                row.step_index += 1
                row.attempt = 0
                if row.step_index >= len(steps):
                    row.status = WorkflowStatus.COMPLETED.value
                    row.current_step = None
                    row.completed_at = now
                else:
                    row.current_step = steps[row.step_index].name
                logger.info(
                    "workflow.step_completed wf=%s step=%s next=%s done=%s",
                    row.workflow_id, current.name, row.current_step,
                    row.status == WorkflowStatus.COMPLETED.value,
                )
                if _wfm:
                    _wfm.record_step(
                        engine=current.engine, step=current.name,
                        status="completed",
                        duration_ms=result.duration_ms,
                        attempts=max(1, row.attempt or 1),
                    )
                    if row.status == WorkflowStatus.COMPLETED.value:
                        dur = (row.completed_at - row.created_at).total_seconds()
                        _wfm.record_terminal(
                            row.kind, "success",
                            duration_seconds=dur,
                            tenant_id=row.tenant_id,
                        )
                return

            # Failure path.
            if result.fatal or row.attempt >= current.max_attempts:
                row.status = WorkflowStatus.QUARANTINED.value
                row.error = result.error
                row.error_context = result.error_context
                row.completed_at = now
                logger.error(
                    "workflow.quarantined wf=%s step=%s attempt=%d max=%d fatal=%s",
                    row.workflow_id, current.name, row.attempt,
                    current.max_attempts, result.fatal,
                )
                if _wfm:
                    _wfm.record_step(
                        engine=current.engine, step=current.name,
                        status="quarantined",
                        duration_ms=result.duration_ms,
                        attempts=row.attempt,
                    )
                    dur = (row.completed_at - row.created_at).total_seconds()
                    _wfm.record_terminal(
                        row.kind, "quarantined",
                        duration_seconds=dur,
                        tenant_id=row.tenant_id,
                    )
                return
            # Soft failure → leave status running; next dispatch retries.
            row.status = WorkflowStatus.PENDING.value
            row.error = result.error
            row.error_context = result.error_context
            logger.warning(
                "workflow.step_retry wf=%s step=%s attempt=%d/%d",
                row.workflow_id, current.name, row.attempt,
                current.max_attempts,
            )
            if _wfm:
                _wfm.record_step(
                    engine=current.engine, step=current.name,
                    status="retry",
                    duration_ms=result.duration_ms,
                    attempts=row.attempt,
                )

    # ─── Heartbeats ──────────────────────────────────────────────

    async def heartbeat(self, workflow_id: str) -> None:
        async with self._db.session() as s:
            await s.execute(
                update(WorkflowStateRow)
                .where(WorkflowStateRow.workflow_id == workflow_id)
                .values(last_heartbeat=_utc_now())
            )

    async def clear_in_flight(self, workflow_id: str) -> None:
        """Clear the DAG `__dag_in_flight_steps__` set for a workflow.

        Used by the orphan-recovery sweeper to release abandoned step
        claims so the next dispatch can re-emit them. Without this,
        a worker that died mid-step leaves its step "in_flight"
        forever and `next_ready_envelopes` refuses to dispatch it.
        """
        async with self._db.session() as s:
            row = await self._lock_row(s, workflow_id)
            if row is None:
                return
            _set_dag_in_flight(row, set())

    # ─── Phase 12: DAG dispatch ─────────────────────────────────
    # When a plan uses `depends_on`, the linear `step_index` becomes
    # ambiguous (multiple steps may be ready at once). These methods
    # use the set of completed step names (stored in checkpoint under
    # _DAG_COMPLETED_KEY) and the dag.ready_steps() algorithm to drive
    # the workflow.
    #
    # Linear plans automatically work — their depends_on chain reduces
    # to one ready step at a time, identical to the legacy behavior.

    async def next_ready_envelopes(
        self, workflow_id: str,
    ) -> list[JobEnvelope]:
        """Return ALL JobEnvelopes whose dependencies are satisfied.

        Linear plans → list with 0-1 element (same as the legacy
        `next_envelope`). DAG plans with parallel branches → list with
        N elements, one per ready step.

        Empty list means: nothing to do (workflow complete, in flight,
        or in terminal state).
        """
        from nexus_sdk.workflows.dag import ready_steps

        async with self._db.session() as s:
            row = await self._lock_row(s, workflow_id)
            if row is None:
                raise WorkflowNotFound(workflow_id)
            if row.status not in {
                WorkflowStatus.PENDING.value, WorkflowStatus.RUNNING.value
            }:
                return []

            steps = _steps_from_plan(row.plan)
            plan_for_dag = _LightweightPlan(steps)
            completed = _dag_completed(row)
            in_flight = _dag_in_flight(row)
            ready_names = ready_steps(
                plan_for_dag,
                completed=completed,
                in_flight=in_flight,
            )
            if not ready_names:
                return []

            row.status = WorkflowStatus.RUNNING.value
            row.last_heartbeat = _utc_now()
            envelopes: list[JobEnvelope] = []
            new_in_flight = set(in_flight)
            # Per-step attempt counter — the retry budget is per-step,
            # not workflow-global. See `record_result_dag` for the gate.
            step_attempts = _dag_step_attempts(row)
            for name in ready_names:
                step = next(st for st in steps if st.name == name)
                step_attempts[name] = int(step_attempts.get(name, 0)) + 1
                this_step_attempt = step_attempts[name]
                # Keep `row.attempt` updated for legacy observability
                # (history rows, dashboards). It is no longer used as
                # the retry-budget gate in DAG mode.
                row.attempt += 1
                step_deadline = _utc_now() + timedelta(seconds=step.deadline_seconds)
                envelope_deadline = min(step_deadline, row.deadline_at)
                # Update legacy step_index to point at the FIRST ready
                # step so legacy code paths (sweeper, DLQ replay) still
                # work in mixed-mode deploys.
                row.current_step = name
                try:
                    row.step_index = steps.index(step)
                except ValueError:
                    pass

                s.add(WorkflowStepHistoryRow(
                    workflow_id=workflow_id,
                    step_name=step.name,
                    engine=step.engine,
                    attempt=this_step_attempt,
                    status="started",
                    started_at=_utc_now(),
                    payload={"checkpoint_keys": sorted(row.checkpoint.keys())},
                ))

                # Filter DAG-internal keys out of the envelope checkpoint
                # so handlers never see them.
                clean_ckpt = {
                    k: v for k, v in (row.checkpoint or {}).items()
                    if not k.startswith("__dag_")
                }
                envelopes.append(JobEnvelope(
                    workflow_id=row.workflow_id,
                    step_name=step.name,
                    step_index=row.step_index,
                    attempt=this_step_attempt,
                    tenant_id=row.tenant_id,
                    session_id=row.session_id,
                    deadline_at_epoch=envelope_deadline.timestamp(),
                    heartbeat_seconds=30,
                    params=dict(step.params),
                    checkpoint=clean_ckpt,
                ))
                new_in_flight.add(name)

            _set_dag_in_flight(row, new_in_flight)
            _set_dag_step_attempts(row, step_attempts)
            return envelopes

    async def record_result_dag(self, result: StepResult) -> None:
        """DAG-aware result handling. On success, mark the step
        completed and check if any downstream steps are now ready. On
        failure, fall through to the same retry/quarantine policy as
        the linear path.
        """
        from nexus_sdk.workflows.dag import ready_steps, is_complete

        async with self._db.session() as s:
            row = await self._lock_row(s, result.workflow_id)
            if row is None:
                raise WorkflowNotFound(result.workflow_id)
            steps = _steps_from_plan(row.plan)
            step = next((st for st in steps if st.name == result.step_name), None)
            if step is None:
                logger.warning(
                    "workflow.result_for_unknown_step wf=%s step=%s",
                    result.workflow_id, result.step_name,
                )
                return

            last_history = await self._latest_history_row(
                s, row.workflow_id, step_name=result.step_name,
            )
            if last_history is not None:
                last_history.completed_at = _utc_now()
                last_history.duration_ms = result.duration_ms
                last_history.status = "completed" if result.success else "failed"
                last_history.error = None if result.success else result.error

            now = _utc_now()
            row.last_heartbeat = now

            try:
                from nexus_sdk.workflows.metrics import get_workflow_metrics
                _wfm = get_workflow_metrics()
            except Exception:
                _wfm = None

            in_flight = _dag_in_flight(row)
            in_flight.discard(result.step_name)

            if result.success:
                # Merge handler's checkpoint output back in (without
                # clobbering DAG-internal bookkeeping).
                handler_ckpt = dict(result.checkpoint or {})
                merged = dict(row.checkpoint or {})
                merged.update({
                    k: v for k, v in handler_ckpt.items()
                    if not k.startswith("__dag_")
                })
                row.checkpoint = merged

                completed = _dag_completed(row)
                completed.add(result.step_name)
                _set_dag_completed(row, completed)
                _set_dag_in_flight(row, in_flight)

                plan_for_dag = _LightweightPlan(steps)
                if is_complete(plan_for_dag, completed):
                    row.status = WorkflowStatus.COMPLETED.value
                    row.current_step = None
                    row.completed_at = now
                    row.attempt = 0
                    if _wfm:
                        _wfm.record_step(
                            engine=step.engine, step=step.name,
                            status="completed",
                            duration_ms=result.duration_ms,
                            attempts=max(1, row.attempt or 1),
                        )
                        dur = (row.completed_at - row.created_at).total_seconds()
                        _wfm.record_terminal(
                            row.kind, "success",
                            duration_seconds=dur,
                            tenant_id=row.tenant_id,
                        )
                else:
                    # More work to do — pick the first ready step to
                    # surface as `current_step` for UI / dashboards.
                    nxt = ready_steps(
                        plan_for_dag, completed=completed, in_flight=in_flight,
                    )
                    row.current_step = nxt[0] if nxt else None
                    if _wfm:
                        _wfm.record_step(
                            engine=step.engine, step=step.name,
                            status="completed",
                            duration_ms=result.duration_ms,
                            attempts=max(1, row.attempt or 1),
                        )
                return

            # Failure → quarantine on max attempts, else mark for retry.
            # Retry budget is **per-step**, not workflow-global. The
            # workflow-global `row.attempt` inflates fast in DAG plans
            # (every parallel dispatch increments it once) and was
            # causing healthy workflows to quarantine on the first soft
            # failure. Use the per-step counter persisted in the
            # checkpoint under `__dag_step_attempts__`.
            _set_dag_in_flight(row, in_flight)
            step_attempts = _dag_step_attempts(row)
            this_step_attempt = int(step_attempts.get(result.step_name, 0))
            if result.fatal or this_step_attempt >= step.max_attempts:
                row.status = WorkflowStatus.QUARANTINED.value
                row.error = result.error
                row.error_context = result.error_context
                row.completed_at = now
                if _wfm:
                    _wfm.record_step(
                        engine=step.engine, step=step.name,
                        status="quarantined",
                        duration_ms=result.duration_ms,
                        attempts=this_step_attempt,
                    )
                    dur = (row.completed_at - row.created_at).total_seconds()
                    _wfm.record_terminal(
                        row.kind, "quarantined",
                        duration_seconds=dur,
                        tenant_id=row.tenant_id,
                    )
                logger.warning(
                    "workflow.quarantined wf=%s step=%s step_attempt=%d/%d fatal=%s",
                    row.workflow_id, result.step_name, this_step_attempt,
                    step.max_attempts, result.fatal,
                )
                return
            row.status = WorkflowStatus.PENDING.value
            row.error = result.error
            row.error_context = result.error_context
            logger.info(
                "workflow.step_retry wf=%s step=%s step_attempt=%d/%d err=%s",
                row.workflow_id, result.step_name, this_step_attempt,
                step.max_attempts, (result.error or "")[:200],
            )
            if _wfm:
                _wfm.record_step(
                    engine=step.engine, step=step.name,
                    status="retry",
                    duration_ms=result.duration_ms,
                    attempts=row.attempt,
                )

    # ─── Cancellation ────────────────────────────────────────────

    async def cancel(self, workflow_id: str, reason: str) -> None:
        async with self._db.session() as s:
            row = await self._lock_row(s, workflow_id)
            if row is None:
                raise WorkflowNotFound(workflow_id)
            if row.status in {
                WorkflowStatus.COMPLETED.value,
                WorkflowStatus.FAILED.value,
                WorkflowStatus.CANCELLED.value,
            }:
                return
            row.status = WorkflowStatus.CANCELLED.value
            row.error = reason
            row.completed_at = _utc_now()

    # ─── Sweeper helpers ────────────────────────────────────────

    async def find_overdue(
        self, limit: int = 200
    ) -> list[WorkflowStateRow]:
        """Workflows whose workflow-level deadline has passed."""
        async with self._db.session() as s:
            stmt = (
                select(WorkflowStateRow)
                .where(WorkflowStateRow.status.in_(
                    [WorkflowStatus.PENDING.value, WorkflowStatus.RUNNING.value]
                ))
                .where(WorkflowStateRow.deadline_at < _utc_now())
                .limit(limit)
            )
            result = await s.execute(stmt)
            return list(result.scalars().all())

    async def find_stale_heartbeat(
        self, max_idle_seconds: int, limit: int = 200
    ) -> list[WorkflowStateRow]:
        """RUNNING workflows whose last heartbeat is older than threshold."""
        cutoff = _utc_now() - timedelta(seconds=max_idle_seconds)
        async with self._db.session() as s:
            stmt = (
                select(WorkflowStateRow)
                .where(WorkflowStateRow.status == WorkflowStatus.RUNNING.value)
                .where(WorkflowStateRow.last_heartbeat < cutoff)
                .limit(limit)
            )
            result = await s.execute(stmt)
            return list(result.scalars().all())

    async def quarantine(
        self,
        workflow_id: str,
        reason: str,
        context: Optional[dict[str, Any]] = None,
    ) -> None:
        async with self._db.session() as s:
            row = await self._lock_row(s, workflow_id)
            if row is None:
                raise WorkflowNotFound(workflow_id)
            row.status = WorkflowStatus.QUARANTINED.value
            row.error = reason
            row.error_context = context or {}
            row.completed_at = _utc_now()
            logger.error(
                "workflow.quarantined wf=%s reason=%s", workflow_id, reason
            )

    # ─── Read helpers ────────────────────────────────────────────

    async def get(self, workflow_id: str) -> Optional[WorkflowStateRow]:
        async with self._db.session() as s:
            return await s.get(WorkflowStateRow, workflow_id)

    async def history(self, workflow_id: str) -> list[WorkflowStepHistoryRow]:
        async with self._db.session() as s:
            stmt = (
                select(WorkflowStepHistoryRow)
                .where(WorkflowStepHistoryRow.workflow_id == workflow_id)
                .order_by(WorkflowStepHistoryRow.id.asc())
            )
            return list((await s.execute(stmt)).scalars().all())

    # ─── Internals ───────────────────────────────────────────────

    async def _lock_row(
        self, session: AsyncSession, workflow_id: str
    ) -> Optional[WorkflowStateRow]:
        """SELECT ... FOR UPDATE to serialize concurrent transitions."""
        stmt = (
            select(WorkflowStateRow)
            .where(WorkflowStateRow.workflow_id == workflow_id)
            .with_for_update()
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def _latest_history_row(
        self,
        session: AsyncSession,
        workflow_id: str,
        step_name: Optional[str] = None,
    ) -> Optional[WorkflowStepHistoryRow]:
        """Return the most recent `started` history row for the workflow.

        When `step_name` is given (DAG plans), filter to rows for THAT
        step. This is critical for parallel-branch DAG plans: without
        the filter, the "latest started row" might belong to a different
        step that happened to be dispatched moments earlier, and the
        completing step's result gets written onto the wrong row,
        leaving the real step's row stuck in "started" forever. The
        UI's polling endpoint reads step_history directly, so this
        contamination shows up as "0.0s grey" for completed steps.
        """
        stmt = (
            select(WorkflowStepHistoryRow)
            .where(WorkflowStepHistoryRow.workflow_id == workflow_id)
            .where(WorkflowStepHistoryRow.status == "started")
        )
        if step_name is not None:
            stmt = stmt.where(WorkflowStepHistoryRow.step_name == step_name)
        stmt = stmt.order_by(WorkflowStepHistoryRow.id.desc()).limit(1)
        result = await session.execute(stmt)
        return result.scalar_one_or_none()
