"""QE-Explorer — crawl CORRELATION, lifecycle events and per-crawl token spend.

Prometheus deliberately holds no ``crawl_id`` (§10/§20: that label is unbounded
and would turn the TSDB into an event database).  This module is the other half
of that architectural split — the place where the high-cardinality truth lives:

    Prometheus  → how many crawls, how deep, why they stopped, how many tokens,
                  aggregated by BOUNDED dimensions
    this module → WHICH crawl, its exact token spend, its exact oracle calls,
                  its exact terminal event — as structured logs and an
                  in-process per-crawl record

WHAT IT PROVIDES
================
  * :func:`bind_crawl` — binds ``crawl_id`` (+ safe context) to a
    :mod:`contextvars` context and to structlog, so EVERY log line emitted while
    the crawl runs carries it without being threaded through call signatures.
    Correlation is propagated, never reconstructed after the fact.
  * :func:`crawl_headers` — merges the bound ``crawl_id`` / correlation id onto
    an outbound httpx header dict, so the id continues across the oracle HTTP
    hop into qe-central and on to the LLM boundary.  One id spans
    ``dispatch → explorer → oracle → LLM → completion``.
  * :func:`emit` — one structured lifecycle event with a fixed vocabulary
    (:data:`LIFECYCLE_EVENTS`), always carrying crawl id, correlation id and
    timestamp.
  * :class:`CrawlTokenUsage` — the canonical per-crawl spend record, aggregating
    every LLM call made on the crawl's behalf across success, error, retry,
    timeout and streaming paths.

SAFETY
======
Nothing in this module logs a prompt, a credential, an ``Authorization`` header,
an API key, raw PII, a screenshot or a full URL.  Lifecycle events carry
IDENTIFIERS, COUNTS, DURATIONS and BOUNDED OUTCOMES only; :func:`_safe_fields`
drops any field whose key matches :data:`_FORBIDDEN_KEY_RE` and truncates the
rest, so a future caller cannot casually widen the blast radius.
"""
from __future__ import annotations

import contextvars
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

logger = logging.getLogger("qe-explorer.telemetry")


# ── Correlation headers (shared vocabulary with qe-central) ────────────────
#: qe-central's CorrelationIdMiddleware reads/echoes this header.
HEADER_REQUEST_ID = "X-Request-ID"
#: The crawl this call is being made on behalf of — the join key between the
#: explorer's logs, qe-central's oracle logs and the LLM token record.
HEADER_CRAWL_ID = "X-QEC-Crawl-ID"

_CRAWL_ID: "contextvars.ContextVar[str]" = contextvars.ContextVar(
    "qec_explorer_crawl_id", default="")
_CORRELATION_ID: "contextvars.ContextVar[str]" = contextvars.ContextVar(
    "qec_explorer_correlation_id", default="")
_TENANT_ID: "contextvars.ContextVar[str]" = contextvars.ContextVar(
    "qec_explorer_tenant_id", default="")


# ── The lifecycle event vocabulary (bounded, greppable) ────────────────────
EV_CRAWL_ACCEPTED = "crawl_accepted"
EV_CRAWL_REFUSED = "crawl_refused"
EV_CRAWL_STARTED = "crawl_started"
EV_EXPLORER_STARTED = "explorer_started"
EV_EXPLORER_STOPPED = "explorer_stopped"
EV_ORACLE_CALLED = "oracle_called"
EV_ORACLE_COMPLETED = "oracle_completed"
EV_LLM_CALLED = "llm_called"
EV_LLM_COMPLETED = "llm_completed"
EV_CRAWL_TERMINAL = "crawl_terminal"
EV_CRAWL_FAILED = "crawl_failed"
EV_CRAWL_CANCELLED = "crawl_cancelled"
EV_CRAWL_TOKENS = "crawl_tokens"

LIFECYCLE_EVENTS = frozenset({
    EV_CRAWL_ACCEPTED, EV_CRAWL_REFUSED, EV_CRAWL_STARTED,
    EV_EXPLORER_STARTED, EV_EXPLORER_STOPPED,
    EV_ORACLE_CALLED, EV_ORACLE_COMPLETED,
    EV_LLM_CALLED, EV_LLM_COMPLETED,
    EV_CRAWL_TERMINAL, EV_CRAWL_FAILED, EV_CRAWL_CANCELLED, EV_CRAWL_TOKENS,
})

