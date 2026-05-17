"""
Workflow dispatch — glue between WorkflowManager and JobQueue.

The orchestrator side calls `dispatch_next` to:
  1. Ask WorkflowManager for the next JobEnvelope for a workflow.
  2. Enqueue it on the correct per-engine, per-resource-class queue.

It also exposes `recover_orphans`, which the sweeper uses to find
running workflows whose worker died mid-step and re-dispatches them.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from nexus_sdk.workflows.manager import WorkflowManager
from nexus_sdk.workflows.models import (
    JobEnvelope, StepKind, WorkflowStatus,
    STEP_PRIORITY_STANDARD, STEP_PRIORITY_PRIORITY,
)

logger = logging.getLogger(__name__)


def queue_name(
    engine: str, kind: StepKind, priority: str = STEP_PRIORITY_STANDARD,
) -> str:
    """Canonical stream key for a per-engine per-resource-class lane.

    Phase 6: when `priority == STEP_PRIORITY_PRIORITY`, route to a
    separate `engine.kind.priority` lane so premium-tenant workloads
    can be scaled independently of the standard lane. Existing lanes
    are unchanged when priority is "standard".
    """
    base = f"{engine}.{kind.value}"
    if priority == STEP_PRIORITY_PRIORITY:
        return f"{base}.priority"
    return base


class WorkflowDispatcher:
    """
    Enqueues JobEnvelopes onto the right queue.

    The `queue_factory` produces a JobQueue for the given queue name —
    the orchestrator caches instances so we don't open a new Redis
    connection per dispatch.
    """

    def __init__(self, manager: WorkflowManager, queue_factory) -> None:
        self._manager = manager
        self._queue_factory = queue_factory

    async def dispatch_next(self, workflow_id: str) -> Optional[str]:
        """
        Dispatch the next ready batch of steps.

        Phase 12: a single dispatch call may enqueue multiple
        envelopes if the plan's DAG has parallel branches. Returns
        the message id of the FIRST envelope dispatched (or None when
        the workflow has nothing ready).

        For linear plans, behavior is identical to the legacy
        single-step dispatch: one envelope per call.
        """
        envelopes = await self._manager.next_ready_envelopes(workflow_id)
        if not envelopes:
            return None

        wf = await self._manager.get(workflow_id)
        if wf is None:
            return None
        steps_by_name = {s["name"]: s for s in wf.plan.get("steps", [])}
        # Phase 6: the plan-level priority becomes the default for steps
        # that don't override. WorkflowManager persists plan as a dict so
        # we read the field defensively.
        plan_priority = wf.plan.get("priority", STEP_PRIORITY_STANDARD)

        first_msg_id: Optional[str] = None
        for envelope in envelopes:
            step = steps_by_name.get(envelope.step_name)
            if step is None:
                logger.warning(
                    "workflow.dispatched_unknown_step wf=%s step=%s",
                    workflow_id, envelope.step_name,
                )
                continue
            # Effective priority: step.priority overrides plan.priority.
            step_priority = step.get("priority")
            effective_priority = step_priority or plan_priority
            qname = queue_name(
                step["engine"],
                StepKind(step.get("kind", "cpu")),
                priority=effective_priority,
            )
            queue = await self._queue_factory(qname)
            # Within-lane message priority is still expressed via the
            # JobQueue's `priority` arg (used for Redis stream metadata
            # only; routing is by lane).
            msg_id = await queue.enqueue(
                job_id=workflow_id,
                payload=envelope.to_wire(),
                priority=effective_priority,
            )
            if first_msg_id is None:
                first_msg_id = msg_id
            logger.info(
                "workflow.dispatched wf=%s step=%s queue=%s msg=%s",
                workflow_id, envelope.step_name, qname, msg_id,
            )
        return first_msg_id

    async def recover_orphans(self, max_idle_seconds: int = 120) -> int:
        """
        Re-dispatch workflows whose worker likely died mid-step.

        Selection criteria:
          - status = RUNNING
          - last_heartbeat older than `max_idle_seconds`

        Phase 2 fix: for DAG workflows, also CLEAR the
        `__dag_in_flight_steps__` set before re-dispatching. Without
        that, `next_ready_envelopes` sees the abandoned step as
        in-flight and refuses to dispatch it — the sweeper would
        bump heartbeat in a tight loop without actually unsticking
        anything. After clearing, the next dispatch finds the step
        eligible again and the orchestrator re-enqueues it on the
        right lane (with a fresh envelope deadline).

        Returns the number of workflows redispatched.
        """
        stale = await self._manager.find_stale_heartbeat(
            max_idle_seconds=max_idle_seconds
        )
        count = 0
        for row in stale:
            try:
                # Clear orphaned in-flight steps BEFORE dispatch.
                in_flight = list(
                    (row.checkpoint or {}).get("__dag_in_flight_steps__") or []
                )
                if in_flight:
                    await self._manager.clear_in_flight(row.workflow_id)
                    logger.warning(
                        "workflow.orphan_in_flight_cleared wf=%s steps=%s",
                        row.workflow_id, in_flight,
                    )
                await self._manager.heartbeat(row.workflow_id)
                if await self.dispatch_next(row.workflow_id):
                    count += 1
                    logger.warning(
                        "workflow.orphan_redispatched wf=%s previous_in_flight=%s",
                        row.workflow_id, in_flight,
                    )
            except Exception as e:
                logger.warning(
                    "workflow.recover_failed wf=%s err=%s",
                    row.workflow_id, e,
                )
        if count:
            logger.warning(
                "workflow.orphans_recovered count=%d max_idle_s=%d",
                count, max_idle_seconds,
            )
        return count
