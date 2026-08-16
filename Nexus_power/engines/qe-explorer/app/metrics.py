"""QE-Explorer — Prometheus metrics for the CRAWL LIFECYCLE (M0.6 / T-OB-01+02).

The explorer is where a crawl actually happens, and until now it exported
nothing: a crawl that started, wandered two pages, never consulted the oracle
and stopped was indistinguishable — from outside the container — from a healthy
deep walk.  This module is the telemetry model for the whole lifecycle:

    crawl starts → work executes → oracle/LLM calls occur → tokens are captured
    → crawl reaches a terminal state → metrics are emitted

DESIGN INVARIANTS (deliberately mirrors platform/qe-central/app/observability/metrics.py)
=========================================================================================
  * **Import-guarded.**  ``prometheus_client`` is listed in ``requirements.txt``
    so the container/CI has it, but a bare checkout may not.  When it is absent —
    or metrics are disabled via ``QEC_EXPLORER_METRICS_ENABLED=0`` — every
    ``record_*`` helper is a hard NO-OP and :func:`get_registry` returns ``None``.
    Nothing breaks in its absence; crawl behaviour is preserved byte-for-byte.
  * **Recording NEVER raises into a caller.**  A metrics failure must never
    become a crawl failure, so every helper is wrapped: a label mistake or a
    prometheus internal error degrades to a debug log.
  * **Dedicated registry.**  All metrics register into a private
    :class:`CollectorRegistry` (NOT the global default) so ``/metrics`` contains
    exactly the explorer's series and tests are deterministic.  Process/runtime
    collectors are attached to the same registry explicitly (see
    :func:`_attach_process_collectors`) rather than inherited by accident.
  * **BOUNDED labels, enforced in code — not by convention.**  A Prometheus
    label whose value space is unbounded is a production outage, so this module
    does not merely *document* the rule:
      - ``crawl_id``, ``tenant_id``, ``application_id``, URLs, prompts and
        exception messages are NEVER labels.  They live in the structured log /
        per-crawl record (see :mod:`app.crawl_context`), which is where
        high-cardinality diagnostics belong.
      - Free-ish values that DO belong on a label (``model``, ``provider``) go
        through :func:`_bounded`, which admits at most
        :data:`MAX_LABEL_VALUES_PER_KEY` distinct values per label key and folds
        every later arrival into ``"other"``.  A provider that starts reporting
        a per-request model string can therefore cost one extra series, not one
        per request.
      - ``terminal_reason`` is normalised through :data:`TERMINAL_REASONS`, an
        explicit allowlist.  An unrecognised reason becomes ``"other"``; an
        exception string can never become a label value.

WHY ``no_oracle`` IS A LABEL, NOT A TERMINAL REASON
===================================================
A crawl that completes without ever consulting the oracle has a real terminal
reason (``completed``, ``budget_max_states``, …) and overwriting it would destroy
the operator's answer to "why did it stop?" in order to answer "did the oracle
participate?".  Both questions matter, so they are separate BOUNDED dimensions:
``terminal_reason`` says why it stopped, ``oracle_state`` says whether the oracle
took part, and :data:`METRIC_CRAWLS_NO_ORACLE` is the dedicated, directly
alertable counter.  ``oracle_state`` further distinguishes the LEGITIMATE
zero-oracle crawl (``unconfigured`` — a probe/observe crawl never wires an
oracle) from the SILENT FAILURE (``configured_unused`` — the oracle was wired
and never once answered), which is the failure mode this milestone exists to
make visible.
"""
from __future__ import annotations

import functools
import logging
import os
from typing import Callable, Optional

logger = logging.getLogger("qe-explorer.metrics")


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
ENV_METRICS_ENABLED = "QEC_EXPLORER_METRICS_ENABLED"
ENV_METRICS_PATH = "QEC_EXPLORER_METRICS_PATH"
DEFAULT_METRICS_PATH = "/metrics"

#: Hard ceiling on DISTINCT values admitted for one label key before every
#: further value folds into ``"other"``.  The backstop that turns a cardinality
#: mistake into one wasted series instead of an unbounded time-series explosion.
MAX_LABEL_VALUES_PER_KEY = 50


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


#: Metrics are ON by default when prometheus_client is importable.
_ENABLED = PROM_AVAILABLE and _env_bool(ENV_METRICS_ENABLED, True)