#: Field keys that must never reach a log line, whatever a caller passes.  The
#: check is on the KEY because the value is exactly what we must not inspect.
_FORBIDDEN_KEY_RE = re.compile(
    r"(prompt|secret|token|password|passwd|credential|authorization|auth_header"
    r"|api_key|apikey|cookie|session|screenshot|image|pii|email|ssn|dob"
    r"|answer_key|creds)",
    re.IGNORECASE,
)
#: Upper bound on a single logged field's rendered length.
_MAX_FIELD_LEN = 200


def _sanitize_id(value: Optional[str], *, max_len: int = 64) -> str:
    """Return ``value`` if it is a safe identifier, else ``""``.

    Ids travel into log lines and outbound headers, so anything outside
    ``[A-Za-z0-9._-]`` or over ``max_len`` is rejected rather than escaped — we
    never emit unbounded caller-controlled bytes.
    """
    if not value:
        return ""
    candidate = str(value).strip()
    if not candidate or len(candidate) > max_len:
        return ""
    return candidate if re.fullmatch(r"[A-Za-z0-9._\-]+", candidate) else ""


#: Public alias — callers outside this module (e.g. the dispatch-refusal event,
#: which has no bound context to read an id from) sanitise through this.
sanitize_id = _sanitize_id


def mint_correlation_id() -> str:
    """Mint a fresh correlation id (uuid4 hex — url/log-safe, 32 chars)."""
    return uuid.uuid4().hex


def current_crawl_id() -> str:
    """The crawl id bound to the current context, or ``""``."""
    return _CRAWL_ID.get("")


def current_correlation_id() -> str:
    """The correlation id bound to the current context, or ``""``."""
    return _CORRELATION_ID.get("")


def current_tenant_id() -> str:
    """The tenant id bound to the current context, or ``""``."""
    return _TENANT_ID.get("")


def bind_crawl(
    *, crawl_id: str, tenant_id: str = "", correlation_id: str = "",
) -> None:
    """Bind this crawl's identity to the current context (and to structlog).

    Called once at the top of the crawl task.  Every later ``emit`` — including
    ones made deep inside the crawler, an oracle callback or a failure handler —
    picks the identity up from the context, so no code path can emit an event
    that cannot be joined back to its crawl.  Deliberately NOT reset per call:
    the explorer runs one crawl at a time per process (single-flight), and the
    binding lives for the crawl task's lifetime.
    """
    safe_crawl = _sanitize_id(crawl_id)
    _CRAWL_ID.set(safe_crawl)
    _TENANT_ID.set(_sanitize_id(tenant_id))
    _CORRELATION_ID.set(_sanitize_id(correlation_id) or mint_correlation_id())
    try:
        import structlog

        structlog.contextvars.bind_contextvars(
            crawl_id=safe_crawl,
            correlation_id=current_correlation_id(),
        )
    except Exception:  # structlog absent/old — the ContextVars are the SoT.
        logger.debug("qec.explorer.structlog_bind_skipped", exc_info=True)


def crawl_headers(headers: Optional[Mapping[str, str]] = None) -> dict:
    """Merge the bound crawl/correlation ids onto an outbound header dict.

    This is how correlation crosses the process boundary: the oracle HTTP call
    to qe-central carries the crawl id, qe-central's ``CorrelationIdMiddleware``
    picks up the request id, and the token usage recorded at the LLM boundary is
    attributable to THIS crawl without anyone reconstructing it afterwards.
    Adds nothing when no id is bound — never a fabricated id.
    """
    merged: dict = dict(headers or {})
    crawl_id = current_crawl_id()
    correlation_id = current_correlation_id()
    if crawl_id:
        merged[HEADER_CRAWL_ID] = crawl_id
    if correlation_id:
        merged[HEADER_REQUEST_ID] = correlation_id
    return merged


def _safe_fields(fields: Mapping[str, Any]) -> dict:
    """Drop forbidden keys and truncate every remaining value.

    Fail-closed on the KEY name: a field called ``prompt_tokens`` is a count and
    is kept, but ``prompt`` / ``prompt_text`` are dropped.  (``prompt_tokens``
    survives because the pattern is matched against the whole key and
    ``_ALLOWED_TOKEN_FIELDS`` re-admits the counts.)
    """
    out: dict = {}
    for key, value in (fields or {}).items():
        k = str(key)
        if k in _ALLOWED_TOKEN_FIELDS:
            out[k] = value
            continue
        if _FORBIDDEN_KEY_RE.search(k):
            continue
        if isinstance(value, (int, float, bool)) or value is None:
            out[k] = value
        else:
            out[k] = str(value)[:_MAX_FIELD_LEN]
    return out


