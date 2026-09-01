"""
Pure unit tests for the admission layer.

Covers:
  - TenantRateLimiter fail-open when Redis is None
  - Token-bucket math: burst capacity, refill rate, retry-after
  - IdempotencyCache fail-open + TTL routing
  - check_admission flow (cached / throttled / clear paths)
  - WorkflowMetrics safety when no metrics provider is wired

No Postgres + no real Redis required. A fake in-memory async dict
stands in for redis-py so the rate-limiter math is exercised
end-to-end.
"""

from __future__ import annotations

import time

import pytest

from nexus_sdk.workflows.admission import (
    IdempotencyCache,
    TenantRateLimiter,
    check_admission,
    _tier_from_env,
)
from nexus_sdk.workflows.metrics import (
    WorkflowMetrics,
    classify_tenant,
)


# ─── Fake Redis ────────────────────────────────────────────────


class _FakeRedis:
    """Minimal async-Redis stand-in. Supports the methods the admission
    layer actually calls — hgetall/hset/expire/get/set."""

    def __init__(self) -> None:
        self._hash: dict[str, dict[str, str]] = {}
        self._strings: dict[str, tuple[str, float]] = {}
        self.fail_next: int = 0  # set >0 to make the next N ops raise

    def _maybe_fail(self) -> None:
        if self.fail_next > 0:
            self.fail_next -= 1
            raise RuntimeError("simulated redis failure")

    async def hgetall(self, key):
        self._maybe_fail()
        return dict(self._hash.get(key, {}))

    async def hset(self, key, mapping):
        self._maybe_fail()
        if key not in self._hash:
            self._hash[key] = {}
        # redis-py stores numbers as strings — keep parity.
        for k, v in mapping.items():
            self._hash[key][k] = str(v)

    async def expire(self, key, seconds):
        self._maybe_fail()
        # no-op for the fake — TTL not enforced

    async def get(self, key):
        self._maybe_fail()
        entry = self._strings.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if expires_at > 0 and time.time() > expires_at:
            self._strings.pop(key, None)
            return None
        return value

    async def set(self, key, value, ex=None):
        self._maybe_fail()
        expires_at = time.time() + ex if ex else 0
        self._strings[key] = (value, expires_at)


# ─── Rate limiter ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rate_limiter_fail_open_without_redis():
    rl = TenantRateLimiter(redis_client=None)
    for _ in range(10):
        allowed, retry, _tier = await rl.acquire("acme", "unknown")
        assert allowed
        assert retry == 0.0


@pytest.mark.asyncio
async def test_rate_limiter_allows_within_burst():
    rl = TenantRateLimiter(_FakeRedis())
    tier = _tier_from_env("unknown")  # burst=10, rate=30/min
    for i in range(tier.burst):
        allowed, retry, _ = await rl.acquire("acme", "unknown")
        assert allowed, f"request {i + 1}/{tier.burst} unexpectedly denied"
        assert retry == 0.0


@pytest.mark.asyncio
async def test_rate_limiter_throttles_beyond_burst():
    rl = TenantRateLimiter(_FakeRedis())
    tier = _tier_from_env("unknown")
    # Drain the bucket.
    for _ in range(tier.burst):
        allowed, _, _ = await rl.acquire("acme", "unknown")
        assert allowed
    # Next request should be throttled.
    allowed, retry, _ = await rl.acquire("acme", "unknown")
    assert not allowed
    # Retry-after should be > 0 and < 60 sec for this tier.
    assert 0.0 < retry < 60.0


@pytest.mark.asyncio
async def test_rate_limiter_fail_opens_when_redis_throws():
    fake = _FakeRedis()
    rl = TenantRateLimiter(fake)
    fake.fail_next = 10
    # Even with Redis throwing, the limiter should allow.
    allowed, retry, _ = await rl.acquire("acme", "unknown")
    assert allowed
    assert retry == 0.0


