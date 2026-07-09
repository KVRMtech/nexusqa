"""QE-Central Phase-5.5 — distributed (redis) admission-backend tests.

Proves the properties that make the redis backend a SAFE horizontal drop-in for
the in-memory controller, WITHOUT requiring a real redis (an in-process,
dict-backed fake that emulates the two atomic Lua scripts + the snapshot reads):

  * **N replicas share ONE limiter** — two backend instances wired to the SAME
    store share the per-host token bucket (combined admits are capped at the
    burst ceiling, never doubled), the global cap, the per-tenant cap and the
    per-host mutex;
  * **POLITENESS-FIRST fail-closed** — when the store raises (or the client is
    absent) admission is DENIED with ``limiter_unavailable`` (a cycle waits),
    never fail-open into a customer-facing burst;
  * **drop-in shape** — the same ``AdmissionDecision`` / ``AdmissionLease`` value
    objects + reason codes, an idempotent ``release``, and a ``snapshot`` whose
    shape matches the in-memory controller's;
  * **factory** — ``get_admission_backend`` returns the in-memory singleton for
    ``memory`` (default) and a fail-closed redis backend when redis is requested
    but its DSN/library is absent.

A DB-of-record-free, clock-injected fake keeps every assertion exact and fast.
An OPTIONAL integration test (skipif ``QEC_TEST_REDIS_URL`` + redis importable)
exercises the REAL Lua against a live redis.
"""
from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

import pytest

from app.controlplane.scheduling.admission import (
    ADMISSION,
    REASON_ADMITTED,
    REASON_GLOBAL_CAP,
    REASON_HOST_BUSY,
    REASON_HOST_UNCONFIGURED,
    REASON_PER_TENANT_CAP,
    REASON_RATE_LIMITED,
    REASON_RATE_UNCONFIGURED,
    AdmissionDecision,
    AdmissionLease,
)
from app.controlplane.scheduling.distributed import (
    ADMIT_LUA,
    REASON_LIMITER_UNAVAILABLE,
    RELEASE_LUA,
    RedisAdmissionBackend,
    get_admission_backend,
)


def run(coro):
    return asyncio.run(coro)


