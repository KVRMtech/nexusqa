"""
Nexus Prometheus Metrics — Custom application metrics + middleware.

Provides:
- Standard HTTP metrics (request count, duration, active requests, errors)
- Per-engine custom metric registration
- ASGI middleware for automatic endpoint instrumentation
- Thread-safe metric access

Production usage:
    # NexusEngine auto-adds this. For standalone services:
    from nexus_sdk.observability.metrics import MetricsMiddleware, get_metrics

    app.add_middleware(MetricsMiddleware, service_name="gateway")
    metrics = get_metrics()
    metrics.custom_counter("pii_detections_total", "Total PII detections").inc()
"""

from __future__ import annotations

import time
from typing import Optional

from prometheus_client import (
    Counter,
    Histogram,
    Gauge,
    Info,
    CollectorRegistry,
    REGISTRY,
)
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

__all__ = [
    "NexusMetrics",
    "MetricsMiddleware",
    "get_metrics",
]


class NexusMetrics:
    """
    Production Prometheus metrics for a Nexus service.

    Automatically tracks:
    - http_requests_total: Counter of all requests by method, path, status
    - http_request_duration_seconds: Histogram of request latency
    - http_requests_in_progress: Gauge of currently active requests
    - http_request_size_bytes: Histogram of request body sizes
    - http_response_size_bytes: Histogram of response body sizes

    Custom metrics can be registered per-engine:
        metrics.custom_counter("transcriptions_total", "Number of transcriptions")
    """

    def __init__(self, service_name: str, registry: CollectorRegistry = REGISTRY):
        self.service_name = service_name
        self._registry = registry
        self._custom_counters: dict[str, Counter] = {}
        self._custom_histograms: dict[str, Histogram] = {}
        self._custom_gauges: dict[str, Gauge] = {}

        # Standard HTTP metrics with service label
        self.requests_total = Counter(
            "http_requests_total",
            "Total HTTP requests",
            ["service", "method", "endpoint", "status_code"],
            registry=registry,
        )

        self.request_duration = Histogram(
            "http_request_duration_seconds",
            "HTTP request duration in seconds",
            ["service", "method", "endpoint"],
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
            registry=registry,
        )

        self.requests_in_progress = Gauge(
            "http_requests_in_progress",
            "Currently active HTTP requests",
            ["service"],
            registry=registry,
        )

        self.request_size = Histogram(
            "http_request_size_bytes",
            "HTTP request body size in bytes",
            ["service", "method", "endpoint"],
            buckets=(100, 1000, 10_000, 100_000, 1_000_000, 10_000_000),
            registry=registry,
        )

        self.response_size = Histogram(
            "http_response_size_bytes",
            "HTTP response body size in bytes",
            ["service", "method", "endpoint"],
            buckets=(100, 1000, 10_000, 100_000, 1_000_000, 10_000_000),
            registry=registry,
        )

        # Service info metric
        self.service_info = Info(
            "nexus_service",
            "Nexus service information",
            registry=registry,
        )

    def set_service_info(self, version: str, environment: str = "production") -> None:
        """Set service metadata as info metric."""
        self.service_info.info({
            "service": self.service_name,
            "version": version,
            "environment": environment,
        })

    def custom_counter(
        self,
        name: str,
        description: str,
        labels: list[str] | None = None,
        with_service: bool = True,
    ) -> Counter:
        """Register or retrieve a custom counter metric.

        with_service: when True (default), prepends a `service` label so
        the same metric name can be shared across services. Set False for
        platform-agnostic metrics (e.g. workflow / queue gauges) whose
        callers pass only the metric's own labels — the auto-prepend
        otherwise causes silent ValueError at .labels() time.
        """
        if name not in self._custom_counters:
            label_names = (["service"] if with_service else []) + (labels or [])
            self._custom_counters[name] = Counter(
                name, description, label_names, registry=self._registry,
            )
        return self._custom_counters[name]

    def custom_histogram(
        self,
        name: str,
        description: str,
        labels: list[str] | None = None,
        buckets: tuple = Histogram.DEFAULT_BUCKETS,
        with_service: bool = True,
    ) -> Histogram:
        """Register or retrieve a custom histogram metric. See
        custom_counter for the with_service semantics."""
        if name not in self._custom_histograms:
            label_names = (["service"] if with_service else []) + (labels or [])
            self._custom_histograms[name] = Histogram(
                name, description, label_names, buckets=buckets, registry=self._registry,
            )
        return self._custom_histograms[name]

    def custom_gauge(
        self,
        name: str,
        description: str,
        labels: list[str] | None = None,
        with_service: bool = True,
    ) -> Gauge:
        """Register or retrieve a custom gauge metric. See custom_counter
        for the with_service semantics."""
        if name not in self._custom_gauges:
            label_names = (["service"] if with_service else []) + (labels or [])
            self._custom_gauges[name] = Gauge(
                name, description, label_names, registry=self._registry,
            )
        return self._custom_gauges[name]