# ── Metric FAMILY names (what appears in the scrape) ────────────────────────
METRIC_BUILD_INFO = "nexus_explorer_build_info"
METRIC_CRAWLS_STARTED = "nexus_crawls_started"
METRIC_CRAWLS_COMPLETED = "nexus_crawls_completed"
METRIC_CRAWLS_FAILED = "nexus_crawls_failed"
METRIC_CRAWLS_CANCELLED = "nexus_crawls_cancelled"
METRIC_CRAWLS_TIMED_OUT = "nexus_crawls_timed_out"
METRIC_CRAWLS_NO_ORACLE = "nexus_crawls_no_oracle"
METRIC_CRAWL_DURATION = "nexus_crawl_duration_seconds"
METRIC_CRAWL_MAX_DEPTH = "nexus_crawl_max_depth"
METRIC_CRAWL_DEEPEST_DEPTH = "nexus_crawl_deepest_depth_observed"
METRIC_CRAWL_STATES = "nexus_crawl_states_visited"
METRIC_CRAWL_IN_FLIGHT = "nexus_crawl_in_flight"
METRIC_ORACLE_CALLS = "nexus_crawl_oracle_calls"
METRIC_ORACLE_FAILURES = "nexus_crawl_oracle_failures"
METRIC_ORACLE_LATENCY = "nexus_crawl_oracle_latency_seconds"
METRIC_DISPATCH_ATTEMPTS = "nexus_crawl_dispatch_attempts"
METRIC_DISPATCH_409 = "nexus_crawl_dispatch_409"
METRIC_LLM_CALLS = "nexus_crawl_llm_calls"
METRIC_LLM_TOKENS = "nexus_crawl_llm_tokens"
METRIC_LLM_COST_USD = "nexus_crawl_llm_estimated_cost_usd"
METRIC_GUARD_BLOCKS = "nexus_crawl_guard_blocks"

#: The metric families a healthy ``/metrics`` scrape MUST expose (test contract).
#: Well above the "≥10 useful crawl metrics" acceptance bar.
EXPECTED_METRIC_NAMES = frozenset({
    METRIC_BUILD_INFO,
    METRIC_CRAWLS_STARTED,
    METRIC_CRAWLS_COMPLETED,
    METRIC_CRAWLS_FAILED,
    METRIC_CRAWLS_CANCELLED,
    METRIC_CRAWLS_TIMED_OUT,
    METRIC_CRAWLS_NO_ORACLE,
    METRIC_CRAWL_DURATION,
    METRIC_CRAWL_MAX_DEPTH,
    METRIC_CRAWL_DEEPEST_DEPTH,
    METRIC_CRAWL_STATES,
    METRIC_CRAWL_IN_FLIGHT,
    METRIC_ORACLE_CALLS,
    METRIC_ORACLE_FAILURES,
    METRIC_ORACLE_LATENCY,
    METRIC_DISPATCH_ATTEMPTS,
    METRIC_DISPATCH_409,
    METRIC_LLM_CALLS,
    METRIC_LLM_TOKENS,
    METRIC_LLM_COST_USD,
    METRIC_GUARD_BLOCKS,
})


# ── Bounded label VOCABULARIES ─────────────────────────────────────────────
#: Fallback for any value outside its allowlist — the reason an exception string
#: can never become a label.
OTHER = "other"

#: Canonical, BOUNDED terminal reasons.  Every crawl outcome maps into exactly
#: one of these; anything unrecognised becomes :data:`OTHER`.
TERMINAL_REASONS = frozenset({
    "completed",
    "cancelled",
    "timeout",
    "budget_max_states",
    "budget_max_requests",
    "max_depth",
    "auth_failed",
    "auth_required",
    "blocked",
    "no_progress",
    "error",
    OTHER,
})

#: explorer ``stop_reason`` (app.crawler / app.budget vocabulary) → the canonical
#: telemetry terminal reason.  Kept as an explicit table so a new stop reason is
#: a deliberate telemetry decision, never a silent new label value.
STOP_REASON_TO_TERMINAL = {
    "completed": "completed",
    "cancelled": "cancelled",
    "error": "error",
    "auth_failed": "auth_failed",
    "auth_required_no_credentials": "auth_required",
    # The wall-clock budget IS the crawl timeout — named as such so the operator
    # question "did it time out?" is one label match, not tribal knowledge.
    "budget_max_wall_ms": "timeout",
    "budget_max_requests": "budget_max_requests",
    "budget_max_states": "budget_max_states",
}