@pytest.mark.asyncio
async def test_rate_limiter_distinct_tenants_have_separate_buckets():
    rl = TenantRateLimiter(_FakeRedis())
    tier = _tier_from_env("unknown")
    # Drain acme.
    for _ in range(tier.burst):
        allowed, _, _ = await rl.acquire("acme", "unknown")
        assert allowed
    # globex starts fresh.
    allowed, retry, _ = await rl.acquire("globex", "unknown")
    assert allowed
    assert retry == 0.0


# ─── Idempotency cache ────────────────────────────────────────


@pytest.mark.asyncio
async def test_idempotency_fail_open_without_redis():
    ic = IdempotencyCache(redis_client=None)
    assert await ic.lookup("acme", "abc") is None
    await ic.store("acme", "abc", {"workflow_id": "wf-1"}, success=True)
    # Still None on fail-open.
    assert await ic.lookup("acme", "abc") is None


@pytest.mark.asyncio
async def test_idempotency_round_trip():
    ic = IdempotencyCache(_FakeRedis())
    await ic.store("acme", "abc", {"workflow_id": "wf-1", "status": "running"}, success=True)
    cached = await ic.lookup("acme", "abc")
    assert cached == {"workflow_id": "wf-1", "status": "running"}


@pytest.mark.asyncio
async def test_idempotency_scopes_by_tenant():
    ic = IdempotencyCache(_FakeRedis())
    await ic.store("acme", "abc", {"workflow_id": "wf-acme"}, success=True)
    assert await ic.lookup("globex", "abc") is None
    assert await ic.lookup("acme", "abc") == {"workflow_id": "wf-acme"}


# ─── check_admission ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_check_admission_cached_short_circuit():
    fake = _FakeRedis()
    rl = TenantRateLimiter(fake)
    ic = IdempotencyCache(fake)
    await ic.store("acme", "abc", {"workflow_id": "wf-1"}, success=True)
    cached, throttled = await check_admission(
        tenant_id="acme", tenant_class="unknown",
        idempotency_key="abc",
        rate_limiter=rl, idem_cache=ic,
    )
    assert cached == {"workflow_id": "wf-1"}
    assert throttled is None


@pytest.mark.asyncio
async def test_check_admission_clear_path():
    fake = _FakeRedis()
    rl = TenantRateLimiter(fake)
    ic = IdempotencyCache(fake)
    cached, throttled = await check_admission(
        tenant_id="acme", tenant_class="unknown",
        idempotency_key=None,
        rate_limiter=rl, idem_cache=ic,
    )
    assert cached is None
    assert throttled is None


@pytest.mark.asyncio
async def test_check_admission_throttled():
    fake = _FakeRedis()
    rl = TenantRateLimiter(fake)
    ic = IdempotencyCache(fake)
    tier = _tier_from_env("unknown")
    # Drain the bucket via the rate-limiter directly.
    for _ in range(tier.burst):
        await rl.acquire("acme", "unknown")
    cached, throttled = await check_admission(
        tenant_id="acme", tenant_class="unknown",
        idempotency_key=None,
        rate_limiter=rl, idem_cache=ic,
    )
    assert cached is None
    assert throttled is not None
    retry_after, returned_tier = throttled
    assert retry_after > 0
    assert returned_tier.burst == tier.burst


# ─── Metrics safety ───────────────────────────────────────────


def test_workflow_metrics_no_op_without_provider():
    wfm = WorkflowMetrics(metrics_provider=None)
    # Every record_* call must be a no-op (no exception) when there's
    # no provider — the engine code paths can't afford to crash on
    # metrics emission.
    wfm.record_created("audio.canonicalize", "acme")
    wfm.record_terminal("audio.canonicalize", "success", duration_seconds=12.5)
    wfm.record_step("eyes", "eyes.extract_frames", "completed", duration_ms=300)
    wfm.record_step("eyes", "eyes.extract_frames", "retry", duration_ms=300, attempts=2)
    wfm.record_orphan_recovered(3)
    wfm.record_deadline_quarantined("video.canonicalize", "running")
    wfm.set_in_flight("audio.canonicalize", 42)
    wfm.set_dlq_depth("eyes.gpu", 0)


def test_tenant_classification():
    assert classify_tenant("") == "unknown"
    assert classify_tenant("never-registered") == "unknown"