# ─── Global Instance ──────────────────────────────────────────

_metrics: Optional[NexusMetrics] = None


def init_metrics(service_name: str, registry: CollectorRegistry = REGISTRY) -> NexusMetrics:
    """Initialize the global NexusMetrics instance."""
    global _metrics
    _metrics = NexusMetrics(service_name, registry=registry)
    return _metrics


def get_metrics() -> Optional[NexusMetrics]:
    """Get the global NexusMetrics instance (None if not initialized)."""
    return _metrics


def register_metrics_endpoint(
    app, path: str = "/metrics", service_name: str = "orchestrator",
) -> None:
    """Register a Prometheus /metrics scrape endpoint on a FastAPI app.

    Phase 4: every Nexus service should expose `/metrics` so Prometheus
    can scrape workflow + queue + engine metrics. Idempotent — safe to
    call from multiple module-init paths.

    Side-effect: ensures `init_metrics()` has run so the NexusMetrics
    singleton exists. Without this, downstream `get_metrics()` callers
    (sweeper, WorkflowMetrics facade) get `None` back and silently
    drop every counter — the cause of the "/metrics returns 0 nexus_*
    series" bug observed in production smoke tests.
    """
    from prometheus_client import (
        CONTENT_TYPE_LATEST, generate_latest,
    )
    from fastapi import Response

    # Ensure the NexusMetrics singleton is initialized; safe to call
    # repeatedly — init_metrics overwrites the holder with a fresh
    # instance, but the underlying Prometheus registry is process-wide
    # so previously-allocated series stay registered.
    if get_metrics() is None:
        try:
            init_metrics(service_name)
        except Exception:
            pass

    # Don't double-register.
    for r in getattr(app, "routes", []):
        try:
            if getattr(r, "path", "") == path:
                return
        except Exception:
            continue

    async def _metrics_endpoint() -> Response:
        return Response(
            content=generate_latest(),
            media_type=CONTENT_TYPE_LATEST,
        )
    app.add_api_route(
        path, _metrics_endpoint,
        methods=["GET"], include_in_schema=False,
        tags=["observability"],
    )


# ─── ASGI Middleware ───────────────────────────────────────────

def _normalize_path(path: str) -> str:
    """
    Normalize URL path for metric labels to avoid cardinality explosion.

    Replaces UUIDs, numeric IDs, and other variable segments with placeholders.
    """
    import re

    # Replace UUIDs
    path = re.sub(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        "{id}",
        path,
    )
    # Replace pure numeric path segments
    path = re.sub(r"/\d+(?=/|$)", "/{id}", path)
    return path


class MetricsMiddleware(BaseHTTPMiddleware):
    """
    ASGI middleware that instruments all HTTP endpoints with Prometheus metrics.

    Tracks request count, duration, in-progress gauge, and body sizes.
    Path normalization prevents cardinality explosion from dynamic URL segments.
    """

    def __init__(self, app, service_name: str | None = None):
        super().__init__(app)
        self._service_name = service_name

    async def dispatch(self, request: Request, call_next) -> Response:
        metrics = get_metrics()
        if not metrics:
            return await call_next(request)

        service = self._service_name or metrics.service_name
        method = request.method
        path = _normalize_path(request.url.path)

        # Skip metrics/health endpoints to avoid recursion and noise
        if path in ("/metrics", "/health", "/health/ready", "/health/detail"):
            return await call_next(request)

        # Track in-progress
        metrics.requests_in_progress.labels(service=service).inc()

        # Track request size
        body = await request.body()
        metrics.request_size.labels(
            service=service, method=method, endpoint=path,
        ).observe(len(body))

        start = time.monotonic()
        try:
            response = await call_next(request)
            status_code = response.status_code
        except Exception:
            status_code = 500
            raise
        finally:
            duration = time.monotonic() - start
            metrics.requests_in_progress.labels(service=service).dec()

            metrics.requests_total.labels(
                service=service, method=method, endpoint=path, status_code=str(status_code),
            ).inc()

            metrics.request_duration.labels(
                service=service, method=method, endpoint=path,
            ).observe(duration)

        # Track response size (if available)
        content_length = response.headers.get("content-length")
        if content_length:
            metrics.response_size.labels(
                service=service, method=method, endpoint=path,
            ).observe(int(content_length))

        return response