#: Whether the oracle took part.  ``unconfigured`` is a LEGITIMATE zero-oracle
#: crawl (probe/observe posture never wires one); ``configured_unused`` is the
#: silent failure this milestone exists to expose.
ORACLE_USED = "used"
ORACLE_UNCONFIGURED = "unconfigured"
ORACLE_CONFIGURED_UNUSED = "configured_unused"
ORACLE_STATES = frozenset({ORACLE_USED, ORACLE_UNCONFIGURED, ORACLE_CONFIGURED_UNUSED})

#: Which oracle.  One label value per oracle KIND — never per call.
ORACLE_KINDS = frozenset({"advance", "medic", "vision", OTHER})

#: Bounded oracle outcomes across all three oracle kinds.
ORACLE_OUTCOMES = frozenset({
    "picked", "none", "unavailable",          # advance
    "proposed", "display_only",               # medic
    "perceived", "empty",                     # vision
    "error", "circuit_open", "cap_reached",   # resilience states
    OTHER,
})

#: Bounded oracle failure reasons — transport/protocol classes, never messages.
ORACLE_FAILURE_REASONS = frozenset({
    "transport", "timeout", "http_error", "bad_body", "circuit_open",
    "cap_reached", OTHER,
})

#: Bounded dispatch outcomes (the qe-central → explorer admission seam).
DISPATCH_OUTCOMES = frozenset({
    "accepted", "busy_409", "unauthorized", "rejected", "transport_error", OTHER,
})

#: Bounded LLM call outcomes.  ``error_with_usage`` is deliberate: a provider that
#: bills for a request it then failed must not vanish from the spend account.
LLM_OUTCOMES = frozenset({
    "success", "error", "error_with_usage", "timeout", "retry", OTHER,
})

#: Token kinds.  ``prompt``/``completion`` sum to ``total``; the cache kinds are
#: Anthropic's prompt-cache accounting, reported separately so they can never be
#: double-counted into the prompt/completion total.
TOKEN_KINDS = frozenset({"prompt", "completion", "cache_read", "cache_creation"})

#: Bounded crawl modes / traversal postures (app.main ExploreRequest vocabulary).
CRAWL_MODES = frozenset({"explore", "target", "e2e", OTHER})
TRAVERSALS = frozenset({"full", "probe", "observe", OTHER})

#: Bounded failure classes for :data:`METRIC_CRAWLS_FAILED` — the crawl-level
#: equivalent of an exception TYPE, never its message.
FAILURE_KINDS = frozenset({
    "crawl_exception", "browser_launch", "navigation", "auth", "callback",
    "cancelled", "timeout", OTHER,
})


# ── Histogram buckets (seconds / depth levels) ─────────────────────────────
#: Crawls run seconds → tens of minutes; the wall budget defaults to 1_800_000ms.
CRAWL_DURATION_BUCKETS = (1, 5, 15, 30, 60, 120, 300, 600, 1200, 1800, 3600, 7200)
#: Depth LEVELS (not seconds) — the default max_depth is 6, e2e walks go deeper.
CRAWL_DEPTH_BUCKETS = (0, 1, 2, 3, 4, 5, 6, 8, 10, 15, 20, 30, 50)
#: One oracle consultation is an LLM round-trip behind an 8–10s timeout.
ORACLE_LATENCY_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 15.0, 30.0, 60.0)


# ── Registry + metric objects (built ONCE, only when enabled) ──────────────
_REGISTRY: "Optional[CollectorRegistry]" = None
#: name → metric object.  Empty when disabled/absent, so every helper no-ops.
_M: dict = {}
#: label key → the set of distinct values already admitted (cardinality guard).
_SEEN_LABEL_VALUES: dict = {}


