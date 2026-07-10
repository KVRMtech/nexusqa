"""QE-Central — Prometheus metrics (import-guarded, no-op when absent).

Phase-5.5 observability.  Instruments the high-value control-plane signals so a
FLEET of cycle drivers is legible in production: what work started, how it
terminated, how the admission gate is deciding, how the UNCHANGED VKPower
factory is behaving over HTTP, how much substrate/cost each cycle produced, and
how the REFUSE harness ruled.

DESIGN INVARIANTS (why this module is safe to wire everywhere)
==============================================================
  * **Import-guarded.**  ``prometheus_client`` is an OPTIONAL dependency (it is
    listed in ``requirements.txt`` so the container/CI has it, but a bare local
    checkout may not).  When it is absent — or when metrics are disabled via
    ``QEC_METRICS_ENABLED=0`` — every ``record_*`` helper is a hard NO-OP and
    :func:`get_metrics_registry` returns ``None``.  Nothing breaks in its
    absence; today's single-instance behaviour is preserved byte-for-byte.
  * **Recording NEVER raises into a caller.**  Every ``record_*`` helper is
    wrapped so a label-cardinality mistake or a prometheus internal error can
    never propagate into the cycle loop / request path — a metrics failure
    degrades to a debug log, never a cycle failure.
  * **Dedicated registry.**  All metrics register into a private
    :class:`CollectorRegistry` (NOT the global default), so the ``/metrics``
    exposition contains exactly QE-Central's metrics, tests are deterministic,
    and there is no duplicate-timeseries risk from the process-wide registry.
  * **Low-cardinality labels only.**  No ``tenant_id`` / ``app_id`` / raw URL /
    ``run_id`` ever becomes a label — callers pass LOGICAL names (e.g. the
    factory endpoint ``generate``/``rtm``, never the artifact-scoped path).  No
    secret or PII is ever exposed, so ``/metrics`` is safe to scrape unauth on an
    internal network (same posture as ``/health``).

WIRING (for the Wire agent — this module edits no other file)
=============================================================
  * Mount the exposition route:  ``app.include_router(metrics_router)`` (or
    ``build_metrics_router()`` to re-read ``QEC_METRICS_PATH`` at build time).
  * Instrument the hot paths by calling the ``record_*`` helpers, e.g. in the
    cycle driver (``record_cycle_started`` / ``record_cycle_completed``), the
    admission controller (``record_admission_decision`` / in-flight gauge), and
    the factory client (``record_factory_call``).
"""
from __future__ import annotations

import functools
import logging
import os
from typing import Callable, Optional

logger = logging.getLogger(__name__)


# ── Optional dependency: prometheus_client (import-guarded) ─────────────────
try:  # pragma: no cover - import branch depends on the environment
    from prometheus_client import (
        CONTENT_TYPE_LATEST,
        CollectorRegistry,
        Counter,
        Gauge,
        Histogram,
        generate_latest,
    )

    PROM_AVAILABLE = True
except Exception:  # prometheus_client not installed — every helper no-ops.
    CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"
    CollectorRegistry = None  # type: ignore[assignment,misc]
    Counter = Gauge = Histogram = None  # type: ignore[assignment,misc]
    generate_latest = None  # type: ignore[assignment]
    PROM_AVAILABLE = False


# ── Env knobs (config-driven; default preserves today's behaviour) ──────────
ENV_METRICS_ENABLED = "QEC_METRICS_ENABLED"
ENV_METRICS_PATH = "QEC_METRICS_PATH"
DEFAULT_METRICS_PATH = "/metrics"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


#: Metrics are ON by default when prometheus_client is importable; a deploy may
#: hard-disable them with ``QEC_METRICS_ENABLED=0`` (record_* then all no-op).
_ENABLED = PROM_AVAILABLE and _env_bool(ENV_METRICS_ENABLED, True)


# ── Metric NAME constants (family/base names — what appears in the scrape) ──
# NOTE: prometheus renders a Counter's samples with a ``_total`` suffix; these
# constants are the FAMILY names (the ``# HELP`` / ``# TYPE`` line names, which
# are what :func:`get_metrics_registry` collect() reports).
METRIC_CYCLES_STARTED = "qec_cycles_started"
METRIC_CYCLES_COMPLETED = "qec_cycles_completed"
METRIC_CYCLE_DURATION = "qec_cycle_duration_seconds"
METRIC_ADMISSION_DECISIONS = "qec_admission_decisions"
METRIC_ADMISSION_IN_FLIGHT = "qec_admission_in_flight"
METRIC_FACTORY_CALLS = "qec_factory_calls"
METRIC_FACTORY_CALL_DURATION = "qec_factory_call_duration_seconds"
METRIC_SUBSTRATE_ROWS = "qec_substrate_rows_written"
METRIC_HARNESS_OUTCOMES = "qec_harness_outcomes"
METRIC_COST_UNITS = "qec_cost_units"
METRIC_QUOTA_DECISIONS = "qec_quota_decisions"