#: Token COUNT fields whose names collide with the forbidden-key pattern
#: (``prompt``/``token``) but which are exactly what this milestone must log.
_ALLOWED_TOKEN_FIELDS = frozenset({
    "prompt_tokens", "completion_tokens", "total_tokens",
    "cache_read_tokens", "cache_creation_tokens", "llm_calls",
})


def emit(event: str, **fields: Any) -> None:
    """Emit ONE structured lifecycle event, correlated to the current crawl.

    Rendered as a single greppable ``qec.crawl.event`` line with a JSON payload
    so it is machine-readable wherever the container's stdout lands, without
    requiring a structlog processor chain to be configured.  Never raises: a
    telemetry failure must not become a crawl failure.
    """
    try:
        name = event if event in LIFECYCLE_EVENTS else "unknown_event"
        payload = {
            "event": name,
            "ts": time.time(),
            "crawl_id": current_crawl_id(),
            "correlation_id": current_correlation_id(),
            "tenant_id": current_tenant_id(),
            **_safe_fields(fields),
        }
        logger.info("qec.crawl.event %s", json.dumps(payload, sort_keys=True,
                                                     default=str))
    except Exception:  # noqa: BLE001
        logger.debug("qec.explorer.event_emit_failed", exc_info=True)


# ── Per-crawl token spend ──────────────────────────────────────────────────


@dataclass
class ProviderSpend:
    """Per (provider, model) breakdown inside one crawl's spend record."""

    provider: str = ""
    model: str = ""
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        """``prompt + completion`` — the cache counters are NOT added in.

        Cache-read/creation tokens are a separate provider accounting dimension;
        folding them into the total would double-count the same prompt.
        """
        return self.prompt_tokens + self.completion_tokens

    def as_dict(self) -> dict:
        return {
            "provider": self.provider, "model": self.model, "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
        }


@dataclass
class CrawlTokenUsage:
    """The canonical per-crawl LLM spend record.

    Accumulates EVERY LLM call made on the crawl's behalf.  The paths that make
    this correct rather than merely present:

      * **success** — the provider-reported usage is recorded verbatim.
      * **error** — if the provider returned usage BEFORE failing, it is still
        recorded (``outcome="error_with_usage"``); the request was billed, so
        dropping it would understate spend exactly when spend is anomalous.
      * **retry** — each ATTEMPT is its own :meth:`record` call, so ``calls``
        counts attempts, not logical requests.  A retried request that consumed
        prompt tokens twice reports both.
      * **timeout** — recorded with whatever usage arrived (often zero), so a
        timing-out provider is visible as calls-without-tokens rather than
        silence.
      * **streaming** — the caller aggregates the stream's final usage frame and
        records ONCE; :meth:`record` is additive, so a caller that instead
        records per-chunk deltas also totals correctly.

    Token counts are always provider-REPORTED.  Nothing here estimates a token
    count: a call whose provider reported no usage contributes to ``calls`` and
    to ``calls_missing_usage``, and is visible as a gap rather than papered over
    with a guess.
    """

    crawl_id: str = ""
    calls: int = 0
    #: Calls the provider gave NO usage for — the honest denominator for "is
    #: this spend figure complete?".
    calls_missing_usage: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    #: ESTIMATED spend, accumulated only from a caller-supplied price. Kept as a
    #: separate field from every token count: tokens are a COUNT and dollars are
    #: a CURRENCY, and the two must never be read as the same number.
    estimated_cost_usd: float = 0.0
    by_provider_model: dict = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        """``prompt_tokens + completion_tokens`` (cache counters excluded)."""
        return self.prompt_tokens + self.completion_tokens

    def record(
        self,
        *,
        provider: str = "",
        model: str = "",
        prompt_tokens: Optional[int] = None,
        completion_tokens: Optional[int] = None,
        cache_read_tokens: Optional[int] = None,
        cache_creation_tokens: Optional[int] = None,
        estimated_cost_usd: float = 0.0,
    ) -> None:
        """Fold ONE LLM attempt's provider-reported usage into the record.

        ``None`` means "the provider did not report this", which is different
        from ``0`` ("it reported zero") — only the former counts toward
        :attr:`calls_missing_usage`.
        """
        self.calls += 1
        reported = [t for t in (prompt_tokens, completion_tokens) if t is not None]
        if not reported:
            self.calls_missing_usage += 1

        p = max(0, int(prompt_tokens or 0))
        c = max(0, int(completion_tokens or 0))
        cr = max(0, int(cache_read_tokens or 0))
        cc = max(0, int(cache_creation_tokens or 0))

        self.prompt_tokens += p
        self.completion_tokens += c
        self.cache_read_tokens += cr
        self.cache_creation_tokens += cc
        self.estimated_cost_usd += max(0.0, float(estimated_cost_usd or 0.0))

        key = f"{provider or 'unknown'}::{model or 'unknown'}"
        entry = self.by_provider_model.get(key)
        if entry is None:
            entry = ProviderSpend(provider=provider or "unknown",
                                  model=model or "unknown")
            self.by_provider_model[key] = entry
        entry.calls += 1
        entry.prompt_tokens += p
        entry.completion_tokens += c
        entry.cache_read_tokens += cr
        entry.cache_creation_tokens += cc

    def as_dict(self) -> dict:
        return {
            "crawl_id": self.crawl_id,
            "llm_calls": self.calls,
            "calls_missing_usage": self.calls_missing_usage,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
            "estimated_cost_usd": round(self.estimated_cost_usd, 6),
            "by_provider_model": [
                v.as_dict() for v in self.by_provider_model.values()
            ],
        }

    def emit_summary(self) -> None:
        """Emit the crawl's spend as one ``crawl_tokens`` lifecycle event."""
        emit(EV_CRAWL_TOKENS, **{
            k: v for k, v in self.as_dict().items()
            if k != "by_provider_model"
        }, providers=len(self.by_provider_model))


