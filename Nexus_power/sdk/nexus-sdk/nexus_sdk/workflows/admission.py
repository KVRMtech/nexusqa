"""
Admission control for canonical-workflow creation.

Two responsibilities:

  1. Per-tenant token-bucket rate limiting. The Redis-backed
     `TenantRateLimiter` keeps a refill clock per tenant so a single
     tenant flooding the API doesn't starve the rest. The limits are
     configurable per tenant class (enterprise / pilot / unknown) via
     env vars; defaults are conservative.

  2. Idempotency. Clients post a request with header
     `Idempotency-Key: <client-supplied-uuid>`. The first request
     creates the workflow; subsequent requests within the TTL window
     return the same workflow_id (the dispatch happens at-most-once).
     A failed first request invalidates the key so the client can
     legitimately retry with the same id.

Both layers degrade safely: if Redis is unreachable the limiter
fail-opens (allow the request), and the idempotency cache becomes a
no-op (every request creates a new workflow). Operators should alert
on `nexus_admission_redis_unavailable` to catch sustained outages.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ─── Configuration ─────────────────────────────────────────────


@dataclass
class _LimitTier:
    rate_per_minute: float
    burst: int


_DEFAULT_TIERS: dict[str, _LimitTier] = {
    "enterprise": _LimitTier(rate_per_minute=600.0, burst=200),
    "pilot":      _LimitTier(rate_per_minute=120.0, burst=40),
    "internal":   _LimitTier(rate_per_minute=600.0, burst=200),
    "unknown":    _LimitTier(rate_per_minute=30.0,  burst=10),
}


def _tier_from_env(tier: str) -> _LimitTier:
    """Override defaults via env: NEXUS_ADMISSION_<tier>_RATE_PER_MINUTE / _BURST."""
    base = _DEFAULT_TIERS.get(tier, _DEFAULT_TIERS["unknown"])
    rate = os.environ.get(f"NEXUS_ADMISSION_{tier.upper()}_RATE_PER_MINUTE")
    burst = os.environ.get(f"NEXUS_ADMISSION_{tier.upper()}_BURST")
    try:
        return _LimitTier(
            rate_per_minute=float(rate) if rate else base.rate_per_minute,
            burst=int(burst) if burst else base.burst,
        )
    except ValueError:
        return base


# ─── Rate limiter ──────────────────────────────────────────────


class TenantRateLimiter:
    """Redis-backed token bucket. One bucket per (tenant_id, route).

    Falls back to fail-open (allow) when Redis is unreachable. Two
    keys per bucket: `<prefix>:tokens` (float) + `<prefix>:updated`
    (float epoch). A single Lua script would be ideal but pure-Python
    is enough until we measure contention.
    """

    KEY_PREFIX = "nexus:admission:bucket"

    def __init__(self, redis_client=None) -> None:
        self._r = redis_client

    @classmethod
    def _key(cls, tenant_id: str, route: str) -> str:
        return f"{cls.KEY_PREFIX}:{route}:{tenant_id}"

    async def acquire(
        self,
        tenant_id: str,
        tier: str,
        route: str = "workflow.create",
        cost: int = 1,
    ) -> tuple[bool, float, _LimitTier]:
        """Try to consume `cost` tokens. Returns (allowed, retry_after_seconds, tier).
        retry_after is 0 when allowed."""
        limit = _tier_from_env(tier)
        if self._r is None:
            return True, 0.0, limit
        now = time.time()
        key = self._key(tenant_id, route)
        try:
            raw = await self._r.hgetall(key)
        except Exception as e:
            logger.warning("admission.redis_get_failed err=%s", e)
            return True, 0.0, limit

        tokens = float(raw.get("tokens", limit.burst)) if raw else float(limit.burst)
        updated = float(raw.get("updated", now)) if raw else now

        # Refill — tokens added at rate/60 per second since last update.
        elapsed = max(0.0, now - updated)
        tokens = min(float(limit.burst), tokens + elapsed * (limit.rate_per_minute / 60.0))

        if tokens < cost:
            # Compute retry-after: how long until enough tokens accrue.
            needed = cost - tokens
            retry_after = needed / (limit.rate_per_minute / 60.0)
            return False, max(retry_after, 0.1), limit

        tokens -= cost
        try:
            await self._r.hset(key, mapping={"tokens": tokens, "updated": now})
            # 1-hour TTL so abandoned tenants don't accumulate state.
            await self._r.expire(key, 3600)
        except Exception as e:
            logger.warning("admission.redis_set_failed err=%s", e)
        return True, 0.0, limit


# ─── Idempotency ──────────────────────────────────────────────


class IdempotencyCache:
    """Tenant-scoped cache mapping idempotency_key → workflow_id.

    TTL defaults to 24h (RFC-suggested for HTTP idempotency keys).
    A failed-result entry uses a shorter TTL (5 min) so a legitimately
    retried client gets a fresh attempt without waiting 24h.
    """

    KEY_PREFIX = "nexus:admission:idem"
    SUCCESS_TTL = 24 * 3600
    FAILURE_TTL = 5 * 60

    def __init__(self, redis_client=None) -> None:
        self._r = redis_client

    @classmethod
    def _key(cls, tenant_id: str, idem_key: str) -> str:
        # Hash boundary: scope by tenant so a leaked key from one
        # tenant can't poison another's namespace.
        return f"{cls.KEY_PREFIX}:{tenant_id}:{idem_key}"

    async def lookup(self, tenant_id: str, idem_key: str) -> Optional[dict[str, Any]]:
        if self._r is None or not idem_key:
            return None
        try:
            raw = await self._r.get(self._key(tenant_id, idem_key))
        except Exception as e:
            logger.warning("admission.idem_get_failed err=%s", e)
            return None
        if not raw:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return None

    async def store(
        self,
        tenant_id: str,
        idem_key: str,
        payload: dict[str, Any],
        success: bool,
    ) -> None:
        if self._r is None or not idem_key:
            return
        ttl = self.SUCCESS_TTL if success else self.FAILURE_TTL
        try:
            await self._r.set(
                self._key(tenant_id, idem_key),
                json.dumps(payload, default=str),
                ex=ttl,
            )
        except Exception as e:
            logger.warning("admission.idem_set_failed err=%s", e)


# ─── Concurrent-workflow gate (architect followup #3) ─────────


class TenantConcurrencyLimiter:
    """Redis-backed gate on concurrent in-flight workflows per tenant.

    Replaces the orchestrator's in-memory dict counter — which was
    per-replica and silently let `replica_count × max_per_tenant`
    workflows through on a 3-replica deployment.

    Uses an atomic Lua check-and-incr so two concurrent uploads
    racing on the same tenant can't both squeak past the limit.
    Falls back to fail-open if Redis is unreachable.
    """

    KEY_PREFIX = "nexus:admission:concurrent"
    KEY_TTL_SECONDS = 3600     # refreshed on every acquire/release;
                                # a stuck counter self-clears within 1h
                                # even if the orchestrator crashes mid-flow.

    # Atomic Lua: GET key (default 0), reject if >= max, else INCR + EXPIRE.
    _ACQUIRE_LUA = """
    local cur = tonumber(redis.call('GET', KEYS[1]) or '0')
    local maxv = tonumber(ARGV[1])
    if cur >= maxv then
      return -1
    end
    local nv = redis.call('INCR', KEYS[1])
    redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]))
    return nv
    """

    # DECR clamps at 0 (Lua does the floor).
    _RELEASE_LUA = """
    local cur = tonumber(redis.call('GET', KEYS[1]) or '0')
    if cur <= 0 then
      return 0
    end
    local nv = redis.call('DECR', KEYS[1])
    if nv <= 0 then
      redis.call('DEL', KEYS[1])
      return 0
    end
    redis.call('EXPIRE', KEYS[1], tonumber(ARGV[1]))
    return nv
    """

    def __init__(self, redis_client=None, max_per_tenant: int = 10) -> None:
        self._r = redis_client
        self._max = int(max_per_tenant)
        self._acquire_sha: Optional[str] = None
        self._release_sha: Optional[str] = None

    @property
    def enabled(self) -> bool:
        return self._max > 0

    @classmethod
    def _key(cls, tenant_id: str) -> str:
        return f"{cls.KEY_PREFIX}:{tenant_id}"

    async def _load_scripts(self) -> None:
        if self._r is None:
            return
        if self._acquire_sha is None:
            try:
                self._acquire_sha = await self._r.script_load(self._ACQUIRE_LUA)
            except Exception as e:
                logger.warning("admission.concurrent.script_load_failed err=%s", e)
        if self._release_sha is None:
            try:
                self._release_sha = await self._r.script_load(self._RELEASE_LUA)
            except Exception as e:
                logger.warning("admission.concurrent.script_load_failed err=%s", e)

    async def acquire(self, tenant_id: str) -> tuple[bool, int]:
        """Try to claim a concurrency slot. Returns (allowed, current_count)."""
        if not self.enabled:
            return True, 0
        if self._r is None:
            # Fail-open: log once at startup, no per-call log spam.
            return True, 0
        await self._load_scripts()
        key = self._key(tenant_id)
        try:
            if self._acquire_sha:
                res = await self._r.evalsha(
                    self._acquire_sha, 1, key, self._max, self.KEY_TTL_SECONDS,
                )
            else:
                res = await self._r.eval(
                    self._ACQUIRE_LUA, 1, key, self._max, self.KEY_TTL_SECONDS,
                )
        except Exception as e:
            logger.warning("admission.concurrent.acquire_failed err=%s", e)
            return True, 0     # fail-open
        n = int(res or 0)
        if n < 0:
            # Rejected — read the current count for the response body.
            try:
                cur = int(await self._r.get(key) or 0)
            except Exception:
                cur = self._max
            return False, cur
        return True, n

    async def release(self, tenant_id: str) -> int:
        """Release a concurrency slot. Returns the new count."""
        if not self.enabled or self._r is None:
            return 0
        await self._load_scripts()
        key = self._key(tenant_id)
        try:
            if self._release_sha:
                res = await self._r.evalsha(
                    self._release_sha, 1, key, self.KEY_TTL_SECONDS,
                )
            else:
                res = await self._r.eval(
                    self._RELEASE_LUA, 1, key, self.KEY_TTL_SECONDS,
                )
        except Exception as e:
            logger.warning("admission.concurrent.release_failed err=%s", e)
            return 0
        return int(res or 0)

    async def current(self, tenant_id: str) -> int:
        """Read current count without modifying. For /admission/status."""
        if self._r is None:
            return 0
        try:
            return int(await self._r.get(self._key(tenant_id)) or 0)
        except Exception:
            return 0


# ─── Queue saturation guard (architect P2) ────────────────────


class QueueSaturationGuard:
    """Reject uploads when the critical-path queues are saturated.

    Prevents the "accept-everything-then-quarantine-everything" failure
    mode the architect P2 review flagged: once eyes.cpu or spine.cpu
    depth crosses the threshold, the orchestrator returns 503 with a
    `Retry-After` header instead of silently piling more work on top
    of a backlog that's already going to miss its deadline.

    Reads XLEN on each protected lane on every check; this is O(1) in
    Redis and ~0.5ms per call. The check runs BEFORE the tenant
    admission gate so a single saturated lane doesn't burn through
    every tenant's concurrency budget.

    Priority-tenant traffic is exempt: the `.priority` lanes are
    separately scaled and don't share workers with the standard pool,
    so a saturated standard queue doesn't block premium uploads.
    """

    DEFAULT_LANES = ("eyes.cpu", "spine.cpu", "eyes.gpu")
    DEFAULT_THRESHOLD = 200

    def __init__(
        self,
        redis_client=None,
        lanes: Optional[tuple[str, ...]] = None,
        threshold: int = DEFAULT_THRESHOLD,
        retry_after_seconds: int = 30,
    ) -> None:
        self._r = redis_client
        self._lanes = tuple(lanes) if lanes else self.DEFAULT_LANES
        self._threshold = int(threshold)
        self._retry_after = int(retry_after_seconds)

    @property
    def enabled(self) -> bool:
        return self._r is not None and self._threshold > 0

    async def check(
        self, *, is_priority: bool = False,
    ) -> tuple[bool, Optional[dict[str, int]], int]:
        """Returns (allowed, depths_by_lane, retry_after_seconds).

        depths_by_lane is non-None only on rejection so the orchestrator
        can include it in the 503 body for operator visibility."""
        if not self.enabled or is_priority:
            return True, None, 0
        depths: dict[str, int] = {}
        try:
            for lane in self._lanes:
                key = f"nexus:queue:{lane}"
                depths[lane] = int(await self._r.xlen(key))
        except Exception as e:
            logger.warning("queue_saturation.read_failed err=%s — fail-open", e)
            return True, None, 0
        saturated = {l: d for l, d in depths.items() if d > self._threshold}
        if saturated:
            return False, saturated, self._retry_after
        return True, None, 0


# ─── FastAPI integration helpers ───────────────────────────────


async def check_admission(
    *,
    tenant_id: str,
    tenant_class: str,
    idempotency_key: Optional[str],
    rate_limiter: TenantRateLimiter,
    idem_cache: IdempotencyCache,
) -> tuple[Optional[dict[str, Any]], Optional[tuple[float, _LimitTier]]]:
    """Single-call admission check used by the workflow create endpoint.

    Returns (cached_response_or_None, throttled_or_None). At most one
    is set:
      - If idempotency key matches a prior request, returns the cached
        response so the caller short-circuits with 200/202.
      - If rate-limited, returns (retry_after_seconds, limit_tier).
      - Otherwise both are None → proceed to create.
    """
    if idempotency_key:
        cached = await idem_cache.lookup(tenant_id, idempotency_key)
        if cached:
            return cached, None
    allowed, retry_after, tier = await rate_limiter.acquire(tenant_id, tenant_class)
    if not allowed:
        return None, (retry_after, tier)
    return None, None
