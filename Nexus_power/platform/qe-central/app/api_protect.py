"""QE-Central — API-layer protections for the control plane (Phase 5.5).

Phases 0-5 shipped a fail-closed JWT gate and security headers but left three
production seams open on QE-Central's OWN API surface, all of which this module
closes as ADDITIVE, wire-able middleware/handlers (``app/main.py`` is NOT edited
here — the Wire agent mounts these):

  1. **Correlation IDs** (:func:`request_id_middleware`) — every request gets a
     stable ``request_id`` (echoing an inbound ``X-Request-ID`` when present, else
     a fresh UUID) on ``request.state`` and on the response header, so a log line,
     a 500 body, and a downstream trace all share one id.  Harmless when mounted;
     costs one UUID per request.

  2. **A global exception handler** (:func:`unhandled_exception_handler`) — maps any
     *unhandled* exception to a clean ``500`` carrying ONLY ``{detail, request_id}``.
     The internal message/stack is NEVER leaked to the client; it is logged with the
     full traceback under the correlation id.  ``HTTPException`` is untouched (it
     keeps its own status via Starlette's dedicated handler), so a deliberate
     ``403``/``404`` never becomes a ``500``.

  3. **A per-principal rate limiter** (:func:`principal_rate_limit_middleware`) —
     an in-process token bucket keyed by the JWT principal (``tenant_id:sub``, or
     the client IP when unauthenticated) that protects the control plane from a hot
     caller.  It is **OFF by default** (``QEC_API_RATE_LIMIT`` unset/≤0 ⇒ fully
     inert pass-through) so existing behaviour is byte-for-byte preserved; when
     enabled it admits a burst then throttles, returning ``429`` + ``Retry-After``.

Every knob is env-driven with a today-preserving default:

  ==================================  ========  ============================================
  Env var                             Default   Meaning
  ==================================  ========  ============================================
  ``QEC_API_RATE_LIMIT``              ``0``     per-principal req/sec; ``0`` ⇒ limiter OFF
  ``QEC_API_RATE_BURST``              ``2.0``   burst factor (bucket capacity = rate × this)
  ``QEC_API_RATE_MAX_PRINCIPALS``     ``10000`` LRU cap on tracked principals (bounded RAM)
  ==================================  ========  ============================================

Wiring order (documented for the Wire agent): the rate limiter reads the JWT
principal from ``request.state.user``, so mount it to run AFTER
``jwt_auth_middleware`` — i.e. register it BEFORE ``jwt_auth_middleware`` (Starlette
runs http middleware in reverse registration order).  It degrades safely to an IP
key if it runs before auth.
"""
from __future__ import annotations

import logging
import math
import os
import time
import uuid
from collections import OrderedDict
from typing import Callable, Optional

from fastapi import Request
from fastapi.responses import JSONResponse

from .auth import PUBLIC_PATHS

logger = logging.getLogger(__name__)

#: Correlation-id request/response header (industry-standard spelling).
REQUEST_ID_HEADER = "X-Request-ID"

#: Env defaults (read lazily so tests/deploys can flip them per-process).
_DEFAULT_RATE_PER_SEC = 0.0        # OFF — preserves today's single-instance behaviour.
_DEFAULT_BURST_FACTOR = 2.0
_DEFAULT_MAX_PRINCIPALS = 10_000


# ── Env parsing (parse-safe → fall back + warn) ─────────────────────────────
def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        logger.warning("qec.api_protect.bad_env_float", extra={"env_var": name, "raw": raw[:64]})
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        logger.warning("qec.api_protect.bad_env_int", extra={"env_var": name, "raw": raw[:64]})
        return default


# ── Correlation id ──────────────────────────────────────────────────────────
def get_request_id(request: Request) -> str:
    """Return this request's correlation id, minting + storing one if absent.

    Prefers an already-stored ``request.state.request_id`` (set by
    :func:`request_id_middleware`), then an inbound ``X-Request-ID`` header, then a
    fresh UUID4 hex.  Idempotent: the resolved id is written back to
    ``request.state`` so every later reader (logs, the exception handler) agrees.
    """
    existing = getattr(request.state, "request_id", None)
    if isinstance(existing, str) and existing:
        return existing
    inbound = (request.headers.get(REQUEST_ID_HEADER) or "").strip()
    rid = inbound[:128] if inbound else uuid.uuid4().hex
    request.state.request_id = rid
    return rid


