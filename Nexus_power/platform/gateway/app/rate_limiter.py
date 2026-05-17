"""
API Gateway — Rate Limiter.

Sliding-window per-tenant per-minute rate limiter.
"""
from __future__ import annotations

import time
import threading
from collections import defaultdict


class RateLimiter:
    """Thread-safe sliding-window rate limiter."""

    def __init__(self, max_per_minute: int = 600):
        self.max_per_minute = max_per_minute
        self._buckets: dict[str, list[float]] = defaultdict(list)
        self._lock = threading.Lock()

    def allow(self, tenant_id: str) -> bool:
        """Return True if the request should be allowed."""
        if self.max_per_minute <= 0:
            return True
        now = time.monotonic()
        cutoff = now - 60.0
        with self._lock:
            bucket = self._buckets[tenant_id]
            self._buckets[tenant_id] = [t for t in bucket if t > cutoff]
            if len(self._buckets[tenant_id]) >= self.max_per_minute:
                return False
            self._buckets[tenant_id].append(now)
            return True