def _build_metrics() -> None:
    """Construct the dedicated registry + every metric object (idempotent).

    A no-op when prometheus_client is unavailable or metrics are disabled, so
    this module imports cleanly in any environment.
    """
    global _REGISTRY, _M
    if not _ENABLED or _REGISTRY is not None:
        return

    reg = CollectorRegistry()
    m: dict = {}

    # -- identity ----------------------------------------------------------
    m[METRIC_BUILD_INFO] = Gauge(
        METRIC_BUILD_INFO,
        "Explorer build identity (always 1). Labels carry version strings.",
        ["version", "refuse_pack_version"],
        registry=reg,
    )

    # -- crawl counters ----------------------------------------------------
    m[METRIC_CRAWLS_STARTED] = Counter(
        METRIC_CRAWLS_STARTED,
        "Crawls STARTED on this explorer (the browser+crawler were built).",
        ["crawl_mode", "traversal"],
        registry=reg,
    )
    m[METRIC_CRAWLS_COMPLETED] = Counter(
        METRIC_CRAWLS_COMPLETED,
        "Crawls that reached a TERMINAL state, by bounded reason and whether "
        "the oracle participated.",
        ["terminal_reason", "oracle_state"],
        registry=reg,
    )
    m[METRIC_CRAWLS_FAILED] = Counter(
        METRIC_CRAWLS_FAILED,
        "Crawls that ended in FAILURE, by bounded failure class (never a "
        "message).",
        ["failure_kind"],
        registry=reg,
    )
    m[METRIC_CRAWLS_CANCELLED] = Counter(
        METRIC_CRAWLS_CANCELLED,
        "Crawls stopped by an operator cancellation request.",
        registry=reg,
    )
    m[METRIC_CRAWLS_TIMED_OUT] = Counter(
        METRIC_CRAWLS_TIMED_OUT,
        "Crawls stopped by the wall-clock budget (the crawl timeout).",
        registry=reg,
    )
    m[METRIC_CRAWLS_NO_ORACLE] = Counter(
        METRIC_CRAWLS_NO_ORACLE,
        "Crawls that reached a terminal state having made ZERO oracle calls. "
        "expected=yes ⇒ no oracle was wired (legitimate); expected=no ⇒ an "
        "oracle WAS wired and never answered (the silent failure).",
        ["terminal_reason", "expected"],
        registry=reg,
    )

    # -- duration / depth / progress --------------------------------------
    m[METRIC_CRAWL_DURATION] = Histogram(
        METRIC_CRAWL_DURATION,
        "Wall-clock duration of a crawl from start to terminal state (seconds).",
        ["terminal_reason"],
        buckets=CRAWL_DURATION_BUCKETS,
        registry=reg,
    )
    m[METRIC_CRAWL_MAX_DEPTH] = Histogram(
        METRIC_CRAWL_MAX_DEPTH,
        "Maximum frontier depth reached by a crawl (distribution over crawls; "
        "the unit is DEPTH LEVELS, not seconds).",
        ["terminal_reason"],
        buckets=CRAWL_DEPTH_BUCKETS,
        registry=reg,
    )
    m[METRIC_CRAWL_DEEPEST_DEPTH] = Gauge(
        METRIC_CRAWL_DEEPEST_DEPTH,
        "High-water mark: the deepest frontier depth any crawl has reached on "
        "this explorer process (genuinely current state).",
        registry=reg,
    )
    m[METRIC_CRAWL_STATES] = Counter(
        METRIC_CRAWL_STATES,
        "Unique states visited across all crawls on this explorer.",
        registry=reg,
    )
    m[METRIC_CRAWL_IN_FLIGHT] = Gauge(
        METRIC_CRAWL_IN_FLIGHT,
        "Crawls currently executing on this explorer (single-flight ⇒ 0 or 1).",
        registry=reg,
    )
    m[METRIC_GUARD_BLOCKS] = Counter(
        METRIC_GUARD_BLOCKS,
        "Requests aborted by the fail-closed network guard, across all crawls.",
        registry=reg,
    )

    # -- oracle ------------------------------------------------------------
    m[METRIC_ORACLE_CALLS] = Counter(
        METRIC_ORACLE_CALLS,
        "Oracle consultations, by oracle kind and bounded outcome.",
        ["oracle", "outcome"],
        registry=reg,
    )
    m[METRIC_ORACLE_FAILURES] = Counter(
        METRIC_ORACLE_FAILURES,
        "Oracle consultations that could not produce a decision, by bounded "
        "failure class.",
        ["oracle", "reason"],
        registry=reg,
    )
    m[METRIC_ORACLE_LATENCY] = Histogram(
        METRIC_ORACLE_LATENCY,
        "Latency of one oracle consultation round-trip (seconds).",
        ["oracle"],
        buckets=ORACLE_LATENCY_BUCKETS,
        registry=reg,
    )

    # -- dispatch (the qe-central → explorer admission seam) ---------------
    m[METRIC_DISPATCH_ATTEMPTS] = Counter(
        METRIC_DISPATCH_ATTEMPTS,
        "Inbound crawl-dispatch attempts, by bounded outcome.",
        ["outcome"],
        registry=reg,
    )
    m[METRIC_DISPATCH_409] = Counter(
        METRIC_DISPATCH_409,
        "Crawl dispatches REFUSED 409 because the single-flight slot was held.",
        registry=reg,
    )

    # -- LLM / tokens ------------------------------------------------------
    m[METRIC_LLM_CALLS] = Counter(
        METRIC_LLM_CALLS,
        "LLM calls made on behalf of a crawl, by provider, model and outcome.",
        ["provider", "model", "outcome"],
        registry=reg,
    )
    m[METRIC_LLM_TOKENS] = Counter(
        METRIC_LLM_TOKENS,
        "Provider-REPORTED tokens consumed on behalf of crawls. kind=prompt|"
        "completion sum to the total; cache_* are reported separately and are "
        "NOT part of that sum.",
        ["provider", "model", "kind"],
        registry=reg,
    )
    m[METRIC_LLM_COST_USD] = Counter(
        METRIC_LLM_COST_USD,
        "ESTIMATED spend in USD derived from token counts and a price table. "
        "A separate concept from token COUNT — never a currency reading of it.",
        ["provider", "model"],
        registry=reg,
    )

    _REGISTRY = reg
    _M = m
    _attach_process_collectors(reg)