#: The metric families a healthy ``/metrics`` scrape MUST expose (test contract).
EXPECTED_METRIC_NAMES = frozenset({
    METRIC_CYCLES_STARTED,
    METRIC_CYCLES_COMPLETED,
    METRIC_CYCLE_DURATION,
    METRIC_ADMISSION_DECISIONS,
    METRIC_ADMISSION_IN_FLIGHT,
    METRIC_FACTORY_CALLS,
    METRIC_FACTORY_CALL_DURATION,
    METRIC_SUBSTRATE_ROWS,
    METRIC_HARNESS_OUTCOMES,
    METRIC_COST_UNITS,
    METRIC_QUOTA_DECISIONS,
})


# ── Histogram buckets (code-level constants; seconds) ───────────────────────
# Cycles run seconds→tens-of-minutes; the default hard ceiling is 1800s (§3.5).
CYCLE_DURATION_BUCKETS = (1, 5, 15, 30, 60, 120, 300, 600, 1200, 1800, 3600)
# Factory calls span a fast /rtm read to a 120s synchronous /generate compile.
FACTORY_CALL_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0)


# ── Registry + metric objects (built ONCE, only when enabled) ───────────────
_REGISTRY: "Optional[CollectorRegistry]" = None
#: name → metric object.  Empty when disabled/absent, so every helper no-ops.
_M: dict = {}


def _build_metrics() -> None:
    """Construct the dedicated registry + all metric objects (idempotent).

    A no-op when prometheus_client is unavailable or metrics are disabled, so
    the module imports cleanly in any environment.
    """
    global _REGISTRY, _M
    if not _ENABLED or _REGISTRY is not None:
        return

    reg = CollectorRegistry()
    m: dict = {}

    m[METRIC_CYCLES_STARTED] = Counter(
        METRIC_CYCLES_STARTED,
        "Regression cycles STARTED by the cycle driver.",
        ["trigger", "mode"],
        registry=reg,
    )
    m[METRIC_CYCLES_COMPLETED] = Counter(
        METRIC_CYCLES_COMPLETED,
        "Regression cycles that reached a TERMINAL state, by that state.",
        ["terminal_state"],
        registry=reg,
    )
    m[METRIC_CYCLE_DURATION] = Histogram(
        METRIC_CYCLE_DURATION,
        "Wall-clock duration of a cycle from start to terminal state (seconds).",
        ["terminal_state"],
        buckets=CYCLE_DURATION_BUCKETS,
        registry=reg,
    )
    m[METRIC_ADMISSION_DECISIONS] = Counter(
        METRIC_ADMISSION_DECISIONS,
        "Admission-gate decisions, by outcome and reason.",
        ["decision", "reason"],
        registry=reg,
    )
    m[METRIC_ADMISSION_IN_FLIGHT] = Gauge(
        METRIC_ADMISSION_IN_FLIGHT,
        "Cycles currently holding an admission lease (in-flight concurrency).",
        registry=reg,
    )
    m[METRIC_FACTORY_CALLS] = Counter(
        METRIC_FACTORY_CALLS,
        "VKPower factory HTTP calls, by logical endpoint, method and status.",
        ["endpoint", "method", "status"],
        registry=reg,
    )
    m[METRIC_FACTORY_CALL_DURATION] = Histogram(
        METRIC_FACTORY_CALL_DURATION,
        "Latency of a VKPower factory HTTP call (seconds).",
        ["endpoint", "method"],
        buckets=FACTORY_CALL_BUCKETS,
        registry=reg,
    )
    m[METRIC_SUBSTRATE_ROWS] = Counter(
        METRIC_SUBSTRATE_ROWS,
        "Evidence-substrate rows written, by table.",
        ["table"],
        registry=reg,
    )
    m[METRIC_HARNESS_OUTCOMES] = Counter(
        METRIC_HARNESS_OUTCOMES,
        "REFUSE-harness rulings, by outcome (e.g. refused/passed/failed).",
        ["outcome"],
        registry=reg,
    )
    m[METRIC_COST_UNITS] = Counter(
        METRIC_COST_UNITS,
        "Cost units recorded to the cost ledger, by unit (summed quantity).",
        ["unit"],
        registry=reg,
    )
    m[METRIC_QUOTA_DECISIONS] = Counter(
        METRIC_QUOTA_DECISIONS,
        "Per-tenant quota decisions, by resource and outcome (allowed|denied).",
        ["resource", "decision"],
        registry=reg,
    )

    _REGISTRY = reg
    _M = m


