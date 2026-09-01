"""
Nexus Observability — Production-grade logging, metrics, tracing, and correlation.

Provides:
- Structured JSON logging via structlog
- Prometheus custom metrics + middleware
- OpenTelemetry distributed tracing
- Correlation ID (X-Request-ID) propagation

Every NexusEngine automatically initializes all observability components.
Platform services (gateway, API) can use these modules directly.
"""

from nexus_sdk.observability.logging import setup_logging, get_logger
from nexus_sdk.observability.metrics import (
    MetricsMiddleware,
    get_metrics,
    NexusMetrics,
)
from nexus_sdk.observability.correlation import CorrelationIdMiddleware
from nexus_sdk.observability.tracing import setup_tracing

__all__ = [
    "setup_logging",
    "get_logger",
    "MetricsMiddleware",
    "get_metrics",
    "NexusMetrics",
    "CorrelationIdMiddleware",
    "setup_tracing",
]