async def request_id_middleware(request: Request, call_next):
    """Attach a correlation id to ``request.state`` and the response header.

    Additive and side-effect-free beyond the header: it never rejects a request.
    """
    rid = get_request_id(request)
    response = await call_next(request)
    response.headers[REQUEST_ID_HEADER] = rid
    return response


# ── Global exception handler ────────────────────────────────────────────────
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Map any unhandled exception to a clean ``500`` — no internal detail leaked.

    Register with ``app.add_exception_handler(Exception, unhandled_exception_handler)``.
    The response body is exactly ``{"detail": "Internal Server Error", "request_id": …}``;
    the real error + traceback are logged (never returned).  ``HTTPException`` is
    handled by Starlette's own handler and never reaches here, so intentional 4xx/
    non-500 responses are unaffected.
    """
    rid = get_request_id(request)
    logger.error(
        "qec.api.unhandled_error",
        extra={
            "request_id": rid,
            "method": request.method,
            "path": request.url.path,
            "error_type": type(exc).__name__,
        },
        exc_info=exc,
    )
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal Server Error", "request_id": rid},
        headers={REQUEST_ID_HEADER: rid},
    )


# ── Per-principal token bucket (starts FULL — API burst-then-throttle) ──────
class _ApiTokenBucket:
    """A monotonic-clock token bucket that starts FULL (capacity tokens).

    Distinct from ``scheduling.admission.TokenBucket`` (which starts EMPTY for
    anti-burst crawler politeness): a *self-protection* API limiter must let normal
    traffic through immediately and only throttle a genuinely hot caller, so it
    begins with a full ``capacity = max(1, rate × burst_factor)``.
    """

    __slots__ = ("rate", "capacity", "tokens", "_clock", "_updated")

    def __init__(self, rate: float, *, burst_factor: float, clock: Callable[[], float]) -> None:
        self.rate = float(rate)
        self.capacity = max(1.0, self.rate * max(1.0, float(burst_factor)))
        self.tokens = self.capacity  # START FULL.
        self._clock = clock
        self._updated = clock()

    def _refill(self) -> None:
        now = self._clock()
        elapsed = now - self._updated
        if elapsed > 0 and self.rate > 0:
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        self._updated = now

    def try_consume(self, amount: float = 1.0) -> tuple[bool, float]:
        """Consume ``amount`` if available. Return ``(allowed, retry_after_seconds)``.

        Purely synchronous (no ``await``) so it is atomic within one asyncio step —
        concurrent requests on the same principal cannot interleave a refill with a
        consume, so no lock is required.
        """
        if self.rate <= 0:
            return True, 0.0  # limiter disabled for this bucket — always allow.
        self._refill()
        if self.tokens + 1e-9 >= amount:
            self.tokens -= amount
            return True, 0.0
        deficit = amount - self.tokens
        return False, deficit / self.rate if self.rate > 0 else 0.0


class PrincipalRateLimiter:
    """Bounded, in-process, per-principal API rate limiter (self-protection).

    Holds an LRU-capped map of principal → :class:`_ApiTokenBucket`.  ``rate <= 0``
    disables it entirely (:attr:`enabled` is ``False`` and :meth:`allow` always
    admits) — the default, so an unconfigured deployment behaves exactly as before.
    """

    def __init__(
        self,
        *,
        rate_per_sec: float,
        burst_factor: float = _DEFAULT_BURST_FACTOR,
        max_principals: int = _DEFAULT_MAX_PRINCIPALS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.rate_per_sec = max(0.0, float(rate_per_sec))
        self.burst_factor = max(1.0, float(burst_factor))
        self.max_principals = max(1, int(max_principals))
        self._clock = clock
        self._buckets: "OrderedDict[str, _ApiTokenBucket]" = OrderedDict()

    @property
    def enabled(self) -> bool:
        """True only when a positive per-principal rate is configured."""
        return self.rate_per_sec > 0

    def allow(self, principal: str) -> tuple[bool, float]:
        """Admit one request for ``principal``; return ``(allowed, retry_after_s)``.

        When disabled, always ``(True, 0.0)``.  Otherwise consumes one token from the
        principal's bucket (created full on first sight) and evicts the least-recently
        used principal when the map exceeds :attr:`max_principals` (bounded memory).
        """
        if not self.enabled:
            return True, 0.0
        bucket = self._buckets.get(principal)
        if bucket is None:
            bucket = _ApiTokenBucket(
                self.rate_per_sec, burst_factor=self.burst_factor, clock=self._clock,
            )
            self._buckets[principal] = bucket
            # Bound memory: evict the oldest principal once over the cap.
            while len(self._buckets) > self.max_principals:
                self._buckets.popitem(last=False)
        else:
            self._buckets.move_to_end(principal)
        return bucket.try_consume(1.0)

    def snapshot(self) -> dict:
        """Observability snapshot (no secrets): config + tracked-principal count."""
        return {
            "enabled": self.enabled,
            "rate_per_sec": self.rate_per_sec,
            "burst_factor": self.burst_factor,
            "max_principals": self.max_principals,
            "tracked_principals": len(self._buckets),
        }


def build_api_rate_limiter() -> PrincipalRateLimiter:
    """Build the process limiter from the environment (§Phase-5.5 knobs).

    ``QEC_API_RATE_LIMIT`` (req/sec/principal; default ``0`` ⇒ OFF),
    ``QEC_API_RATE_BURST`` (burst factor; default ``2.0``),
    ``QEC_API_RATE_MAX_PRINCIPALS`` (LRU cap; default ``10000``).
    """
    return PrincipalRateLimiter(
        rate_per_sec=_env_float("QEC_API_RATE_LIMIT", _DEFAULT_RATE_PER_SEC),
        burst_factor=_env_float("QEC_API_RATE_BURST", _DEFAULT_BURST_FACTOR),
        max_principals=_env_int("QEC_API_RATE_MAX_PRINCIPALS", _DEFAULT_MAX_PRINCIPALS),
    )


#: Process-wide singleton — import as ``from app.api_protect import API_RATE_LIMITER``.
API_RATE_LIMITER = build_api_rate_limiter()


def _principal_key(request: Request) -> str:
    """Derive the rate-limit key: the JWT principal, else the client IP.

    Prefers ``request.state.user`` (populated by ``jwt_auth_middleware``) as
    ``tenant_id:sub`` — the true per-principal identity.  Falls back to ``ip:<host>``
    when auth has not run/populated a user, so the limiter still bounds an
    unauthenticated flood without depending on middleware ordering.
    """
    user = getattr(request.state, "user", None)
    if isinstance(user, dict):
        tid = str(user.get("tenant_id") or "").strip()
        sub = str(user.get("sub") or "").strip()
        if tid or sub:
            return f"{tid}:{sub}"
    client = request.client
    host = client.host if client is not None else "unknown"
    return f"ip:{host}"


def _is_limitable(path: str) -> bool:
    """Whether ``path`` is subject to the API limiter (only QE-Central's own API).

    Public paths (health/docs) and non-``/api/`` paths are never limited — parity
    with the JWT middleware's skip set so liveness probes are never throttled.
    """
    stripped = path.rstrip("/")
    if stripped in PUBLIC_PATHS or stripped.startswith("/docs") or stripped.startswith("/redoc"):
        return False
    return stripped.startswith("/api/")


def make_principal_rate_limit_middleware(
    limiter: Optional[PrincipalRateLimiter] = None,
):
    """Build the rate-limit middleware bound to ``limiter`` (default: the singleton).

    Returns an ``async (request, call_next)`` callable for
    ``app.middleware("http")(...)``.  When the limiter is disabled the middleware is
    a near-zero-cost pass-through (a single boolean check); when enabled it returns
    ``429`` + ``Retry-After`` for a principal over its bucket, tagged with the
    correlation id for traceability.
    """
    active = limiter if limiter is not None else API_RATE_LIMITER

    async def _middleware(request: Request, call_next):
        if not active.enabled or not _is_limitable(request.url.path):
            return await call_next(request)
        allowed, retry_after = active.allow(_principal_key(request))
        if not allowed:
            rid = get_request_id(request)
            retry_s = max(1, int(math.ceil(retry_after)))
            logger.warning(
                "qec.api.rate_limited",
                extra={"request_id": rid, "path": request.url.path,
                       "principal": _principal_key(request), "retry_after_s": retry_s},
            )
            return JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded", "request_id": rid},
                headers={"Retry-After": str(retry_s), REQUEST_ID_HEADER: rid},
            )
        return await call_next(request)

    return _middleware


#: Ready-to-mount middleware bound to the process singleton (Wire-agent convenience).
principal_rate_limit_middleware = make_principal_rate_limit_middleware()


__all__ = [
    "REQUEST_ID_HEADER",
    "get_request_id",
    "request_id_middleware",
    "unhandled_exception_handler",
    "PrincipalRateLimiter",
    "build_api_rate_limiter",
    "API_RATE_LIMITER",
    "make_principal_rate_limit_middleware",
    "principal_rate_limit_middleware",
]
