"""
Contract test for TenantConcurrencyLimiter (architect followup #3).

Pins the invariants the multi-replica admission gate must hold:

  - With max_per_tenant=N, the (N+1)th acquire returns False.
  - acquire() is atomic — a race between two callers can't put two
    workflows over the limit (validated against a fake Redis that
    serialises eval calls).
  - release() decrements; below-zero releases are clamped.
  - Fail-open when redis_client is None.

Uses a deterministic in-memory Lua shim so the test runs in CI
without Redis. The shim's API matches the subset of redis-py the
limiter uses (`script_load`, `evalsha`, `eval`, `get`).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "sdk" / "nexus-sdk"))

from nexus_sdk.workflows.admission import TenantConcurrencyLimiter  # noqa: E402


# ─── Fake Redis ──────────────────────────────────────────────


class _FakeRedis:
    """In-memory Redis stub that runs the Lua scripts via Python.

    Only implements what TenantConcurrencyLimiter touches; intentionally
    rejects anything else so tests fail loudly if the limiter adds new
    commands without updating this stub."""

    def __init__(self) -> None:
        self._data: dict[str, str] = {}
        self._scripts: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def script_load(self, script: str) -> str:
        # Deterministic sha-like ID — only needs to be unique per script.
        sid = f"sha:{len(self._scripts)}"
        self._scripts[sid] = script
        return sid

    async def evalsha(self, sid: str, n_keys: int, *args):
        script = self._scripts[sid]
        return await self._run(script, n_keys, *args)

    async def eval(self, script: str, n_keys: int, *args):
        return await self._run(script, n_keys, *args)

    async def _run(self, script: str, n_keys: int, *args):
        keys = list(args[:n_keys])
        argv = list(args[n_keys:])
        # Serialise so the test mirrors Redis single-threaded eval.
        async with self._lock:
            if "redis.call('INCR'" in script and "ARGV[1]" in script:
                # ACQUIRE script
                key = keys[0]
                maxv = int(argv[0])
                cur = int(self._data.get(key, "0"))
                if cur >= maxv:
                    return -1
                nv = cur + 1
                self._data[key] = str(nv)
                return nv
            if "redis.call('DECR'" in script:
                # RELEASE script
                key = keys[0]
                cur = int(self._data.get(key, "0"))
                if cur <= 0:
                    return 0
                nv = cur - 1
                if nv <= 0:
                    self._data.pop(key, None)
                    return 0
                self._data[key] = str(nv)
                return nv
            raise AssertionError(f"unknown script: {script[:60]}...")

    async def get(self, key: str):
        return self._data.get(key)


# ─── Tests ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_acquire_blocks_after_max_reached():
    r = _FakeRedis()
    lim = TenantConcurrencyLimiter(redis_client=r, max_per_tenant=3)
    granted = []
    for _ in range(5):
        allowed, n = await lim.acquire("t1")
        granted.append((allowed, n))
    # First 3 allowed, last 2 rejected.
    assert [g[0] for g in granted] == [True, True, True, False, False]
    assert granted[0][1] == 1
    assert granted[2][1] == 3
    # Rejected calls report current count == max.
    assert granted[3][1] == 3


@pytest.mark.asyncio
async def test_release_decrements_and_clamps_at_zero():
    r = _FakeRedis()
    lim = TenantConcurrencyLimiter(redis_client=r, max_per_tenant=5)
    await lim.acquire("t1")
    await lim.acquire("t1")
    assert (await lim.current("t1")) == 2
    await lim.release("t1")
    assert (await lim.current("t1")) == 1
    await lim.release("t1")
    assert (await lim.current("t1")) == 0
    # Extra release doesn't go negative.
    await lim.release("t1")
    assert (await lim.current("t1")) == 0


@pytest.mark.asyncio
async def test_concurrent_acquires_respect_limit():
    """40 concurrent acquires against max=10 must result in exactly 10
    `True` outcomes — never more."""
    r = _FakeRedis()
    lim = TenantConcurrencyLimiter(redis_client=r, max_per_tenant=10)
    results = await asyncio.gather(*(lim.acquire("t1") for _ in range(40)))
    granted = [allowed for allowed, _ in results]
    assert granted.count(True) == 10
    assert granted.count(False) == 30


@pytest.mark.asyncio
async def test_fail_open_when_redis_missing():
    """No Redis client → always allow. The architect explicitly preferred
    fail-open over fail-closed for the admission layer (rejecting every
    upload during a Redis blip would be worse than a temporary over-shoot)."""
    lim = TenantConcurrencyLimiter(redis_client=None, max_per_tenant=1)
    a1, _ = await lim.acquire("t1")
    a2, _ = await lim.acquire("t1")
    assert a1 is True and a2 is True
    # Release is a no-op when Redis is None.
    assert (await lim.release("t1")) == 0


@pytest.mark.asyncio
async def test_disabled_when_max_zero():
    """max_per_tenant=0 disables the gate entirely (legacy compat)."""
    r = _FakeRedis()
    lim = TenantConcurrencyLimiter(redis_client=r, max_per_tenant=0)
    assert lim.enabled is False
    for _ in range(100):
        allowed, _ = await lim.acquire("t1")
        assert allowed is True


@pytest.mark.asyncio
async def test_tenants_have_separate_buckets():
    r = _FakeRedis()
    lim = TenantConcurrencyLimiter(redis_client=r, max_per_tenant=2)
    # t1 saturates.
    assert (await lim.acquire("t1"))[0] is True
    assert (await lim.acquire("t1"))[0] is True
    assert (await lim.acquire("t1"))[0] is False
    # t2 unaffected.
    assert (await lim.acquire("t2"))[0] is True
    assert (await lim.acquire("t2"))[0] is True
    assert (await lim.acquire("t2"))[0] is False


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