def _attach_process_collectors(reg) -> None:
    """Attach prometheus_client's process/platform collectors to OUR registry.

    A dedicated registry deliberately does not inherit the default registry's
    collectors, so process CPU/RSS/fd and the python GC/platform series must be
    registered explicitly.  Best-effort: these collectors are unavailable on some
    platforms (``ProcessCollector`` reads /proc and is a no-op on Windows), and
    their absence must never cost the application metrics.
    """
    try:
        from prometheus_client import (
            GC_COLLECTOR, PLATFORM_COLLECTOR, PROCESS_COLLECTOR,
        )

        for collector in (PROCESS_COLLECTOR, PLATFORM_COLLECTOR, GC_COLLECTOR):
            try:
                reg.register(collector)
            except Exception:  # already registered / unsupported platform
                logger.debug("qec.explorer.metrics.collector_skipped", exc_info=True)
    except Exception:
        logger.debug("qec.explorer.metrics.process_collectors_unavailable", exc_info=True)


_build_metrics()


# ── Public introspection ───────────────────────────────────────────────────

def is_enabled() -> bool:
    """True when metrics are live (prometheus present AND not disabled)."""
    return bool(_ENABLED and _REGISTRY is not None)


def get_registry() -> "Optional[CollectorRegistry]":
    """The dedicated :class:`CollectorRegistry`, or ``None`` when unavailable."""
    return _REGISTRY


def render_latest() -> tuple[bytes, str]:
    """Render the current metrics as ``(payload, content_type)`` for exposition.

    When metrics are unavailable the payload is a single valid-exposition comment
    line rather than an error — a scraper polling a prometheus-less checkout sees
    an empty scrape, never a 500.
    """
    if not is_enabled() or generate_latest is None or _REGISTRY is None:
        return (
            b"# qe-explorer metrics disabled (prometheus_client absent or "
            b"QEC_EXPLORER_METRICS_ENABLED=0)\n",
            "text/plain; charset=utf-8",
        )
    try:
        return (generate_latest(_REGISTRY), CONTENT_TYPE_LATEST)
    except Exception:  # exposition failure must not surface as a 500 loop
        logger.debug("qec.explorer.metrics.render_failed", exc_info=True)
        return (b"# qe-explorer metrics render error\n", "text/plain; charset=utf-8")


# ── Label normalisation (the cardinality firewall) ─────────────────────────

def _never_raises(fn: Callable) -> Callable:
    """Decorator: a metrics-recording failure degrades to a debug log.

    Metrics are observability, never correctness — a bad label or a prometheus
    internal error must never propagate into the crawl loop / request path.
    """

    @functools.wraps(fn)
    def _wrapper(*args, **kwargs):
        if not _M:
            return None
        try:
            return fn(*args, **kwargs)
        except Exception:  # noqa: BLE001 - deliberately swallow ALL
            logger.debug("qec.explorer.metrics.record_error",
                         extra={"metric": fn.__name__}, exc_info=True)
            return None

    return _wrapper