# ══════════════════════ in-process fake redis ══════════════════════════════
class FakeRedis:
    """A single-store fake emulating the exact effects of the two Lua scripts
    plus the direct reads ``snapshot`` performs.

    Sharing ONE ``FakeRedis`` between two :class:`RedisAdmissionBackend`
    instances models two qe-central replicas talking to one redis, which is what
    the shared-limiter assertions rely on.  ``now_ms`` is a hand-cranked clock
    (redis ``TIME``) so token-bucket refills are deterministic; ``fail=True``
    makes every call raise :class:`ConnectionError` to prove fail-closed.
    """

    def __init__(self, *, now_ms: int = 0) -> None:
        self.now_ms = int(now_ms)
        self.fail = False
        self._kv: dict[str, str] = {}
        self._pexp: dict[str, int] = {}
        self._hash: dict[str, dict[str, str]] = {}
        self._zset: dict[str, dict[str, float]] = {}
        self._set: dict[str, set[str]] = {}

    # ── clock helper ─────────────────────────────────────────────────────────
    def advance(self, seconds: float) -> None:
        self.now_ms += int(round(seconds * 1000))

    async def time(self):
        self._guard()
        return [self.now_ms // 1000, (self.now_ms % 1000) * 1000]

    def _guard(self) -> None:
        if self.fail:
            raise ConnectionError("fake redis: store unreachable")

    def _get_raw(self, key: str):
        exp = self._pexp.get(key)
        if exp is not None and self.now_ms >= exp:
            self._kv.pop(key, None)
            self._pexp.pop(key, None)
        return self._kv.get(key)

    def _zrem_expired(self, key: str, now_ms: int) -> None:
        z = self._zset.get(key)
        if not z:
            return
        for member in [m for m, score in z.items() if score <= now_ms]:
            z.pop(member, None)

    # ── the atomic scripts ───────────────────────────────────────────────────
    async def eval(self, script, numkeys, *rest):
        self._guard()
        keys = list(rest[:numkeys])
        argv = list(rest[numkeys:])
        if script == ADMIT_LUA:
            return self._admit(keys, argv)
        if script == RELEASE_LUA:
            return self._release(keys, argv)
        raise ValueError("fake redis: unknown script")

    def _admit(self, keys, argv):
        now = self.now_ms
        g, tset, mkey, bkey, hosts, tenants, seqk, seqh = keys
        tenant, host = argv[0], argv[1]
        max_rps, burst = float(argv[2]), float(argv[3])
        max_global, max_per_tenant = int(float(argv[4])), int(float(argv[5]))
        lease_id, ttl_ms = argv[6], int(float(argv[7]))

        if self._get_raw(mkey) is not None:
            return [0, "host_busy", 0]

        self._zrem_expired(g, now)
        if max_global > 0 and len(self._zset.get(g, {})) >= max_global:
            return [0, "global_cap", 0]

        self._zrem_expired(tset, now)
        if max_per_tenant > 0 and len(self._zset.get(tset, {})) >= max_per_tenant:
            return [0, "per_tenant_cap", 0]

        self._set.setdefault(hosts, set()).add(host)
        b = self._hash.get(bkey)
        if not b:
            tokens, updated, rate = 0.0, float(now), max_rps
        else:
            tokens, updated, rate = float(b["t"]), float(b["u"]), float(b["r"])
            cap_old = max(1.0, rate * burst) if rate > 0 else 0.0
            elapsed = (now - updated) / 1000.0
            if elapsed > 0 and rate > 0:
                tokens = min(cap_old, tokens + elapsed * rate)
            updated = float(now)
            if rate != max_rps:
                rate = max_rps
                cap_new = max(1.0, rate * burst) if rate > 0 else 0.0
                tokens = min(tokens, cap_new)

        consumed, retry_ms = False, 0
        if rate > 0 and tokens + 1e-9 >= 1.0:
            tokens -= 1.0
            consumed = True
        elif rate > 0:
            retry_ms = int((1.0 - tokens) / rate * 1000.0 + 0.5)

        self._hash[bkey] = {"t": str(tokens), "u": str(updated), "r": str(rate)}
        if not consumed:
            return [0, "rate_limited", retry_ms]

        expire = now + ttl_ms
        self._zset.setdefault(g, {})[lease_id] = expire
        self._zset.setdefault(tset, {})[lease_id] = expire
        self._kv[mkey] = lease_id
        self._pexp[mkey] = now + ttl_ms
        self._set.setdefault(tenants, set()).add(tenant)
        cur = int(self._kv.get(seqk, "0")) + 1
        self._kv[seqk] = str(cur)
        self._hash.setdefault(seqh, {})[tenant] = str(cur)
        return [1, "", 0]

    def _release(self, keys, argv):
        g, tset, mkey = keys
        lease_id = argv[0]
        self._zset.get(g, {}).pop(lease_id, None)
        self._zset.get(tset, {}).pop(lease_id, None)
        if self._get_raw(mkey) == lease_id:
            self._kv.pop(mkey, None)
            self._pexp.pop(mkey, None)
        return 1

    # ── direct reads used by snapshot ────────────────────────────────────────
    async def get(self, key):
        self._guard()
        return self._get_raw(key)

    async def hgetall(self, key):
        self._guard()
        return dict(self._hash.get(key, {}))

    async def zcard(self, key):
        self._guard()
        return len(self._zset.get(key, {}))

    async def zremrangebyscore(self, key, mn, mx):
        self._guard()
        self._zrem_expired(key, int(mx))
        return 0

    async def smembers(self, key):
        self._guard()
        return set(self._set.get(key, set()))


def _backend(client, **kw) -> RedisAdmissionBackend:
    """A backend with dev caps unless overridden (host politeness always on)."""
    kw.setdefault("max_global", 8)
    kw.setdefault("max_per_tenant", 2)
    kw.setdefault("burst_factor", 2.0)
    kw.setdefault("namespace", "qec:adm")
    return RedisAdmissionBackend(client=client, **kw)


async def _warm(backend: RedisAdmissionBackend, fake: FakeRedis, host: str,
                *, tenant: str = "warm", rps: float = 1000.0, fill: float = 1.0) -> None:
    """Create ``host``'s shared bucket (starts EMPTY ⇒ denied) then refill it."""
    d = await backend.try_admit(tenant_id=tenant, canonical_host=host, max_rps=rps)
    assert d.admitted is False
    assert d.reason == REASON_RATE_LIMITED
    fake.advance(fill)


# ══════════════════════ shared token bucket (rate) ═════════════════════════
class TestSharedRateLimiter:
    def test_two_replicas_share_one_bucket(self):
        async def body():
            fake = FakeRedis()
            r1, r2 = _backend(fake, max_per_tenant=8), _backend(fake, max_per_tenant=8)
            # Replica 1 creates the bucket (empty ⇒ denied); after 0.5s @2rps = 1 token.
            d0 = await r1.try_admit(tenant_id="A", canonical_host="h.x", max_rps=2)
            assert d0.admitted is False and d0.reason == REASON_RATE_LIMITED
            fake.advance(0.5)
            # Replica 1 consumes the ONE token, then frees the host mutex.
            d1 = await r1.try_admit(tenant_id="A", canonical_host="h.x", max_rps=2)
            assert d1.admitted is True
            await r1.release(d1.lease)
            # Replica 2 (different process, SAME store) now sees an EMPTY bucket.
            d2 = await r2.try_admit(tenant_id="B", canonical_host="h.x", max_rps=2)
            assert d2.admitted is False
            assert d2.reason == REASON_RATE_LIMITED

        run(body())

    def test_combined_rate_capped_at_burst_ceiling_across_replicas(self):
        async def body():
            fake = FakeRedis()
            r1, r2 = _backend(fake, max_per_tenant=8), _backend(fake, max_per_tenant=8)
            await _warm(r1, fake, "h.x", rps=2, fill=0.0)  # create bucket, cap = 2*2 = 4
            fake.advance(100.0)  # idle long — tokens CAP at 4, never accumulate to 200

            admitted = 0
            for i in range(8):
                backend = r1 if i % 2 == 0 else r2  # alternate 'replicas'
                d = await backend.try_admit(tenant_id="A", canonical_host="h.x", max_rps=2)
                if d.admitted:
                    admitted += 1
                    await backend.release(d.lease)  # free the mutex, keep depleting
                else:
                    assert d.reason == REASON_RATE_LIMITED
                    break
            # Two replicas CANNOT double the ceiling — exactly the shared burst of 4.
            assert admitted == 4

        run(body())


# ══════════════════════ shared concurrency caps + mutex ════════════════════
class TestSharedConcurrency:
    def test_global_cap_shared_across_replicas(self):
        async def body():
            fake = FakeRedis()
            r1 = _backend(fake, max_global=1, max_per_tenant=5)
            r2 = _backend(fake, max_global=1, max_per_tenant=5)
            await _warm(r1, fake, "a.x")
            await _warm(r1, fake, "b.x")

            d1 = await r1.try_admit(tenant_id="A", canonical_host="a.x", max_rps=1000)
            assert d1.admitted is True
            # Different replica, different tenant + host — still blocked by the ONE
            # shared global slot.
            d2 = await r2.try_admit(tenant_id="B", canonical_host="b.x", max_rps=1000)
            assert d2.admitted is False
            assert d2.reason == REASON_GLOBAL_CAP

        run(body())

    def test_per_tenant_cap_shared_across_replicas(self):
        async def body():
            fake = FakeRedis()
            r1 = _backend(fake, max_global=10, max_per_tenant=1)
            r2 = _backend(fake, max_global=10, max_per_tenant=1)
            await _warm(r1, fake, "a.x")
            await _warm(r1, fake, "b.x")

            d1 = await r1.try_admit(tenant_id="A", canonical_host="a.x", max_rps=1000)
            assert d1.admitted is True
            d2 = await r2.try_admit(tenant_id="A", canonical_host="b.x", max_rps=1000)
            assert d2.admitted is False
            assert d2.reason == REASON_PER_TENANT_CAP

        run(body())

    def test_host_mutex_shared_across_replicas(self):
        async def body():
            fake = FakeRedis()
            r1 = _backend(fake, max_global=10, max_per_tenant=10)
            r2 = _backend(fake, max_global=10, max_per_tenant=10)
            await _warm(r1, fake, "shared.x")

            d1 = await r1.try_admit(tenant_id="A", canonical_host="shared.x", max_rps=1000)
            assert d1.admitted is True
            # A different replica targeting the SAME host is blocked by the mutex.
            d2 = await r2.try_admit(tenant_id="B", canonical_host="shared.x", max_rps=1000)
            assert d2.admitted is False
            assert d2.reason == REASON_HOST_BUSY
            # Releasing frees the mutex for the other replica (tokens remain).
            await r1.release(d1.lease)
            d3 = await r2.try_admit(tenant_id="B", canonical_host="shared.x", max_rps=1000)
            assert d3.admitted is True

        run(body())


# ══════════════════════ fail-closed (politeness first) ═════════════════════
class TestFailClosed:
    def test_store_unreachable_denies_admission(self):
        async def body():
            fake = FakeRedis()
            backend = _backend(fake)
            await _warm(backend, fake, "h.x")  # bucket is ready…
            fake.fail = True                    # …but the store goes away.
            d = await backend.try_admit(tenant_id="A", canonical_host="h.x", max_rps=1000)
            assert d.admitted is False
            assert d.reason == REASON_LIMITER_UNAVAILABLE
            assert d.reason not in {REASON_ADMITTED}
            # A store outage must never permanently FAIL the cycle (not fail-closed
            # misconfig) — it defers, so it retries once redis recovers.
            from app.controlplane.scheduling.admission import FAIL_CLOSED_REASONS
            assert d.reason not in FAIL_CLOSED_REASONS

        run(body())

    def test_none_client_is_fail_closed(self):
        async def body():
            backend = _backend(None)
            d = await backend.try_admit(tenant_id="A", canonical_host="h.x", max_rps=1000)
            assert d.admitted is False
            assert d.reason == REASON_LIMITER_UNAVAILABLE

        run(body())

    def test_missing_host_and_rate_precheck_fail_closed(self):
        async def body():
            backend = _backend(FakeRedis())
            d_host = await backend.try_admit(tenant_id="A", canonical_host="", max_rps=5)
            assert d_host.admitted is False and d_host.reason == REASON_HOST_UNCONFIGURED
            d_rate = await backend.try_admit(tenant_id="A", canonical_host="h.x", max_rps=0)
            assert d_rate.admitted is False and d_rate.reason == REASON_RATE_UNCONFIGURED

        run(body())

    def test_release_never_raises_when_store_unreachable(self):
        async def body():
            fake = FakeRedis()
            backend = _backend(fake)
            await _warm(backend, fake, "h.x")
            d = await backend.try_admit(tenant_id="A", canonical_host="h.x", max_rps=1000)
            assert d.admitted is True
            fake.fail = True
            await backend.release(d.lease)  # best-effort — must NOT raise
            await backend.release(None)     # None lease is a no-op

        run(body())


# ══════════════════════ drop-in shape + release safety ═════════════════════
class TestDropInShape:
    def test_admit_returns_shared_value_objects(self):
        async def body():
            fake = FakeRedis()
            backend = _backend(fake)
            await _warm(backend, fake, "h.x")
            d = await backend.try_admit(tenant_id="A", canonical_host="h.x", max_rps=1000)
            assert isinstance(d, AdmissionDecision)
            assert d.reason == REASON_ADMITTED
            assert isinstance(d.lease, AdmissionLease)
            assert d.lease.tenant_id == "A"
            assert d.lease.canonical_host == "h.x"
            assert d.lease.max_rps == 1000.0
            assert d.lease.lease_id  # opaque, non-empty

        run(body())

    def test_release_is_idempotent_and_frees_slot(self):
        async def body():
            fake = FakeRedis()
            backend = _backend(fake, max_global=4, max_per_tenant=2)
            await _warm(backend, fake, "h.x")
            d = await backend.try_admit(tenant_id="A", canonical_host="h.x", max_rps=1000)
            assert d.admitted is True

            await backend.release(d.lease)   # real release
            await backend.release(d.lease)   # double release must not underflow

            snap = await backend.snapshot()
            assert snap["global_running"] == 0
            assert snap["per_tenant_running"] == {}
            assert snap["held_hosts"] == {}

        run(body())

    def test_snapshot_shape_matches_in_memory(self):
        async def body():
            fake = FakeRedis()
            backend = _backend(fake, max_global=4, max_per_tenant=2)
            await _warm(backend, fake, "h.x")
            d = await backend.try_admit(tenant_id="A", canonical_host="h.x", max_rps=1000)
            assert d.admitted is True

            snap = await backend.snapshot()
            # SAME keys the in-memory AdmissionController.snapshot() emits.
            assert set(snap.keys()) == {
                "global_running", "per_tenant_running", "max_global",
                "max_per_tenant", "held_hosts", "host_buckets", "last_admit_seq",
            }
            assert snap["max_global"] == 4
            assert snap["max_per_tenant"] == 2
            assert snap["global_running"] == 1
            assert snap["per_tenant_running"]["A"] == 1
            assert "h.x" in snap["held_hosts"]
            bucket = snap["host_buckets"]["h.x"]
            assert bucket["mutex_held"] is True
            assert bucket["rate"] == 1000.0
            assert bucket["capacity"] == 2000.0
            assert snap["last_admit_seq"]["A"] >= 1

        run(body())

    def test_snapshot_degraded_when_store_unreachable(self):
        async def body():
            fake = FakeRedis()
            backend = _backend(fake)
            fake.fail = True
            snap = await backend.snapshot()
            # Static caps survive; dynamics are zeroed and an error is surfaced.
            assert snap["max_global"] == 8
            assert snap["global_running"] == 0
            assert "error" in snap

        run(body())


# ══════════════════════ the backend factory ════════════════════════════════
class TestFactory:
    def test_memory_backend_is_the_in_memory_singleton(self):
        cfg = SimpleNamespace(qec_admission_backend="memory")
        assert get_admission_backend(settings_obj=cfg) is ADMISSION

    def test_default_unset_is_memory(self):
        cfg = SimpleNamespace()  # no attribute ⇒ defaults to 'memory'
        assert get_admission_backend(settings_obj=cfg) is ADMISSION

    def test_redis_requested_without_dsn_is_fail_closed(self):
        cfg = SimpleNamespace(
            qec_admission_backend="redis",
            qec_redis_url="",  # no DSN ⇒ client None ⇒ fail-closed
            qec_admission_redis_namespace="qec:adm",
            qec_admission_lease_ttl_seconds=7200,
            qec_admission_unavailable_backoff_seconds=1.0,
        )
        backend = get_admission_backend(settings_obj=cfg)
        assert isinstance(backend, RedisAdmissionBackend)

        async def body():
            d = await backend.try_admit(tenant_id="A", canonical_host="h.x", max_rps=5)
            assert d.admitted is False
            assert d.reason == REASON_LIMITER_UNAVAILABLE

        run(body())


# ══════════════════════ OPTIONAL live-redis integration (real Lua) ═════════
_TEST_REDIS_URL = os.getenv("QEC_TEST_REDIS_URL")
try:  # pragma: no cover - environment-dependent
    import redis.asyncio as _real_aioredis  # noqa: F401

    _REDIS_LIB = True
except Exception:  # noqa: BLE001
    _REDIS_LIB = False


@pytest.mark.skipif(
    not (_TEST_REDIS_URL and _REDIS_LIB),
    reason="requires QEC_TEST_REDIS_URL and the redis library (real Lua path)",
)
def test_real_redis_shared_mutex_and_fail_closed():  # pragma: no cover - live only
    import uuid

    import redis.asyncio as aioredis

    async def body():
        ns = f"qec:test:{uuid.uuid4().hex}"
        client = aioredis.from_url(_TEST_REDIS_URL, decode_responses=True)
        r1 = RedisAdmissionBackend(client=client, namespace=ns, max_global=10, max_per_tenant=10)
        r2 = RedisAdmissionBackend(client=client, namespace=ns, max_global=10, max_per_tenant=10)
        try:
            # Poll until a token exists on the shared real bucket (fresh = empty).
            admitted = None
            for _ in range(200):
                d = await r1.try_admit(tenant_id="A", canonical_host="live.x", max_rps=1000)
                if d.admitted:
                    admitted = d
                    break
                await asyncio.sleep(0.01)
            assert admitted is not None and admitted.admitted is True
            # Second replica, same host → real-Lua mutex denial.
            busy = await r2.try_admit(tenant_id="B", canonical_host="live.x", max_rps=1000)
            assert busy.admitted is False and busy.reason == REASON_HOST_BUSY
            await r1.release(admitted.lease)
            snap = await r1.snapshot()
            assert set(snap.keys()) >= {"global_running", "host_buckets"}
        finally:
            await client.aclose()

    run(body())
