"""
Phase 1 canonical workflow plane — wired into the orchestrator alongside
the legacy chain engine. This module owns:

  - The Postgres-backed WorkflowManager.
  - A per-lane JobQueue factory (cached so we don't open a Redis
    connection per dispatch).
  - The WorkflowDispatcher and WorkflowSweeper instances.
  - The FastAPI routers under /api/v1/workflows and /api/v1/admin/dlq.

The orchestrator's main.py calls `WorkflowPlane.attach(app)` once at
startup and `await plane.aclose()` on shutdown.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Optional

from fastapi import FastAPI

from nexus_sdk.config import PostgresConfig
from nexus_sdk.db import Database
from nexus_sdk.observability.metrics import get_metrics
from nexus_sdk.queue import JobQueue
from nexus_sdk.workflows import (
    StepKind,
    WorkflowDispatcher,
    WorkflowManager,
    queue_name as workflow_queue_name,
)
from nexus_sdk.workflows.models import (
    STEP_PRIORITY_STANDARD, STEP_PRIORITY_PRIORITY,
)
from nexus_sdk.workflows.dlq import build_dlq_router
from nexus_sdk.workflows.http import build_router as build_workflow_router
from nexus_sdk.workflows.sweeper import SweeperConfig, WorkflowSweeper

logger = logging.getLogger(__name__)


# ─── Static lane inventory ──────────────────────────────────────
#
# The canonical plans in nexus_sdk.workflows.plans reference these
# engines. Each engine has a CPU lane by default; only GPU-capable
# engines also expose a GPU lane. Update here when adding a new
# engine to the canonical pipeline.

_LANE_ENGINES_CPU = [
    "shield", "eyes", "ears", "backbone", "legs",
    "spine", "mouth", "nerves", "hands", "brain",
]
_LANE_ENGINES_GPU = ["eyes", "ears", "heart", "spine", "brain"]


def known_lanes() -> list[str]:
    """All lanes the orchestrator dispatches onto + watches for DLQs.

    Phase 6 includes both the `standard` and `priority` variants for
    every CPU/GPU lane so the sweeper monitors queue health on premium
    lanes too. Worker pools for priority lanes are deployed via the
    KEDA-scaled premium-pool charts; this list just makes them visible
    to observability.
    """
    out: list[str] = []
    for prio in (STEP_PRIORITY_STANDARD, STEP_PRIORITY_PRIORITY):
        out.extend(
            workflow_queue_name(e, StepKind.CPU, priority=prio)
            for e in _LANE_ENGINES_CPU
        )
        out.extend(
            workflow_queue_name(e, StepKind.GPU, priority=prio)
            for e in _LANE_ENGINES_GPU
        )
    return out


# ─── Plane container ────────────────────────────────────────────


class WorkflowPlane:
    """Owns the Phase 1 canonical-workflow runtime."""

    def __init__(self, redis_url: str) -> None:
        self._redis_url = redis_url
        self._redis_host, self._redis_port, self._redis_password = (
            _parse_redis_url(redis_url)
        )

        self._db: Optional[Database] = None
        self._manager: Optional[WorkflowManager] = None
        self._dispatcher: Optional[WorkflowDispatcher] = None
        self._sweeper: Optional[WorkflowSweeper] = None
        self._queues: dict[str, JobQueue] = {}
        # Phase 4: a dedicated low-level Redis client used by the
        # sweeper's queue-health hook. Reads XLEN / XPENDING / XINFO
        # STREAM for every known lane without forcing a JobQueue to
        # be opened (queues stay lazy for dispatch). DB matches the
        # queue's redis_db (3).
        self._inventory_redis = None

    # ─── Lifecycle ──────────────────────────────────────────────

    async def start(self) -> None:
        # Database — workflow_state lives in the platform Postgres.
        self._db = Database(PostgresConfig())
        await self._db.connect()

        self._manager = WorkflowManager(self._db)
        self._dispatcher = WorkflowDispatcher(self._manager, self._queue_for)

        # Phase 4: ensure the NexusMetrics singleton exists. Without this,
        # the orchestrator's main.py never calls init_metrics() and
        # get_metrics() returns None — the WorkflowMetrics facade then
        # silently drops every counter. This is the safety net for any
        # service that mounts the workflow plane but forgot to init.
        from nexus_sdk.observability.metrics import init_metrics
        if get_metrics() is None:
            try:
                init_metrics("orchestrator")
            except Exception as exc:
                logger.warning(
                    "workflow_plane.init_metrics_failed err=%s", exc,
                )

        # Phase 4: attach queue-inventory hooks onto the metrics
        # singleton so the sweeper can populate per-lane gauges
        # (depth, pending, oldest-pending-age, DLQ depth) without
        # forcing a JobQueue creation for every lane.
        metrics_provider = get_metrics()
        if metrics_provider is not None:
            try:
                self._open_inventory_redis()
                metrics_provider.get_dlq_depths = self._inventory_dlq_depths
                metrics_provider.get_queue_health = self._inventory_queue_health
            except Exception as exc:
                logger.warning(
                    "workflow_plane.inventory_hooks_failed err=%s", exc,
                )
        # Phase 4: eager-init the WorkflowMetrics singleton so /metrics
        # exposes every series at startup (with value 0) instead of only
        # after the first workflow runs. Prometheus needs the baseline
        # to compute rates correctly.
        try:
            from nexus_sdk.workflows.metrics import get_workflow_metrics
            get_workflow_metrics()
        except Exception as exc:
            logger.warning(
                "workflow_plane.workflow_metrics_init_failed err=%s", exc,
            )

        self._sweeper = WorkflowSweeper(
            manager=self._manager,
            dispatcher=self._dispatcher,
            config=SweeperConfig(
                orphan_threshold_seconds=int(
                    os.environ.get("WORKFLOW_ORPHAN_THRESHOLD_SECONDS", "120")
                ),
                deadline_tick_seconds=float(
                    os.environ.get("WORKFLOW_DEADLINE_TICK_SECONDS", "10")
                ),
                orphan_tick_seconds=float(
                    os.environ.get("WORKFLOW_ORPHAN_TICK_SECONDS", "15")
                ),
            ),
            metrics=metrics_provider,
        )
        await self._sweeper.start()
        logger.info(
            "workflow_plane.started lanes=%d redis=%s:%d",
            len(known_lanes()), self._redis_host, self._redis_port,
        )

    async def aclose(self) -> None:
        if self._sweeper:
            await self._sweeper.stop()
        for q in self._queues.values():
            try:
                await q.close()
            except Exception:
                pass
        self._queues.clear()
        if self._inventory_redis is not None:
            try:
                await self._inventory_redis.aclose()
            except Exception:
                pass
            self._inventory_redis = None
        if self._db:
            await self._db.disconnect()
        logger.info("workflow_plane.stopped")

    # ─── Queue inventory (Phase 4 metrics) ──────────────────────

    def _open_inventory_redis(self) -> None:
        """Lazy-open a low-level Redis client on the queue's DB (3).
        Used only by the sweeper's per-lane health hooks — keeps
        JobQueue allocation lazy for dispatch."""
        if self._inventory_redis is not None:
            return
        import redis.asyncio as aioredis
        self._inventory_redis = aioredis.Redis(
            host=self._redis_host,
            port=self._redis_port,
            password=self._redis_password or None,
            db=int(os.environ.get("NEXUS_QUEUE_REDIS_DB", "3")),
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5,
        )

    async def _inventory_dlq_depths(self) -> list[tuple[str, int]]:
        """Return [(lane, dlq_depth), ...] for every known lane.
        Best-effort: a missing stream just reports depth 0."""
        if self._inventory_redis is None:
            return []
        out: list[tuple[str, int]] = []
        for lane in known_lanes():
            dlq_key = f"nexus:queue:{lane}:dlq"
            try:
                depth = await self._inventory_redis.xlen(dlq_key)
            except Exception:
                depth = 0
            out.append((lane, int(depth)))
        return out

    async def _inventory_queue_health(
        self,
    ) -> list[tuple[str, int, int, float]]:
        """Return [(lane, depth, pending, oldest_pending_age_s), ...]."""
        if self._inventory_redis is None:
            return []
        out: list[tuple[str, int, int, float]] = []
        now = time.time()
        for lane in known_lanes():
            stream_key = f"nexus:queue:{lane}"
            group_name = f"nexus:workers:{lane}"
            try:
                depth = int(await self._inventory_redis.xlen(stream_key))
            except Exception:
                depth = 0
            pending = 0
            oldest_age = 0.0
            try:
                info = await self._inventory_redis.xpending(
                    stream_key, group_name,
                )
                # XPENDING summary form: (count, min_id, max_id, [(consumer, n), ...])
                if isinstance(info, dict):
                    pending = int(info.get("pending", 0) or 0)
                    min_id = info.get("min") or info.get("min_id") or "0-0"
                else:
                    pending = int((info[0] if info else 0) or 0)
                    min_id = (info[1] if info and len(info) > 1 else "0-0") or "0-0"
                if pending and isinstance(min_id, str) and "-" in min_id:
                    try:
                        ms_part = min_id.split("-", 1)[0]
                        oldest_age = max(
                            0.0, now - (int(ms_part) / 1000.0),
                        )
                    except Exception:
                        oldest_age = 0.0
            except Exception:
                pass
            out.append((lane, depth, pending, oldest_age))
        return out

    # ─── Queue cache ────────────────────────────────────────────

    async def _queue_for(self, lane: str) -> JobQueue:
        q = self._queues.get(lane)
        if q is not None and q._connected:
            return q
        q = JobQueue(
            engine_name=lane,
            redis_host=self._redis_host,
            redis_port=self._redis_port,
            redis_password=self._redis_password,
        )
        if not await q.connect():
            raise RuntimeError(f"failed to connect JobQueue for lane {lane!r}")
        self._queues[lane] = q
        return q

    # ─── Router mounting ────────────────────────────────────────

    @property
    def manager(self) -> WorkflowManager:
        if not self._manager:
            raise RuntimeError("WorkflowPlane.start() must run before manager access")
        return self._manager

    @property
    def dispatcher(self) -> WorkflowDispatcher:
        if not self._dispatcher:
            raise RuntimeError("WorkflowPlane.start() must run before dispatcher access")
        return self._dispatcher

    def attach(self, app: FastAPI) -> None:
        if not self._manager or not self._dispatcher:
            raise RuntimeError("WorkflowPlane.start() must run before attach()")
        # Phase 4: expose Prometheus /metrics for scrape. Idempotent.
        try:
            from nexus_sdk.observability.metrics import register_metrics_endpoint
            register_metrics_endpoint(app)
        except Exception as exc:
            logger.warning("workflow_plane.metrics_endpoint_failed err=%s", exc)
        # Open a Redis client for admission control. Same DB as the
        # job queues so a single Redis cluster serves both.
        redis_client = self._open_admission_redis()
        # Architect followup #3: wire the Redis concurrency limiter
        # into the orchestrator's tenant-admission singleton so the
        # /api/v1/orchestrator/process upload path enforces tenant
        # limits across all replicas (not just the local one).
        if redis_client is not None:
            try:
                from nexus_sdk.workflows.admission import (
                    TenantConcurrencyLimiter, QueueSaturationGuard,
                )
                # Import lazily to avoid coupling to main.py at module-load
                # (workflow_plane is imported by main.py).
                import app.main as _orch_main
                _orch_main.admission.attach_redis_limiter(
                    TenantConcurrencyLimiter(
                        redis_client=redis_client,
                        max_per_tenant=_orch_main.config.max_concurrent_workflows_per_tenant,
                    ),
                )
                logger.info(
                    "workflow_plane.tenant_admission.redis_attached max_per_tenant=%d",
                    _orch_main.config.max_concurrent_workflows_per_tenant,
                )
                # Architect P2: queue saturation guard. Uses the
                # inventory_redis (queue DB 3), NOT the admission redis,
                # because XLEN reads happen against the stream keys.
                if self._inventory_redis is not None:
                    _orch_main.queue_saturation_guard = QueueSaturationGuard(
                        redis_client=self._inventory_redis,
                        threshold=int(os.environ.get(
                            "NEXUS_QUEUE_SATURATION_THRESHOLD", "200",
                        )),
                        retry_after_seconds=int(os.environ.get(
                            "NEXUS_QUEUE_SATURATION_RETRY_AFTER_S", "30",
                        )),
                    )
                    logger.info(
                        "workflow_plane.queue_saturation_guard.attached threshold=%d lanes=%s",
                        _orch_main.queue_saturation_guard._threshold,
                        _orch_main.queue_saturation_guard._lanes,
                    )
            except Exception as exc:
                logger.warning(
                    "workflow_plane.tenant_admission.redis_attach_failed err=%s "
                    "(falling back to in-memory per-replica admission)", exc,
                )
        app.include_router(
            build_workflow_router(
                self._manager, self._dispatcher,
                redis_client=redis_client,
            )
        )
        app.include_router(
            build_dlq_router(
                self._manager,
                self._dispatcher,
                self._queue_for,
                known_lanes(),
            )
        )

    def _open_admission_redis(self):
        """Best-effort Redis connection for the admission layer. Returns
        None when redis-py is missing or the connection fails — admission
        then fails open (allow all)."""
        try:
            import redis.asyncio as aioredis

            return aioredis.Redis(
                host=self._redis_host,
                port=self._redis_port,
                password=self._redis_password or None,
                db=int(os.environ.get("NEXUS_ADMISSION_REDIS_DB", "4")),
                decode_responses=True,
                socket_connect_timeout=5,
                socket_timeout=5,
            )
        except Exception as e:
            logger.warning("workflow_plane.admission_redis_unavailable err=%s", e)
            return None


# ─── Helpers ────────────────────────────────────────────────────


def _parse_redis_url(url: str) -> tuple[str, int, str]:
    """Decompose redis://[:pw@]host:port[/db] into host/port/password."""
    if not url:
        return "localhost", 6379, ""
    raw = url.split("://", 1)[1] if "://" in url else url
    password = ""
    if "@" in raw:
        creds, raw = raw.rsplit("@", 1)
        if ":" in creds:
            password = creds.split(":", 1)[1]
        else:
            password = creds
    host_port = raw.split("/", 1)[0]
    if ":" in host_port:
        host, port = host_port.split(":", 1)
        return host, int(port), password
    return host_port, 6379, password