def _enum(value, allowed: frozenset, *, default: str = OTHER) -> str:
    """Normalise ``value`` against an explicit allowlist.

    The reason an exception message, a URL or a tenant-supplied string can never
    reach a Prometheus label: anything outside ``allowed`` becomes ``default``.
    """
    text = str(value).strip().lower() if value is not None else ""
    return text if text in allowed else default


def _bounded(key: str, value, *, default: str = "unknown") -> str:
    """Admit ``value`` as a label for ``key`` only while ``key`` stays bounded.

    For dimensions that are bounded IN PRACTICE but not by an allowlist we can
    write down — ``provider`` and ``model`` come from deployment config, so the
    real value space is a handful of strings, but a provider that began
    reporting a per-request model id would otherwise mint a series per request.
    The first :data:`MAX_LABEL_VALUES_PER_KEY` distinct values are admitted
    verbatim; every later arrival folds into :data:`OTHER`.  Sanitised to
    ``[a-z0-9._:-]`` and length-capped first, so a hostile string cannot inject
    exposition syntax.
    """
    raw = str(value).strip().lower() if value is not None else ""
    if not raw:
        return default
    cleaned = "".join(c if (c.isalnum() or c in "._:-") else "_" for c in raw)[:64]
    if not cleaned:
        return default
    seen = _SEEN_LABEL_VALUES.setdefault(key, set())
    if cleaned in seen:
        return cleaned
    if len(seen) >= MAX_LABEL_VALUES_PER_KEY:
        return OTHER
    seen.add(cleaned)
    return cleaned


def terminal_reason_for(stop_reason: str) -> str:
    """Map an explorer ``stop_reason`` onto the canonical bounded vocabulary."""
    mapped = STOP_REASON_TO_TERMINAL.get(
        str(stop_reason or "").strip().lower(), "")
    return _enum(mapped, TERMINAL_REASONS)


def oracle_state_for(*, oracle_configured: bool, oracle_calls: int) -> str:
    """Classify oracle participation into the bounded 3-state vocabulary.

    The distinction that makes a no-oracle crawl actionable: a crawl with no
    oracle wired at all was never going to make a call (legitimate), while a
    crawl that wired one and made zero is the silent failure.
    """
    if int(oracle_calls or 0) > 0:
        return ORACLE_USED
    return ORACLE_CONFIGURED_UNUSED if oracle_configured else ORACLE_UNCONFIGURED


# ── Recording helpers (all NO-OP when disabled; NEVER raise) ───────────────

@_never_raises
def set_build_info(*, version: str, refuse_pack_version: str) -> None:
    """Publish the explorer build identity (one series per build)."""
    _M[METRIC_BUILD_INFO].labels(
        version=_bounded("version", version),
        refuse_pack_version=_bounded("refuse_pack_version", refuse_pack_version),
    ).set(1)


@_never_raises
def record_dispatch(*, outcome: str) -> None:
    """Count one inbound dispatch attempt; 409s also hit the dedicated counter.

    Both series are deliberate: ``outcome="busy_409"`` keeps the dispatch funnel
    readable as one metric, and :data:`METRIC_DISPATCH_409` gives the alert rule
    a target that cannot be broken by a label-selector typo.
    """
    resolved = _enum(outcome, DISPATCH_OUTCOMES)
    _M[METRIC_DISPATCH_ATTEMPTS].labels(outcome=resolved).inc()
    if resolved == "busy_409":
        _M[METRIC_DISPATCH_409].inc()


@_never_raises
def record_crawl_started(*, crawl_mode: str, traversal: str) -> None:
    """Count one crawl START and raise the in-flight gauge."""
    _M[METRIC_CRAWLS_STARTED].labels(
        crawl_mode=_enum(crawl_mode, CRAWL_MODES),
        traversal=_enum(traversal, TRAVERSALS),
    ).inc()
    _M[METRIC_CRAWL_IN_FLIGHT].inc()


