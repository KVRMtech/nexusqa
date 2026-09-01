"""QE-Central — HTTP retry/backoff for the VKPower factory clients (Phase 5.5).

The factory clients (``clients/factory.py`` / ``clients/platform_api.py``) already
bound every call with a per-call timeout, but a single transient blip (a dropped
connection, a read timeout, an upstream 502/503/504 while platform-api rolls) then
fails the whole cycle.  This module adds a small, SURGICAL retry wrapper the
factory client can drape over its calls — **without** changing any existing
behaviour: a caller that does not opt in keeps the exact single-attempt semantics.

Design (fail-safe, conservative, honest):

  * **Only idempotent/safe operations are retried.**  ``call_with_retries`` takes
    an explicit ``idempotent: bool`` the CALLER must set from first-hand knowledge
    of the operation.  A non-idempotent call (``idempotent=False`` — e.g. a
    ``POST …/generate`` that materialises grounded cases and may double-submit) is
    executed EXACTLY ONCE and its failure propagates untouched.  We never guess.
  * **Only transient failures are retried.**  The default classifier retries
    transport-layer failures (surfaced by the factory idiom as
    :class:`~app.clients.factory.FactoryClientError` with ``status_code == 0`` —
    connect errors / timeouts) and the transient upstream statuses 502/503/504.
    Any deterministic status (400/401/403/404/409/422/5xx-non-transient) is NOT
    retried — retrying it would only amplify a real error.
  * **Bounded exponential backoff with equal jitter.**  Delay for attempt *n* is
    ``min(max_delay, base * 2**(n-1))`` scaled into ``[0.5×, 1.0×]`` by a jitter
    factor, so a fleet of clients never re-collides in lockstep and the total wait
    is provably bounded by ``max_retries × max_delay``.
  * **Deterministic under test.**  Both the async ``sleep`` and the ``rand``
    source are injectable, so a test can record the exact backoff sequence and pin
    it, and ``asyncio.CancelledError`` is always re-raised (never retried).

Every knob is env-driven with a today-preserving default:

  ================================  =======  =========================================
  Env var                           Default  Meaning
  ================================  =======  =========================================
  ``QEC_HTTP_MAX_RETRIES``          ``2``    extra attempts for an idempotent call
  ``QEC_HTTP_RETRY_BASE_DELAY_S``   ``0.2``  first backoff (seconds), doubles per retry
  ``QEC_HTTP_RETRY_MAX_DELAY_S``    ``5.0``  per-attempt backoff ceiling (seconds)
  ================================  =======  =========================================
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
from typing import Awaitable, Callable, Optional, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

# ── Retry classification ───────────────────────────────────────────────────
#: Upstream HTTP statuses treated as transient (safe to retry an idempotent op).
#: Deliberately conservative — 429 is EXCLUDED (a politeness/backpressure signal
#: the control plane must respect, not hammer through) as are all 4xx.
TRANSIENT_HTTP_STATUSES = frozenset({502, 503, 504})

#: The factory clients surface a transport-layer failure (connect error, read
#: timeout) as ``FactoryClientError(status_code=0, …)``; that sentinel is retryable
#: for an idempotent op (the request may never have reached the server).
_TRANSPORT_SENTINEL_STATUS = 0

#: Equal-jitter floor: the actual sleep is uniformly drawn from
#: ``[JITTER_FLOOR × capped, capped]`` so backoff is randomised yet still bounded.
_JITTER_FLOOR = 0.5

# ── Env defaults (read lazily; parse-safe → fall back + warn) ───────────────
_DEFAULT_MAX_RETRIES = 2
_DEFAULT_BASE_DELAY_S = 0.2
_DEFAULT_MAX_DELAY_S = 5.0


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        logger.warning("qec.resilience.bad_env_int", extra={"env_var": name, "raw": raw[:64]})
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        logger.warning("qec.resilience.bad_env_float", extra={"env_var": name, "raw": raw[:64]})
        return default


def env_max_retries() -> int:
    """Configured extra-attempt cap for idempotent calls (``QEC_HTTP_MAX_RETRIES``).

    Clamped to ``>= 0`` — a negative value can never turn into extra attempts.
    """
    return max(0, _env_int("QEC_HTTP_MAX_RETRIES", _DEFAULT_MAX_RETRIES))


def env_base_delay_s() -> float:
    """First-retry backoff seconds (``QEC_HTTP_RETRY_BASE_DELAY_S``), clamped >= 0."""
    return max(0.0, _env_float("QEC_HTTP_RETRY_BASE_DELAY_S", _DEFAULT_BASE_DELAY_S))


def env_max_delay_s() -> float:
    """Per-attempt backoff ceiling seconds (``QEC_HTTP_RETRY_MAX_DELAY_S``), >= 0."""
    return max(0.0, _env_float("QEC_HTTP_RETRY_MAX_DELAY_S", _DEFAULT_MAX_DELAY_S))


def default_is_retryable(exc: BaseException) -> bool:
    """Classify ``exc`` as a transient failure worth retrying (idempotent ops only).

    Retryable:
      * anything carrying a ``status_code`` of ``0`` (the factory transport
        sentinel — connect error / timeout) or one of :data:`TRANSIENT_HTTP_STATUSES`
        (502/503/504); this covers :class:`FactoryClientError` without importing it;
      * a raw ``httpx.TransportError`` (connect/read/write/timeout) when a caller
        wraps httpx directly rather than the factory helper.

    Everything else — deterministic statuses, ``asyncio.CancelledError``, bugs — is
    NOT retryable (returns ``False``) so a real error surfaces immediately.
    """
    # Never retry cooperative cancellation — it must propagate promptly.
    if isinstance(exc, asyncio.CancelledError):
        return False

    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and not isinstance(status, bool):
        if status == _TRANSPORT_SENTINEL_STATUS:
            return True
        return status in TRANSIENT_HTTP_STATUSES

    # Best-effort raw-httpx recognition (import-guarded — httpx is a hard dep of
    # the service, but the resilience helper stays usable without it).
    try:
        import httpx
    except Exception:  # pragma: no cover - httpx always present in the service
        return False
    return isinstance(exc, httpx.TransportError)


def _backoff_delay(
    attempt: int,
    *,
    base_delay_s: float,
    max_delay_s: float,
    rand: Callable[[], float],
) -> float:
    """Equal-jitter exponential backoff for a 1-indexed ``attempt`` number.

    ``capped = min(max_delay, base × 2**(attempt-1))``; the returned delay is
    uniformly drawn from ``[JITTER_FLOOR × capped, capped]`` (so with ``rand()``
    pinned to ``1.0`` a test sees the exact ``capped`` sequence).  Always
    non-negative and never exceeds ``max_delay_s``.
    """
    if base_delay_s <= 0 or max_delay_s <= 0:
        return 0.0
    exp = base_delay_s * (2.0 ** max(0, attempt - 1))
    capped = min(max_delay_s, exp)
    # random.random() → [0,1); clamp defensively in case an injected rand strays.
    r = min(1.0, max(0.0, float(rand())))
    return capped * (_JITTER_FLOOR + (1.0 - _JITTER_FLOOR) * r)


async def call_with_retries(
    fn: Callable[[], Awaitable[T]],
    *,
    idempotent: bool,
    max_retries: Optional[int] = None,
    base_delay_s: Optional[float] = None,
    max_delay_s: Optional[float] = None,
    is_retryable: Optional[Callable[[BaseException], bool]] = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    rand: Callable[[], float] = random.random,
    op: str = "http",
) -> T:
    """Invoke ``fn`` (a zero-arg async thunk) with bounded transient-failure retries.

    Args:
      fn: a zero-argument callable returning an awaitable — typically
        ``lambda: _call(method="GET", …)``.  It is (re)invoked from scratch each
        attempt so no per-attempt state leaks.
      idempotent: MUST be ``True`` only for a safe/repeatable operation (a GET, a
        keyed read, a poll).  When ``False`` the call is executed EXACTLY ONCE and
        any exception propagates unretried — the guard against double-submitting a
        non-idempotent ``POST``.
      max_retries: extra attempts on top of the first (idempotent only); defaults
        to :func:`env_max_retries` (``QEC_HTTP_MAX_RETRIES``, default 2).
      base_delay_s / max_delay_s: backoff shape; default to the env knobs.
      is_retryable: exception classifier; defaults to :func:`default_is_retryable`.
      sleep: awaitable delay fn (injected in tests to record the backoff sequence).
      rand: ``[0,1)`` source for jitter (injected in tests for determinism).
      op: short label for structured retry logs (e.g. ``"rtm"``).

    Returns:
      Whatever ``fn`` returns on the first success.

    Raises:
      The exception from the final attempt — the original error type is preserved
      (e.g. :class:`FactoryClientError`), never wrapped, so callers keep mapping
      upstream status faithfully.
    """
    attempts_cap = 1 + (max(0, env_max_retries() if max_retries is None else max_retries)
                        if idempotent else 0)
    base = env_base_delay_s() if base_delay_s is None else max(0.0, base_delay_s)
    ceil = env_max_delay_s() if max_delay_s is None else max(0.0, max_delay_s)
    classify = is_retryable or default_is_retryable

    attempt = 0
    while True:
        attempt += 1
        try:
            return await fn()
        except asyncio.CancelledError:
            # Cooperative cancellation is never a transient error — propagate now.
            raise
        except Exception as exc:  # noqa: BLE001 - reclassified below, re-raised if not transient
            last_attempt = attempt >= attempts_cap
            if not idempotent or last_attempt or not classify(exc):
                if idempotent and last_attempt and attempt > 1:
                    logger.warning(
                        "qec.resilience.exhausted",
                        extra={"op": op, "attempts": attempt,
                               "error": str(exc)[:200]},
                    )
                raise
            delay = _backoff_delay(
                attempt, base_delay_s=base, max_delay_s=ceil, rand=rand,
            )
            logger.info(
                "qec.resilience.retry",
                extra={"op": op, "attempt": attempt, "attempts_cap": attempts_cap,
                       "delay_s": round(delay, 4), "error": str(exc)[:200]},
            )
            if delay > 0:
                await sleep(delay)


__all__ = [
    "call_with_retries",
    "default_is_retryable",
    "TRANSIENT_HTTP_STATUSES",
    "env_max_retries",
    "env_base_delay_s",
    "env_max_delay_s",
]
