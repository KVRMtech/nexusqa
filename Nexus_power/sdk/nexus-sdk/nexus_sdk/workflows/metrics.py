"""
Prometheus metrics for the canonical workflow plane.

Series exposed (every label set is small + cardinality-bounded):

  Counters:
    nexus_workflow_created_total {kind, tenant_id_class}
    nexus_workflow_completed_total {kind, status}    status ∈ {success, failed, cancelled, quarantined}
    nexus_workflow_step_completed_total {engine, step, status}
    nexus_workflow_orphans_recovered_total
    nexus_workflow_deadline_quarantined_total {kind, status}

  Histograms (seconds):
    nexus_workflow_duration_seconds {kind, status}
    nexus_workflow_step_duration_seconds {engine, step, status}
    nexus_workflow_step_attempts {engine, step}

  Gauges:
    nexus_workflow_in_flight {kind}          — RUNNING / PENDING count by kind
    nexus_workflow_dlq_depth {lane}          — set by sweeper

Cardinality budget: tenant_id is bucketed into a coarse class
(`unknown` / `enterprise` / `pilot`) instead of raw — Prometheus
explodes if you label by tenant. The workflow_state row carries
the exact tenant_id; metrics labels are just for slicing.

The sweeper already populated some of these in Phase 1; this module
adds the missing ones (creation counter, terminal counters, duration
histograms) and provides a single `WorkflowMetrics` facade so the
manager, dispatcher, and sweeper all write to the same instances.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


# Tenant-class buckets. Operators can override via env if they want
# finer granularity, but the default keeps cardinality bounded.
_TENANT_CLASS_DEFAULT = "unknown"


def classify_tenant(tenant_id: str) -> str:
    """Bucket a tenant id into a coarse class label.

    Default heuristic: looks at a `nexus_tenants_<class>` env var
    that lists tenant ids per class. Falls back to 'unknown'. This
    keeps Prometheus label cardinality at ~3 instead of one-per-tenant.
    """
    import os

    if not tenant_id:
        return _TENANT_CLASS_DEFAULT
    for cls in ("enterprise", "pilot", "internal"):
        env = os.environ.get(f"NEXUS_TENANTS_{cls.upper()}", "")
        if not env:
            continue
        ids = {x.strip() for x in env.split(",") if x.strip()}
        if tenant_id in ids:
            return cls
    return _TENANT_CLASS_DEFAULT


class WorkflowMetrics:
    """Facade that lazily creates each Prometheus series and proxies
    `inc()` / `observe()` calls."""

    def __init__(self, metrics_provider=None) -> None:
        self._m = metrics_provider
        # Lazy holders — None until the provider hands us back a real
        # series. Failure to allocate any one of them is non-fatal.
        self._c_created = None
        self._c_completed = None
        self._c_step_completed = None
        self._c_orphans = None
        self._c_deadline = None
        self._h_duration = None
        self._h_step_duration = None
        self._h_step_attempts = None
        self._g_in_flight = None
        self._g_dlq_depth = None
        if metrics_provider is not None:
            self._init_series(metrics_provider)

    # ─── Initialisation ────────────────────────────────────────

    def _init_series(self, m) -> None:
        # Every workflow-level series passes with_service=False because the
        # workflow plane is canonically a single-service abstraction.
        # Without this, NexusMetrics would auto-prepend a `service` label
        # that the .labels() helpers below don't pass — every counter
        # call would raise ValueError and the try/except above would
        # silently swallow it, producing an empty /metrics surface.
        try:
            self._c_created = m.custom_counter(
                "nexus_workflow_created_total",
                "Workflows accepted by the canonical orchestrator.",
                labels=["kind", "tenant_class"],
                with_service=False,
            )
            # Phase 6: include tenant_class so dashboards can compute
            # per-tenant SLOs without breaking cardinality. classify_tenant
            # caps the cardinality at the configured class count.
            self._c_completed = m.custom_counter(
                "nexus_workflow_completed_total",
                "Workflows that reached a terminal state.",
                labels=["kind", "status", "tenant_class"],
                with_service=False,
            )
            self._c_step_completed = m.custom_counter(
                "nexus_workflow_step_completed_total",
                "Step attempts that returned a result.",
                labels=["engine", "step", "status"],
                with_service=False,
            )
            self._c_orphans = m.custom_counter(
                "nexus_workflow_orphans_recovered_total",
                "Workflows re-dispatched after detecting a stale heartbeat.",
                with_service=False,
            )
            self._c_deadline = m.custom_counter(
                "nexus_workflow_deadline_quarantined_total",
                "Workflows quarantined for exceeding the workflow-level deadline.",
                labels=["kind", "status"],
                with_service=False,
            )
            self._h_duration = m.custom_histogram(
                "nexus_workflow_duration_seconds",
                "End-to-end duration of canonical workflows.",
                labels=["kind", "status", "tenant_class"],
                buckets=(5, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200),
                with_service=False,
            )
            self._h_step_duration = m.custom_histogram(
                "nexus_workflow_step_duration_seconds",
                "Per-step duration on the worker side.",
                labels=["engine", "step", "status"],
                buckets=(0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600, 1200),
                with_service=False,
            )
            self._h_step_attempts = m.custom_histogram(
                "nexus_workflow_step_attempts",
                "Number of attempts a step took before terminating.",
                labels=["engine", "step"],
                buckets=(1, 2, 3, 5, 8, 13),
                with_service=False,
            )
            self._g_in_flight = m.custom_gauge(
                "nexus_workflow_in_flight",
                "Pending + running workflows, by kind.",
                labels=["kind"],
                with_service=False,
            )
            self._g_dlq_depth = m.custom_gauge(
                "nexus_workflow_dlq_depth",
                "Per-lane DLQ depth.",
                labels=["lane"],
                with_service=False,
            )
            # Phase 4 — additions for the new failure modes Phase 1/2/3 exposed.
            self._g_queue_depth = m.custom_gauge(
                "nexus_workflow_queue_depth",
                "Per-lane Redis stream length (sample).",
                labels=["lane"],
                with_service=False,
            )
            self._g_queue_pending = m.custom_gauge(
                "nexus_workflow_queue_pending",
                "Per-lane consumer-group pending entries (PEL).",
                labels=["lane"],
                with_service=False,
            )
            self._g_queue_oldest_pending_age = m.custom_gauge(
                "nexus_workflow_queue_oldest_pending_age_seconds",
                "Age of the oldest pending entry per lane (seconds).",
                labels=["lane"],
                with_service=False,
            )
            self._g_workflow_stuck = m.custom_gauge(
                "nexus_workflow_stuck_count",
                "RUNNING workflows whose heartbeat is stale beyond the configured threshold.",
                with_service=False,
            )
            self._c_ocr_frame_timeout = m.custom_counter(
                "nexus_eyes_ocr_frame_timeout_total",
                "OCR frames that hit per-frame timeout (returned placeholder).",
                with_service=False,
            )
            self._c_vision_degraded = m.custom_counter(
                "nexus_eyes_vision_degraded_total",
                "Times eyes.analyze_scenes fell back to OCR-only descriptions.",
                labels=["reason"],
                with_service=False,
            )
            self._c_minimal_artifact = m.custom_counter(
                "nexus_artifact_minimal_persisted_total",
                "Phase 3 — minimal canonical_artifacts rows written.",
                labels=["kind"],
                with_service=False,
            )
            self._c_artifact_enriched = m.custom_counter(
                "nexus_artifact_enriched_total",
                "Phase 3 — canonical_artifacts rows upgraded from minimal to persisted.",
                labels=["kind", "degraded"],
                with_service=False,
            )
            self._c_pel_replay = m.custom_counter(
                "nexus_queue_pel_replay_total",
                "Phase 2 — own-PEL messages replayed at consumer startup.",
                labels=["lane"],
                with_service=False,
            )
        except Exception as e:
            logger.warning("workflow_metrics.init_failed err=%s", e)

    # ─── Counter helpers ───────────────────────────────────────

    def record_created(self, kind: str, tenant_id: str) -> None:
        if not self._c_created:
            return
        try:
            self._c_created.labels(
                kind=kind, tenant_class=classify_tenant(tenant_id),
            ).inc()
        except Exception:
            pass

    def record_terminal(
        self,
        kind: str,
        status: str,
        duration_seconds: Optional[float] = None,
        tenant_id: str = "",
    ) -> None:
        # Phase 6: tenant_class included so per-tenant SLO dashboards
        # work. Defaults to the "unknown" bucket when tenant_id is
        # empty, which keeps existing call sites correct.
        tenant_class = classify_tenant(tenant_id)
        if self._c_completed:
            try:
                self._c_completed.labels(
                    kind=kind, status=status, tenant_class=tenant_class,
                ).inc()
            except Exception:
                pass
        if duration_seconds is not None and self._h_duration:
            try:
                self._h_duration.labels(
                    kind=kind, status=status, tenant_class=tenant_class,
                ).observe(duration_seconds)
            except Exception:
                pass

    def record_step(
        self,
        engine: str,
        step: str,
        status: str,
        duration_ms: Optional[int] = None,
        attempts: Optional[int] = None,
    ) -> None:
        if self._c_step_completed:
            try:
                self._c_step_completed.labels(
                    engine=engine, step=step, status=status,
                ).inc()
            except Exception:
                pass
        if duration_ms is not None and self._h_step_duration:
            try:
                self._h_step_duration.labels(
                    engine=engine, step=step, status=status,
                ).observe(duration_ms / 1000.0)
            except Exception:
                pass
        if attempts is not None and self._h_step_attempts:
            try:
                self._h_step_attempts.labels(
                    engine=engine, step=step,
                ).observe(attempts)
            except Exception:
                pass

    def record_orphan_recovered(self, count: int = 1) -> None:
        if not self._c_orphans or count <= 0:
            return
        try:
            self._c_orphans.inc(count)
        except Exception:
            pass

    def record_deadline_quarantined(self, kind: str, status: str) -> None:
        if not self._c_deadline:
            return
        try:
            self._c_deadline.labels(kind=kind, status=status).inc()
        except Exception:
            pass

    # ─── Gauge helpers ─────────────────────────────────────────

    def set_in_flight(self, kind: str, count: int) -> None:
        if not self._g_in_flight:
            return
        try:
            self._g_in_flight.labels(kind=kind).set(count)
        except Exception:
            pass

    def set_dlq_depth(self, lane: str, depth: int) -> None:
        if not self._g_dlq_depth:
            return
        try:
            self._g_dlq_depth.labels(lane=lane).set(depth)
        except Exception:
            pass

    # ─── Phase 4 helpers ───────────────────────────────────────

    def set_queue_health(
        self,
        lane: str,
        depth: int,
        pending: int,
        oldest_pending_age_s: float,
    ) -> None:
        for gauge, val in (
            (getattr(self, "_g_queue_depth", None), depth),
            (getattr(self, "_g_queue_pending", None), pending),
            (getattr(self, "_g_queue_oldest_pending_age", None), oldest_pending_age_s),
        ):
            if not gauge:
                continue
            try:
                gauge.labels(lane=lane).set(val)
            except Exception:
                pass

    def set_stuck_count(self, count: int) -> None:
        g = getattr(self, "_g_workflow_stuck", None)
        if not g:
            return
        try:
            g.set(count)
        except Exception:
            pass

    def record_ocr_frame_timeout(self, count: int = 1) -> None:
        c = getattr(self, "_c_ocr_frame_timeout", None)
        if not c or count <= 0:
            return
        try:
            c.inc(count)
        except Exception:
            pass

    def record_vision_degraded(self, reason: str = "unknown") -> None:
        c = getattr(self, "_c_vision_degraded", None)
        if not c:
            return
        try:
            c.labels(reason=reason).inc()
        except Exception:
            pass

    def record_minimal_artifact(self, kind: str) -> None:
        c = getattr(self, "_c_minimal_artifact", None)
        if not c:
            return
        try:
            c.labels(kind=kind).inc()
        except Exception:
            pass

    def record_artifact_enriched(self, kind: str, degraded: bool = False) -> None:
        c = getattr(self, "_c_artifact_enriched", None)
        if not c:
            return
        try:
            c.labels(kind=kind, degraded=str(bool(degraded)).lower()).inc()
        except Exception:
            pass

    def record_pel_replay(self, lane: str, count: int = 1) -> None:
        c = getattr(self, "_c_pel_replay", None)
        if not c or count <= 0:
            return
        try:
            c.labels(lane=lane).inc(count)
        except Exception:
            pass


# Singleton — the manager / dispatcher / sweeper all write to one
# instance per process. Allocation is cheap; reallocation is harmless.
_singleton: Optional[WorkflowMetrics] = None


def get_workflow_metrics() -> WorkflowMetrics:
    global _singleton
    if _singleton is None:
        try:
            from nexus_sdk.observability.metrics import get_metrics

            _singleton = WorkflowMetrics(get_metrics())
        except Exception:
            _singleton = WorkflowMetrics(None)
    return _singleton