@_never_raises
def record_crawl_terminal(
    *,
    stop_reason: str,
    duration_seconds: Optional[float] = None,
    max_depth: Optional[int] = None,
    states: int = 0,
    guard_blocks: int = 0,
    oracle_configured: bool = False,
    oracle_calls: int = 0,
    failed: bool = False,
    failure_kind: str = "",
) -> None:
    """Record EVERY terminal outcome of a crawl in one call.

    Deliberately one entry point rather than a family of per-outcome helpers:
    the caller invokes it from a ``finally``, so a crawl that raised, timed out,
    was cancelled or completed all traverse the SAME instrumentation and no path
    can silently skip the terminal signal.  ``failed`` and the counters are not
    mutually exclusive — a failed crawl still has a terminal reason, a duration
    and an oracle verdict, and suppressing those on the failure path is exactly
    how error-path blindness gets built in.
    """
    reason = terminal_reason_for(stop_reason)
    state = oracle_state_for(
        oracle_configured=oracle_configured, oracle_calls=oracle_calls)

    _M[METRIC_CRAWLS_COMPLETED].labels(
        terminal_reason=reason, oracle_state=state).inc()
    _M[METRIC_CRAWL_IN_FLIGHT].dec()

    if duration_seconds is not None:
        _M[METRIC_CRAWL_DURATION].labels(terminal_reason=reason).observe(
            max(0.0, float(duration_seconds)))
    if max_depth is not None:
        depth = max(0, int(max_depth))
        _M[METRIC_CRAWL_MAX_DEPTH].labels(terminal_reason=reason).observe(depth)
        # A Gauge is correct here BECAUSE it is genuinely current state: the
        # deepest depth this process has ever seen, not a per-crawl reset.
        _bump_deepest(depth)
    if int(states or 0) > 0:
        _M[METRIC_CRAWL_STATES].inc(int(states))
    if int(guard_blocks or 0) > 0:
        _M[METRIC_GUARD_BLOCKS].inc(int(guard_blocks))

    if reason == "cancelled":
        _M[METRIC_CRAWLS_CANCELLED].inc()
    if reason == "timeout":
        _M[METRIC_CRAWLS_TIMED_OUT].inc()
    if failed or reason == "error":
        _M[METRIC_CRAWLS_FAILED].labels(
            failure_kind=_enum(failure_kind or "crawl_exception", FAILURE_KINDS)
        ).inc()

    # THE milestone signal: a crawl reached a terminal state having never
    # consulted the oracle.  ``expected`` separates the legitimate case from the
    # silent failure so the alert can fire on one without drowning in the other.
    if state != ORACLE_USED:
        _M[METRIC_CRAWLS_NO_ORACLE].labels(
            terminal_reason=reason,
            expected="yes" if state == ORACLE_UNCONFIGURED else "no",
        ).inc()


@_never_raises
def _bump_deepest(depth: int) -> None:
    """Raise the deepest-depth high-water gauge (monotonic within a process)."""
    gauge = _M[METRIC_CRAWL_DEEPEST_DEPTH]
    try:
        current = gauge._value.get()  # prometheus_client Gauge internal value
    except Exception:
        current = 0.0
    if float(depth) > float(current or 0.0):
        gauge.set(float(depth))


@_never_raises
def record_oracle_call(
    *, oracle: str, outcome: str, duration_seconds: Optional[float] = None,
    failure_reason: str = "",
) -> None:
    """Count one oracle consultation, its latency, and any failure class."""
    kind = _enum(oracle, ORACLE_KINDS)
    _M[METRIC_ORACLE_CALLS].labels(
        oracle=kind, outcome=_enum(outcome, ORACLE_OUTCOMES)).inc()
    if duration_seconds is not None:
        _M[METRIC_ORACLE_LATENCY].labels(oracle=kind).observe(
            max(0.0, float(duration_seconds)))
    if failure_reason:
        _M[METRIC_ORACLE_FAILURES].labels(
            oracle=kind, reason=_enum(failure_reason, ORACLE_FAILURE_REASONS)
        ).inc()


@_never_raises
def record_llm_usage(
    *,
    provider: str = "",
    model: str = "",
    outcome: str = "success",
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    estimated_cost_usd: float = 0.0,
) -> None:
    """Record one LLM call's provider-reported token usage (AGGREGATE only).

    No ``crawl_id`` label — per-crawl attribution lives in the structured log and
    the :class:`app.crawl_context.CrawlTokenUsage` ledger, which is where an
    unbounded key belongs.  Prometheus answers "how many tokens, by provider and
    model, over time"; the crawl record answers "which crawl spent them".
    """
    prov = _bounded("provider", provider, default="unknown")
    mdl = _bounded("model", model, default="unknown")
    _M[METRIC_LLM_CALLS].labels(
        provider=prov, model=mdl, outcome=_enum(outcome, LLM_OUTCOMES)).inc()
    for kind, amount in (
        ("prompt", prompt_tokens),
        ("completion", completion_tokens),
        ("cache_read", cache_read_tokens),
        ("cache_creation", cache_creation_tokens),
    ):
        n = int(amount or 0)
        if n > 0:
            _M[METRIC_LLM_TOKENS].labels(
                provider=prov, model=mdl, kind=kind).inc(n)
    cost = float(estimated_cost_usd or 0.0)
    if cost > 0:
        _M[METRIC_LLM_COST_USD].labels(provider=prov, model=mdl).inc(cost)