_build_metrics()


# ── Public introspection ────────────────────────────────────────────────────

def is_enabled() -> bool:
    """True when metrics are live (prometheus present AND not disabled)."""
    return bool(_ENABLED and _REGISTRY is not None)


def get_metrics_registry() -> "Optional[CollectorRegistry]":
    """Return the dedicated :class:`CollectorRegistry`, or ``None`` when metrics
    are unavailable/disabled.  Callers (tests, the ``/metrics`` route) treat
    ``None`` as "no metrics" — never an error."""
    return _REGISTRY


def render_latest() -> tuple[bytes, str]:
    """Render the current metrics as ``(payload, content_type)`` for exposition.

    When metrics are unavailable the payload is a single valid-exposition comment
    line (HTTP 200-friendly) rather than an error — a scraper polling a
    prometheus-less checkout simply sees an empty scrape.
    """
    if not is_enabled() or generate_latest is None or _REGISTRY is None:
        return (
            b"# qe-central metrics disabled (prometheus_client absent or "
            b"QEC_METRICS_ENABLED=0)\n",
            "text/plain; charset=utf-8",
        )
    try:
        return (generate_latest(_REGISTRY), CONTENT_TYPE_LATEST)
    except Exception:  # exposition failure must not surface as a 500 loop
        logger.debug("qec.metrics.render_failed", exc_info=True)
        return (b"# qe-central metrics render error\n", "text/plain; charset=utf-8")


# ── Recording helpers (all NO-OP when disabled; NEVER raise) ────────────────

def _never_raises(fn: Callable) -> Callable:
    """Decorator: a metrics-recording failure degrades to a debug log.

    Metrics are observability, never correctness — a bad label or a prometheus
    internal error must never propagate into the cycle loop / request path.
    """

    @functools.wraps(fn)
    def _wrapper(*args, **kwargs):
        if not _M:
            return None
        try:
            return fn(*args, **kwargs)
        except Exception:  # noqa: BLE001 - deliberately swallow ALL
            logger.debug("qec.metrics.record_error", extra={"metric": fn.__name__}, exc_info=True)
            return None

    return _wrapper


def _lbl(value, *, default: str = "none") -> str:
    """Normalise a label value: coerce to str, trim, map empty → ``default``.

    Keeps label VALUES bounded/consistent; callers are still responsible for
    passing LOW-CARDINALITY logical values (never tenant/app ids or raw paths).
    """
    text = str(value).strip() if value is not None else ""
    return text or default


@_never_raises
def record_cycle_started(*, trigger: str, mode: str) -> None:
    """Count one cycle START (labels: trigger, mode)."""
    _M[METRIC_CYCLES_STARTED].labels(trigger=_lbl(trigger), mode=_lbl(mode)).inc()


@_never_raises
def record_cycle_completed(
    *, terminal_state: str, duration_seconds: Optional[float] = None,
) -> None:
    """Count one cycle reaching a TERMINAL state and (optionally) observe its
    wall-clock duration in the same terminal-state bucket."""
    state = _lbl(terminal_state)
    _M[METRIC_CYCLES_COMPLETED].labels(terminal_state=state).inc()
    if duration_seconds is not None:
        _M[METRIC_CYCLE_DURATION].labels(terminal_state=state).observe(
            max(0.0, float(duration_seconds))
        )


@_never_raises
def record_admission_decision(*, admitted: bool, reason: str = "") -> None:
    """Count one admission decision (labels: decision=admitted|denied, reason)."""
    decision = "admitted" if admitted else "denied"
    _M[METRIC_ADMISSION_DECISIONS].labels(decision=decision, reason=_lbl(reason)).inc()


@_never_raises
def inc_admission_in_flight(amount: float = 1.0) -> None:
    """Increment the in-flight-concurrency gauge (call on a granted lease)."""
    _M[METRIC_ADMISSION_IN_FLIGHT].inc(float(amount))


