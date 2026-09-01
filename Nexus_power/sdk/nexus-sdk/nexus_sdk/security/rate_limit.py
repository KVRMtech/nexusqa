"""
Nexus Rate Limiting Middleware — Sliding-window per-client rate limiter.

Limits requests per client IP using an in-memory sliding window.
Production deployments should front this with an API gateway rate limiter
(e.g. Kong, Envoy); this middleware acts as a last line of defense.

Configuration via environment variables:
    NEXUS_RATE_LIMIT_RPM        — requests per minute per client (default: 120)
    NEXUS_RATE_LIMIT_ENABLED    — "true" / "false" (default: true)

Usage:
    app.add_middleware(RateLimitMiddleware, rpm=100)
"""

from __future__ import annotations

import os
import time
from collections import defaultdict
from typing import Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

__all__ = ["RateLimitMiddleware"]

# Default: 120 requests per minute per client IP
_DEFAULT_RPM = 120
_WINDOW_SECONDS = 60.0

# Paths exempt from rate limiting (health checks, metrics)
_EXEMPT_PATHS = frozenset({"/health", "/health/ready", "/health/live", "/metrics"})


class _SlidingWindow:
    """Per-client sliding window tracker."""

    __slots__ = ("_timestamps",)

    def __init__(self) -> None:
        self._timestamps: list[float] = []

    def record_and_check(self, now: float, limit: int) -> bool:
        """Record a request and return True if within limit."""
        cutoff = now - _WINDOW_SECONDS
        # Prune old entries
        self._timestamps = [t for t in self._timestamps if t > cutoff]
        if len(self._timestamps) >= limit:
            return False
        self._timestamps.append(now)
        return True

    def remaining(self, now: float, limit: int) -> int:
        cutoff = now - _WINDOW_SECONDS
        active = sum(1 for t in self._timestamps if t > cutoff)
        return max(0, limit - active)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Sliding-window rate limiter per client IP.

    Returns 429 Too Many Requests when the client exceeds ``rpm``
    requests within a 60-second window.
    """

    def __init__(self, app, rpm: Optional[int] = None):
        super().__init__(app)
        env_rpm = os.environ.get("NEXUS_RATE_LIMIT_RPM", "")
        self.rpm = rpm or (int(env_rpm) if env_rpm.isdigit() else _DEFAULT_RPM)
        self.enabled = os.environ.get(
            "NEXUS_RATE_LIMIT_ENABLED", "true"
        ).lower() in ("true", "1", "yes")
        self._windows: dict[str, _SlidingWindow] = defaultdict(_SlidingWindow)

    def _client_key(self, request: Request) -> str:
        """Extract client identifier — prefers X-Forwarded-For behind proxy."""
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
        client = request.client
        return client.host if client else "unknown"

    async def dispatch(self, request: Request, call_next) -> Response:
        if not self.enabled:
            return await call_next(request)

        # Skip health/metrics endpoints
        if request.url.path in _EXEMPT_PATHS:
            return await call_next(request)

        client_ip = self._client_key(request)
        now = time.monotonic()
        window = self._windows[client_ip]

        if not window.record_and_check(now, self.rpm):
            return JSONResponse(
                status_code=429,
                content={
                    "error": "rate_limit_exceeded",
                    "detail": f"Rate limit of {self.rpm} requests per minute exceeded. Retry later.",
                },
                headers={
                    "Retry-After": "60",
                    "X-RateLimit-Limit": str(self.rpm),
                    "X-RateLimit-Remaining": "0",
                },
            )

        response = await call_next(request)
        remaining = window.remaining(time.monotonic(), self.rpm)
        response.headers["X-RateLimit-Limit"] = str(self.rpm)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        return response
