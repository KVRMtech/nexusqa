"""
Workflow sweeper — orchestrator background tasks.

Runs three loops concurrently:
  1. deadline_loop      — cancels workflows past their workflow-level
                          deadline (the user-facing SLO).
  2. orphan_loop        — re-dispatches workflows whose worker died
                          mid-step (last_heartbeat older than threshold).
  3. dlq_audit_loop     — emits metrics on DLQ depth per queue lane.

All three are designed to be idempotent and lossless: if the sweeper
itself crashes mid-tick, the next tick re-evaluates the same rows.
WorkflowManager's SELECT … FOR UPDATE locks prevent two sweeper
instances (HA orchestrator deployment) from acting on the same row.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Optional

from nexus_sdk.workflows.dispatch import WorkflowDispatcher
from nexus_sdk.workflows.manager import WorkflowManager
from nexus_sdk.workflows.models import WorkflowStatus

logger = logging.getLogger(__name__)


@dataclass
class SweeperConfig:
    # How often each loop ticks (seconds).
    deadline_tick_seconds: float = 10.0
    orphan_tick_seconds: float = 15.0
    dlq_tick_seconds: float = 60.0
    # Heartbeat staleness threshold above which a RUNNING workflow is
    # considered orphaned. Must be > the typical step duration; default
    # 2 min matches the slowest single step in canonical plans.
    orphan_threshold_seconds: int = 120
    # Per-tick batch size — bounded so a backlog never starves the loop.
    batch_size: int = 200
    # Hook called when a workflow is quarantined or cancelled by the
    # sweeper. Pass a coroutine that emits to your event bus.
    on_quarantine: Optional[callable] = None


class WorkflowSweeper:
    """Owns the three sweeper loops."""

    def __init__(
        self,
        manager: WorkflowManager,
        dispatcher: WorkflowDispatcher,
        config: Optional[SweeperConfig] = None,
        metrics=None,
    ) -> None:
        self._manager = manager
        self._dispatcher = dispatcher
        self._cfg = config or SweeperConfig()
        self._running = False
        self._tasks: list[asyncio.Task] = []
        self._metrics = metrics
        # Prometheus counters
        self._m_deadline_cancelled = None
        self._m_orphans_recovered = None
        self._m_dlq_depth = None

    # ─── Lifecycle ───────────────────────────────────────────────

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._init_metrics()
        self._tasks = [
            asyncio.create_task(self._deadline_loop(), name="sweeper.deadline"),
            asyncio.create_task(self._orphan_loop(), name="sweeper.orphan"),
            asyncio.create_task(self._dlq_audit_loop(), name="sweeper.dlq"),
        ]
        logger.info(
            "workflow_sweeper.started deadline_tick=%.1fs orphan_tick=%.1fs orphan_threshold=%ds",
            self._cfg.deadline_tick_seconds,
            self._cfg.orphan_tick_seconds,
            self._cfg.orphan_threshold_seconds,
        )

    async def stop(self) -> None:
        self._running = False
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks = []
        logger.info("workflow_sweeper.stopped")

    # ─── Loop bodies ────────────────────────────────────────────

    async def _deadline_loop(self) -> None:
        while self._running:
            try:
                await self._tick_deadlines()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("sweeper.deadline.error err=%s", e)
            await asyncio.sleep(self._cfg.deadline_tick_seconds)

    async def _orphan_loop(self) -> None:
        while self._running:
            try:
                await self._tick_orphans()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("sweeper.orphan.error err=%s", e)
            await asyncio.sleep(self._cfg.orphan_tick_seconds)

    async def _dlq_audit_loop(self) -> None:
        while self._running:
            try:
                await self._tick_dlq()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug("sweeper.dlq.error err=%s", e)
            # Phase 4 — emit stuck-workflow gauge on the same cadence so
            # the SLO alert (RUNNING + stale heartbeat) fires within a
            # minute of the threshold being crossed.
            try:
                await self._tick_stuck()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug("sweeper.stuck.error err=%s", e)
            await asyncio.sleep(self._cfg.dlq_tick_seconds)

    # ─── Tick implementations ───────────────────────────────────

    async def _tick_deadlines(self) -> None:
        overdue = await self._manager.find_overdue(limit=self._cfg.batch_size)
        if not overdue:
            return
        for row in overdue:
            try:
                await self._manager.quarantine(
                    row.workflow_id,
                    reason="workflow-level deadline exceeded",
                    context={
                        "deadline_at": row.deadline_at.isoformat(),
                        "current_step": row.current_step,
                        "step_index": row.step_index,
                        "attempt": row.attempt,
                    },
                )
                if self._m_deadline_cancelled:
                    self._m_deadline_cancelled.labels(
                        kind=row.kind, status=row.status,
                    ).inc()
                if self._cfg.on_quarantine:
                    await self._cfg.on_quarantine(row)
            except Exception as e:
                logger.warning(
                    "sweeper.deadline.quarantine_failed wf=%s err=%s",
                    row.workflow_id, e,
                )
        logger.warning(
            "sweeper.deadline.quarantined count=%d", len(overdue),
        )

    async def _tick_orphans(self) -> None:
        recovered = await self._dispatcher.recover_orphans(
            max_idle_seconds=self._cfg.orphan_threshold_seconds,
        )
        if recovered and self._m_orphans_recovered:
            self._m_orphans_recovered.inc(recovered)

    async def _tick_dlq(self) -> None:
        # Best-effort DLQ inventory. The queue layer's DLQ key naming
        # convention is nexus:queue:<lane>:dlq. We don't enumerate
        # every lane in the SDK; expose a hook so the orchestrator can
        # plug in its known lanes.
        if not self._metrics:
            return
        # Caller-supplied function: returns list[(lane, depth)].
        get_dlq_depths = getattr(self._metrics, "get_dlq_depths", None)
        if not callable(get_dlq_depths):
            return
        for lane, depth in await get_dlq_depths():
            if self._m_dlq_depth:
                self._m_dlq_depth.labels(lane=lane).set(depth)

        # Phase 4: also emit queue health metrics (depth, pending,
        # oldest pending age) so the SLO alerts can fire on a stuck
        # queue. The metrics provider exposes an optional hook
        # `get_queue_health()` that returns [(lane, depth, pending,
        # oldest_pending_age_s), ...].
        get_queue_health = getattr(self._metrics, "get_queue_health", None)
        if not callable(get_queue_health):
            return
        try:
            from nexus_sdk.workflows.metrics import get_workflow_metrics
            wfm = get_workflow_metrics()
            for lane, depth, pending, oldest_age in await get_queue_health():
                wfm.set_queue_health(
                    lane=lane,
                    depth=int(depth or 0),
                    pending=int(pending or 0),
                    oldest_pending_age_s=float(oldest_age or 0.0),
                )
        except Exception as e:
            logger.debug("sweeper.queue_health.failed err=%s", e)

    async def _tick_stuck(self) -> None:
        """Phase 4: emit count of stuck workflows (running + stale
        heartbeat beyond orphan threshold). Alert when > 0 for >5min."""
        try:
            stale = await self._manager.find_stale_heartbeat(
                max_idle_seconds=self._cfg.orphan_threshold_seconds,
                limit=500,
            )
            from nexus_sdk.workflows.metrics import get_workflow_metrics
            get_workflow_metrics().set_stuck_count(len(stale))
        except Exception as e:
            logger.debug("sweeper.stuck.failed err=%s", e)

    # ─── Metrics ────────────────────────────────────────────────

    def _init_metrics(self) -> None:
        if self._metrics is None:
            return
        try:
            # Sweeper metrics describe workflow-state, not per-service
            # HTTP traffic — pass with_service=False so the auto-prepend
            # doesn't silently break .labels(lane=...) at tick time.
            self._m_deadline_cancelled = self._metrics.custom_counter(
                "workflow_deadline_quarantined_total",
                "Workflows quarantined because the workflow-level deadline elapsed.",
                labels=["kind", "status"],
                with_service=False,
            )
            self._m_orphans_recovered = self._metrics.custom_counter(
                "workflow_orphans_recovered_total",
                "Workflows re-dispatched after detecting a stale heartbeat.",
                with_service=False,
            )
            self._m_dlq_depth = self._metrics.custom_gauge(
                "workflow_dlq_depth",
                "DLQ depth per queue lane.",
                labels=["lane"],
                with_service=False,
            )
        except Exception as e:
            logger.warning("sweeper.metrics_init_failed err=%s", e)