@_never_raises
def dec_admission_in_flight(amount: float = 1.0) -> None:
    """Decrement the in-flight-concurrency gauge (call on a released lease)."""
    _M[METRIC_ADMISSION_IN_FLIGHT].dec(float(amount))


@_never_raises
def set_admission_in_flight(value: float) -> None:
    """Set the in-flight gauge absolutely (e.g. reconcile from a snapshot)."""
    _M[METRIC_ADMISSION_IN_FLIGHT].set(max(0.0, float(value)))


@_never_raises
def record_factory_call(
    *, endpoint: str, method: str, status, duration_seconds: float,
) -> None:
    """Count one factory HTTP call and observe its latency.

    ``endpoint`` MUST be a LOGICAL name (e.g. ``generate``, ``rtm``, ``run``) —
    never the artifact-scoped path — to keep label cardinality bounded.
    ``status`` is the HTTP status (int) or a token like ``transport_error``.
    """
    ep = _lbl(endpoint)
    meth = _lbl(method).upper()
    _M[METRIC_FACTORY_CALLS].labels(
        endpoint=ep, method=meth, status=_lbl(status),
    ).inc()
    _M[METRIC_FACTORY_CALL_DURATION].labels(endpoint=ep, method=meth).observe(
        max(0.0, float(duration_seconds))
    )


@_never_raises
def record_substrate_rows(*, table: str, count: int) -> None:
    """Count evidence-substrate rows written to ``table``."""
    n = int(count)
    if n > 0:
        _M[METRIC_SUBSTRATE_ROWS].labels(table=_lbl(table)).inc(n)


@_never_raises
def record_harness_outcome(*, outcome: str) -> None:
    """Count one REFUSE-harness ruling (labels: outcome)."""
    _M[METRIC_HARNESS_OUTCOMES].labels(outcome=_lbl(outcome)).inc()


@_never_raises
def record_cost_units(*, unit: str, quantity) -> None:
    """Add ``quantity`` to the cost-units counter for ``unit`` (Decimal-safe)."""
    qty = float(quantity)
    if qty > 0:
        _M[METRIC_COST_UNITS].labels(unit=_lbl(unit)).inc(qty)


@_never_raises
def record_quota_decision(*, resource: str, allowed: bool) -> None:
    """Count one per-tenant quota decision (labels: resource, decision).

    ``resource`` is a LOW-cardinality logical name (``apps`` / ``concurrent_cycles``
    / ``monthly_browser_seconds`` / ``monthly_llm_tokens``); ``decision`` is
    ``allowed`` or ``denied``.  No tenant/app id ever becomes a label.
    """
    decision = "allowed" if allowed else "denied"
    _M[METRIC_QUOTA_DECISIONS].labels(resource=_lbl(resource), decision=decision).inc()


# ── The /metrics exposition route (provider — main.py is NOT edited here) ────

def build_metrics_router():
    """Build an :class:`fastapi.APIRouter` exposing the metrics scrape endpoint.

    The path is read from ``QEC_METRICS_PATH`` (default ``/metrics``) at build
    time.  Kept OUTSIDE the ``/api/*`` prefix by default so the fail-closed JWT
    middleware skips it (Prometheus scrapers hold no JWT) — the same public
    posture as ``/health``.  The Wire agent mounts it via
    ``app.include_router(build_metrics_router())`` or the module-level
    :data:`metrics_router`.
    """
    from fastapi import APIRouter, Response

    router = APIRouter(tags=["observability"])
    path = os.getenv(ENV_METRICS_PATH, DEFAULT_METRICS_PATH) or DEFAULT_METRICS_PATH

    @router.get(path, include_in_schema=False)
    async def metrics_endpoint() -> Response:  # noqa: D401 - fastapi handler
        """Prometheus exposition of QE-Central metrics."""
        payload, content_type = render_latest()
        return Response(content=payload, media_type=content_type)

    return router


#: Convenience module-level router (built once with the import-time env path).
metrics_router = build_metrics_router()


__all__ = [
    "PROM_AVAILABLE",
    "is_enabled",
    "get_metrics_registry",
    "render_latest",
    "build_metrics_router",
    "metrics_router",
    # metric name constants
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
    "EXPECTED_METRIC_NAMES",
    # recording helpers
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
    # env knobs
    "ENV_METRICS_ENABLED",
    "ENV_METRICS_PATH",
    "DEFAULT_METRICS_PATH",
]