# ── Test/introspection helper ──────────────────────────────────────────────

def reset_for_tests() -> None:
    """Rebuild the registry from scratch — TEST-ONLY.

    Counters must never reset in production (a reset is indistinguishable from a
    process restart to Prometheus, and ``rate()`` would read it as a spike), so
    this is deliberately named for its only legitimate caller.
    """
    global _REGISTRY, _M, _SEEN_LABEL_VALUES
    _REGISTRY = None
    _M = {}
    _SEEN_LABEL_VALUES = {}
    _build_metrics()


# ── The /metrics exposition route ──────────────────────────────────────────

def build_metrics_router():
    """Build an :class:`fastapi.APIRouter` exposing the scrape endpoint.

    The path is read from ``QEC_EXPLORER_METRICS_PATH`` (default ``/metrics``).
    Deliberately OUTSIDE the ``/api/*`` prefix and WITHOUT the ``X-QEC-Token``
    dependency — Prometheus scrapers hold no token, and this is the same public
    posture ``/health`` already has.  The exposition carries no crawl id, no
    URL, no credential and no prompt, so it is safe to scrape unauthenticated on
    the internal network.  Rendering reads pre-aggregated counters only: it does
    no crawl computation and stays responsive while a crawl is running.
    """
    from fastapi import APIRouter, Response

    router = APIRouter(tags=["observability"])
    path = os.getenv(ENV_METRICS_PATH, DEFAULT_METRICS_PATH) or DEFAULT_METRICS_PATH

    @router.get(path, include_in_schema=False)
    async def metrics_endpoint() -> Response:  # noqa: D401 - fastapi handler
        """Prometheus exposition of the explorer's crawl-lifecycle metrics."""
        payload, content_type = render_latest()
        return Response(content=payload, media_type=content_type)

    return router


__all__ = [
    "PROM_AVAILABLE",
    "is_enabled",
    "get_registry",
    "render_latest",
    "build_metrics_router",
    "reset_for_tests",
    "terminal_reason_for",
    "oracle_state_for",
    # metric name constants
    "METRIC_BUILD_INFO",
    "METRIC_CRAWLS_STARTED",
    "METRIC_CRAWLS_COMPLETED",
    "METRIC_CRAWLS_FAILED",
    "METRIC_CRAWLS_CANCELLED",
    "METRIC_CRAWLS_TIMED_OUT",
    "METRIC_CRAWLS_NO_ORACLE",
    "METRIC_CRAWL_DURATION",
    "METRIC_CRAWL_MAX_DEPTH",
    "METRIC_CRAWL_DEEPEST_DEPTH",
    "METRIC_CRAWL_STATES",
    "METRIC_CRAWL_IN_FLIGHT",
    "METRIC_ORACLE_CALLS",
    "METRIC_ORACLE_FAILURES",
    "METRIC_ORACLE_LATENCY",
    "METRIC_DISPATCH_ATTEMPTS",
    "METRIC_DISPATCH_409",
    "METRIC_LLM_CALLS",
    "METRIC_LLM_TOKENS",
    "METRIC_LLM_COST_USD",
    "METRIC_GUARD_BLOCKS",
    "EXPECTED_METRIC_NAMES",
    # bounded vocabularies
    "TERMINAL_REASONS",
    "STOP_REASON_TO_TERMINAL",
    "ORACLE_STATES",
    "ORACLE_USED",
    "ORACLE_UNCONFIGURED",
    "ORACLE_CONFIGURED_UNUSED",
    "MAX_LABEL_VALUES_PER_KEY",
    # recording helpers
    "set_build_info",
    "record_dispatch",
    "record_crawl_started",
    "record_crawl_terminal",
    "record_oracle_call",
    "record_llm_usage",
    # env knobs
    "ENV_METRICS_ENABLED",
    "ENV_METRICS_PATH",
    "DEFAULT_METRICS_PATH",
]
