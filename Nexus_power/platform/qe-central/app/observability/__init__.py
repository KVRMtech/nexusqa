"""QE-Central — observability package (Phase-5.5).

Two additive, opt-in concerns that make a production FLEET legible without
changing any existing behaviour:

  * :mod:`.metrics` — import-guarded Prometheus counters/histograms/gauges for
    the high-value control-plane signals, a ``record_*`` helper API that is a
    hard NO-OP when ``prometheus_client`` is absent (or ``QEC_METRICS_ENABLED=0``),
    and a ``/metrics`` exposition route provider (:data:`metrics_router`).
  * :mod:`.middleware` — a correlation-id (request-id) ASGI middleware
    (:class:`CorrelationIdMiddleware`) that binds a sanitised id into structlog
    contextvars + the response header, plus :func:`outbound_request_id_headers`
    to propagate it onto the VKPower factory httpx calls.

Everything here is inert until the Wire agent mounts it; the defaults preserve
today's single-instance behaviour.
"""
from __future__ import annotations

from .metrics import (
    DEFAULT_METRICS_PATH,
    ENV_METRICS_ENABLED,
    ENV_METRICS_PATH,
    EXPECTED_METRIC_NAMES,
    METRIC_ADMISSION_DECISIONS,
    METRIC_ADMISSION_IN_FLIGHT,
    METRIC_COST_UNITS,
    METRIC_CYCLE_DURATION,
    METRIC_CYCLES_COMPLETED,
    METRIC_CYCLES_STARTED,
    METRIC_FACTORY_CALL_DURATION,
    METRIC_FACTORY_CALLS,
    METRIC_HARNESS_OUTCOMES,
    METRIC_QUOTA_DECISIONS,
    METRIC_SUBSTRATE_ROWS,
    PROM_AVAILABLE,
    build_metrics_router,
    dec_admission_in_flight,
    get_metrics_registry,
    inc_admission_in_flight,
    is_enabled,
    metrics_router,
    record_admission_decision,
    record_cost_units,
    record_cycle_completed,
    record_cycle_started,
    record_factory_call,
    record_harness_outcome,
    record_quota_decision,
    record_substrate_rows,
    render_latest,
    set_admission_in_flight,
)
from .middleware import (
    DEFAULT_REQUEST_ID_HEADER,
    ENV_REQUEST_ID_HEADER,
    LOG_FIELD_REQUEST_ID,
    MAX_REQUEST_ID_LEN,
    CorrelationIdMiddleware,
    current_request_id,
    mint_request_id,
    outbound_request_id_headers,
)

__all__ = [
    # ── metrics: introspection + exposition ──
    "PROM_AVAILABLE",
    "is_enabled",
    "get_metrics_registry",
    "render_latest",
    "build_metrics_router",
    "metrics_router",
    "EXPECTED_METRIC_NAMES",
    "DEFAULT_METRICS_PATH",
    "ENV_METRICS_ENABLED",
    "ENV_METRICS_PATH",
    # ── metrics: name constants ──
    "METRIC_CYCLES_STARTED",
    "METRIC_CYCLES_COMPLETED",
    "METRIC_CYCLE_DURATION",
    "METRIC_ADMISSION_DECISIONS",
    "METRIC_ADMISSION_IN_FLIGHT",
    "METRIC_FACTORY_CALLS",
    "METRIC_FACTORY_CALL_DURATION",
    "METRIC_SUBSTRATE_ROWS",
    "METRIC_HARNESS_OUTCOMES",
    "METRIC_COST_UNITS",
    "METRIC_QUOTA_DECISIONS",
    # ── metrics: recording helpers ──
    "record_cycle_started",
    "record_cycle_completed",
    "record_admission_decision",
    "inc_admission_in_flight",
    "dec_admission_in_flight",
    "set_admission_in_flight",
    "record_factory_call",
    "record_substrate_rows",
    "record_harness_outcome",
    "record_cost_units",
    "record_quota_decision",
    # ── correlation id ──
    "CorrelationIdMiddleware",
    "current_request_id",
    "mint_request_id",
    "outbound_request_id_headers",
    "DEFAULT_REQUEST_ID_HEADER",
    "ENV_REQUEST_ID_HEADER",
    "LOG_FIELD_REQUEST_ID",
    "MAX_REQUEST_ID_LEN",
]