def usage_from_response(body: Optional[Mapping[str, Any]]) -> dict:
    """Normalise a heterogeneous LLM/oracle response body into ONE usage shape.

    Providers disagree on names — OpenAI reports ``usage.prompt_tokens`` /
    ``completion_tokens``, Anthropic ``usage.input_tokens`` / ``output_tokens``,
    Ollama ``prompt_eval_count`` / ``eval_count`` — and the platform LLM router
    already folds those into ``prompt_tokens`` / ``completion_tokens``.  This
    reads BOTH the normalised field names and the raw provider spellings, at the
    top level or nested under ``usage``, so a usage field is never silently
    discarded because a hop reshaped the envelope.

    Returns ``{provider, model, prompt_tokens, completion_tokens,
    cache_read_tokens, cache_creation_tokens}`` with ``None`` for anything the
    body did not report — never an estimate.
    """
    src: dict = dict(body or {})
    nested = src.get("usage")
    if isinstance(nested, Mapping):
        merged = dict(nested)
        merged.update({k: v for k, v in src.items() if k != "usage"})
        src = merged

    def _first_int(*names: str) -> Optional[int]:
        for name in names:
            value = src.get(name)
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                return int(value)
            if isinstance(value, str) and value.strip().isdigit():
                return int(value.strip())
        return None

    def _first_str(*names: str) -> str:
        for name in names:
            value = src.get(name)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    return {
        "provider": _first_str("provider"),
        "model": _first_str("model"),
        "prompt_tokens": _first_int(
            "prompt_tokens", "input_tokens", "prompt_eval_count"),
        "completion_tokens": _first_int(
            "completion_tokens", "output_tokens", "eval_count"),
        "cache_read_tokens": _first_int(
            "cache_read_tokens", "cache_read_input_tokens"),
        "cache_creation_tokens": _first_int(
            "cache_creation_tokens", "cache_creation_input_tokens"),
    }


__all__ = [
    "HEADER_CRAWL_ID",
    "HEADER_REQUEST_ID",
    "LIFECYCLE_EVENTS",
    "EV_CRAWL_ACCEPTED", "EV_CRAWL_REFUSED", "EV_CRAWL_STARTED",
    "EV_EXPLORER_STARTED", "EV_EXPLORER_STOPPED",
    "EV_ORACLE_CALLED", "EV_ORACLE_COMPLETED",
    "EV_LLM_CALLED", "EV_LLM_COMPLETED",
    "EV_CRAWL_TERMINAL", "EV_CRAWL_FAILED", "EV_CRAWL_CANCELLED",
    "EV_CRAWL_TOKENS",
    "bind_crawl", "crawl_headers", "emit", "sanitize_id",
    "current_crawl_id", "current_correlation_id", "current_tenant_id",
    "mint_correlation_id",
    "CrawlTokenUsage", "ProviderSpend", "usage_from_response",
]
