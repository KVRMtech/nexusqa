"""QE-Central Phase-5.5 — the DISTRIBUTED admission backend (redis).

WHY
===
The in-memory :class:`~app.controlplane.scheduling.admission.AdmissionController`
is process-local: run N replicas of qe-central and each keeps its OWN token
buckets + concurrency counters, so a fleet of N replicas admits at N× the
configured per-host rate — which reads as an ATTACK on the customer's app — and
N daemons could each start a cycle for the same host.  This module lifts the
SAME admission contract onto a shared Redis so all replicas enforce ONE global
limiter.

DROP-IN
=======
:class:`RedisAdmissionBackend` exposes the SAME hot-path interface as the
in-memory controller — ``async try_admit(*, tenant_id, canonical_host, max_rps)``
returning an :class:`AdmissionDecision`, and ``async release(lease)`` — and
returns the VERY SAME :class:`AdmissionLease` / :class:`AdmissionDecision` value
objects and ``REASON_*`` reason codes, so
:func:`app.controlplane.cycle.driver.run_cycle` drives it unchanged.
:func:`get_admission_backend` selects the backend from config: ``memory``
(default) returns the existing in-memory singleton — today's behavior, byte for
byte — and ``redis`` returns this backend.

ATOMICITY
=========
The whole admit decision (per-host mutex → global cap → per-tenant cap → per-host
token bucket, in that exact order) executes inside ONE atomic Redis Lua script,
and grant/release mutate a single shared key space, so two replicas racing the
same host can never both be admitted and can never jointly exceed ``max_rps``.
Global + per-tenant concurrency use sorted sets scored by a lease expiry so a
crashed replica's slot SELF-HEALS after ``QEC_ADMISSION_LEASE_TTL_SECONDS``
(which MUST exceed the max cycle wall-clock so a live cycle is never reaped).

HONESTY — POLITENESS-FIRST, FAIL-CLOSED
=======================================
If Redis is unreachable (or the ``redis`` library / DSN is absent), EVERY admit
is DENIED with :data:`REASON_LIMITER_UNAVAILABLE` — the cycle WAITS rather than
fall open and burst the customer's app.  Denial is the safe direction: a deferred
cycle costs latency, an un-rate-limited crawl costs the customer.  Buckets live
in Redis and a brand-new host's bucket starts EMPTY, so a fresh deploy can never
manufacture a politeness burst either.

The ``redis.asyncio`` import is guarded: when the library is missing the module
still imports and ``memory`` mode is completely unaffected.
"""
from __future__ import annotations

import logging
import os
import time
import uuid
from typing import Optional, Protocol, runtime_checkable

from .admission import (
    ADMISSION,
    REASON_ADMITTED,
    REASON_HOST_UNCONFIGURED,
    REASON_RATE_UNCONFIGURED,
    AdmissionDecision,
    AdmissionLease,
)

logger = logging.getLogger(__name__)

# ── Import-guarded redis.asyncio (absent ⇒ memory mode is unaffected) ───────
try:  # pragma: no cover - exercised by whichever env has/lacks redis
    import redis.asyncio as _aioredis  # type: ignore

    _REDIS_AVAILABLE = True
except Exception:  # noqa: BLE001 - any import failure ⇒ redis simply unavailable
    _aioredis = None  # type: ignore
    _REDIS_AVAILABLE = False


# ── New decision reason (fail-closed store outage) ─────────────────────────
#: Returned when the shared limiter store cannot be reached.  It is deliberately
#: NOT in ``FAIL_CLOSED_REASONS`` (a misconfiguration that needs an operator) nor
#: in ``RETRYABLE_REASONS`` capacity codes: the driver's ``_acquire_admission``
#: treats any non-fail-closed denial as a bounded WAIT, so a store outage defers
#: the cycle and it retries once Redis recovers — never a permanent fail, never a
#: fail-open burst.
REASON_LIMITER_UNAVAILABLE = "limiter_unavailable"


# ── env helpers (mirror admission.build_default_controller knobs) ──────────
def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        return default


# ══════════════════════ the shared admission interface ═════════════════════
@runtime_checkable
class AdmissionBackend(Protocol):
    """The hot-path contract :func:`run_cycle` depends on.

    Both the in-memory :class:`AdmissionController` and
    :class:`RedisAdmissionBackend` satisfy it structurally, so
    :func:`get_admission_backend` can return either as a drop-in.  (``snapshot``
    is intentionally NOT part of this Protocol: the redis backend's snapshot is
    async because the state is external, whereas the in-memory one is sync — and
    no production path calls ``snapshot`` on the hot loop.)
    """

    async def try_admit(
        self, *, tenant_id: str, canonical_host: str, max_rps: float,
    ) -> AdmissionDecision: ...

    async def release(self, lease: Optional[AdmissionLease]) -> None: ...


# ══════════════════════ the atomic Lua scripts ═════════════════════════════
# ADMIT — one atomic decision across ALL replicas.  Checks, in the SAME order as
# the in-memory controller (cheap/reversible first; the token is consumed LAST so
# a denial never wastes politeness budget): host mutex → global cap → per-tenant
# cap → per-host token bucket.  Uses redis server TIME as the single shared clock
# so every replica refills one bucket from the same timeline.
#
# KEYS: 1 global-zset  2 tenant-zset  3 host-mutex  4 host-bucket-hash
#       5 hosts-set     6 tenants-set  7 seq-counter 8 seq-hash
# ARGV: 1 tenant  2 host  3 max_rps  4 burst_factor  5 max_global
#       6 max_per_tenant  7 lease_id  8 lease_ttl_ms
# RETURN: {admitted(0|1), reason, retry_after_ms}
ADMIT_LUA = """
local t = redis.call('TIME')
local now_ms = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)

local tenant = ARGV[1]
local host = ARGV[2]
local max_rps = tonumber(ARGV[3])
local burst = tonumber(ARGV[4])
local max_global = tonumber(ARGV[5])
local max_per_tenant = tonumber(ARGV[6])
local lease_id = ARGV[7]
local ttl_ms = math.floor(tonumber(ARGV[8]))
local ttl_ms_str = ARGV[8]  -- clean integer string for SET ... PX (no float fmt)

-- 1) per-host mutex (one active cycle per host at a time)
if redis.call('GET', KEYS[3]) then
  return {0, 'host_busy', 0}
end

-- 2) global concurrency cap (reap expired leases first — self-healing)
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now_ms)
local global_running = redis.call('ZCARD', KEYS[1])
if max_global > 0 and global_running >= max_global then
  return {0, 'global_cap', 0}
end

-- 3) per-tenant concurrency cap
redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', now_ms)
local tenant_running = redis.call('ZCARD', KEYS[2])
if max_per_tenant > 0 and tenant_running >= max_per_tenant then
  return {0, 'per_tenant_cap', 0}
end

-- 4) per-host token bucket (rate = max_rps, capacity = rate * burst)
redis.call('SADD', KEYS[5], host)
local b = redis.call('HMGET', KEYS[4], 't', 'u', 'r')
local tokens, updated, rate
if b[1] == false then
  tokens = 0.0       -- START EMPTY: a brand-new host can never burst
  updated = now_ms
  rate = max_rps
else
  tokens = tonumber(b[1]); updated = tonumber(b[2]); rate = tonumber(b[3])
  local cap_old = 0.0
  if rate > 0 then cap_old = math.max(1.0, rate * burst) end
  local elapsed = (now_ms - updated) / 1000.0
  if elapsed > 0 and rate > 0 then
    tokens = math.min(cap_old, tokens + elapsed * rate)
  end
  updated = now_ms
  if rate ~= max_rps then
    -- fence changed max_rps: adopt it, clamp to new capacity, NEVER refund
    rate = max_rps
    local cap_new = 0.0
    if rate > 0 then cap_new = math.max(1.0, rate * burst) end
    tokens = math.min(tokens, cap_new)
  end
end

local consumed = false
local retry_ms = 0
if rate > 0 and tokens + 1e-9 >= 1.0 then
  tokens = tokens - 1.0
  consumed = true
elseif rate > 0 then
  retry_ms = math.floor(((1.0 - tokens) / rate) * 1000.0 + 0.5)
end

redis.call('HSET', KEYS[4], 't', tostring(tokens), 'u', tostring(updated), 'r', tostring(rate))

if not consumed then
  return {0, 'rate_limited', retry_ms}
end

-- 5) GRANT — record the lease (expiry-scored so a crash self-heals)
local expire = now_ms + ttl_ms
redis.call('ZADD', KEYS[1], expire, lease_id)
redis.call('ZADD', KEYS[2], expire, lease_id)
redis.call('SET', KEYS[3], lease_id, 'PX', ttl_ms_str)
redis.call('SADD', KEYS[6], tenant)
local seq = redis.call('INCR', KEYS[7])
redis.call('HSET', KEYS[8], tenant, seq)
return {1, '', 0}
"""

# RELEASE — free the global slot, per-tenant slot and (only when this lease owns
# it) the per-host mutex.  Idempotent: a repeated or stale release is a no-op and
# never frees another cycle's mutex.
# KEYS: 1 global-zset  2 tenant-zset  3 host-mutex   ARGV: 1 lease_id
RELEASE_LUA = """
redis.call('ZREM', KEYS[1], ARGV[1])
redis.call('ZREM', KEYS[2], ARGV[1])
if redis.call('GET', KEYS[3]) == ARGV[1] then
  redis.call('DEL', KEYS[3])
end
return 1
"""


# ══════════════════════ the redis backend ══════════════════════════════════
class RedisAdmissionBackend:
    """A shared, atomic, fail-closed admission limiter (drop-in for the
    in-memory :class:`AdmissionController`).

    All state lives in Redis under ``{namespace}:*`` keys, so N replicas share
    ONE limiter.  The client is INJECTED (``redis.asyncio.Redis`` in production,
    a fake in tests) so this class carries no hard dependency on the redis
    library.  A ``None`` client (library/DSN absent) makes the backend
    permanently fail-closed — every admit returns
    :data:`REASON_LIMITER_UNAVAILABLE`.
    """

    def __init__(
        self,
        *,
        client,
        namespace: str = "qec:adm",
        max_global: int = 8,
        max_per_tenant: int = 2,
        burst_factor: float = 2.0,
        lease_ttl_seconds: float = 7200.0,
        unavailable_backoff_seconds: float = 1.0,
    ) -> None:
        self._client = client
        self._ns = str(namespace or "qec:adm").rstrip(":")
        self.max_global = int(max_global)
        self.max_per_tenant = int(max_per_tenant)
        self.burst_factor = max(1.0, float(burst_factor))
        self._lease_ttl_ms = int(max(1.0, float(lease_ttl_seconds)) * 1000)
        self._unavailable_backoff = max(0.0, float(unavailable_backoff_seconds))

    #: A stable name for observability/logging (parity with the memory backend).
    backend_name = "redis"

    # ── key helpers ─────────────────────────────────────────────────────────
    def _k_global(self) -> str:
        return f"{self._ns}:g"

    def _k_tenant(self, tenant: str) -> str:
        return f"{self._ns}:t:{tenant}"

    def _k_mutex(self, host: str) -> str:
        return f"{self._ns}:m:{host}"

    def _k_bucket(self, host: str) -> str:
        return f"{self._ns}:b:{host}"

    def _k_hosts(self) -> str:
        return f"{self._ns}:hosts"

    def _k_tenants(self) -> str:
        return f"{self._ns}:tenants"

    def _k_seq(self) -> str:
        return f"{self._ns}:seq"

    def _k_seq_hash(self) -> str:
        return f"{self._ns}:seq_t"

    def _unavailable(self) -> AdmissionDecision:
        """The fail-closed denial: deny (wait), never fall open and burst."""
        return AdmissionDecision(
            False, REASON_LIMITER_UNAVAILABLE,
            retry_after_seconds=self._unavailable_backoff,
        )

    # ── admission ───────────────────────────────────────────────────────────
    async def try_admit(
        self, *, tenant_id: str, canonical_host: str, max_rps: float,
    ) -> AdmissionDecision:
        """Attempt to admit ONE cycle (non-blocking), atomically across replicas.

        Same fail-closed pre-checks as the in-memory controller (missing host ⇒
        ``host_unconfigured``; non-positive ``max_rps`` ⇒ ``rate_unconfigured``);
        then the atomic Lua decision.  A store outage ⇒
        :data:`REASON_LIMITER_UNAVAILABLE` (deny, never burst).
        """
        tenant = str(tenant_id or "")
        host = str(canonical_host or "").strip().lower()

        if not host:
            return AdmissionDecision(False, REASON_HOST_UNCONFIGURED)
        if max_rps is None or float(max_rps) <= 0:
            return AdmissionDecision(False, REASON_RATE_UNCONFIGURED)
        if self._client is None:
            return self._unavailable()

        lease_id = uuid.uuid4().hex
        keys = [
            self._k_global(), self._k_tenant(tenant), self._k_mutex(host),
            self._k_bucket(host), self._k_hosts(), self._k_tenants(),
            self._k_seq(), self._k_seq_hash(),
        ]
        args = [
            tenant, host, repr(float(max_rps)), repr(self.burst_factor),
            str(self.max_global), str(self.max_per_tenant),
            lease_id, str(self._lease_ttl_ms),
        ]
        try:
            res = await self._client.eval(ADMIT_LUA, len(keys), *keys, *args)
        except Exception as exc:  # noqa: BLE001 - ANY store error ⇒ fail-closed
            logger.warning(
                "qec.admission.redis.admit_unavailable",
                extra={"tenant_id": tenant, "canonical_host": host,
                       "error": str(exc)[:300]},
            )
            return self._unavailable()

        admitted = bool(int(res[0]))
        reason = _as_str(res[1])
        retry_ms = int(res[2] or 0)

        if admitted:
            lease = AdmissionLease(
                lease_id=lease_id, tenant_id=tenant, canonical_host=host,
                max_rps=float(max_rps), acquired_at=time.time(),
            )
            logger.info(
                "qec.admission.redis.admitted",
                extra={"tenant_id": tenant, "canonical_host": host,
                       "lease_id": lease_id, "max_rps": float(max_rps)},
            )
            return AdmissionDecision(True, REASON_ADMITTED, lease=lease)

        return AdmissionDecision(
            False, reason, retry_after_seconds=round(retry_ms / 1000.0, 4),
        )

    async def release(self, lease: Optional[AdmissionLease]) -> None:
        """Free a lease's global/per-tenant slot + per-host mutex (idempotent).

        Best-effort: a release that cannot reach Redis is logged, not raised —
        the safety TTL on the lease/mutex reaps a stranded slot eventually, and
        a stuck mutex only makes that ONE host wait (the safe direction).
        """
        if lease is None:
            return
        if self._client is None:
            logger.warning(
                "qec.admission.redis.release_no_client",
                extra={"lease_id": lease.lease_id},
            )
            return
        keys = [
            self._k_global(), self._k_tenant(lease.tenant_id),
            self._k_mutex(lease.canonical_host),
        ]
        try:
            await self._client.eval(RELEASE_LUA, len(keys), *keys, lease.lease_id)
        except Exception as exc:  # noqa: BLE001 - never raise into the cycle loop
            logger.warning(
                "qec.admission.redis.release_failed",
                extra={"tenant_id": lease.tenant_id,
                       "canonical_host": lease.canonical_host,
                       "lease_id": lease.lease_id, "error": str(exc)[:300]},
            )
            return
        logger.info(
            "qec.admission.redis.released",
            extra={"tenant_id": lease.tenant_id,
                   "canonical_host": lease.canonical_host,
                   "lease_id": lease.lease_id},
        )

    async def snapshot(self) -> dict:
        """Observability snapshot with the SAME shape as the in-memory one.

        Async because the state is external (a divergence from the sync in-memory
        ``snapshot`` — no production hot path calls it).  Best-effort: a store
        outage yields the static caps with zeroed dynamics + an ``error`` note,
        never raises.
        """
        empty = {
            "global_running": 0,
            "per_tenant_running": {},
            "max_global": self.max_global,
            "max_per_tenant": self.max_per_tenant,
            "held_hosts": {},
            "host_buckets": {},
            "last_admit_seq": {},
        }
        if self._client is None:
            return {**empty, "error": "limiter_unavailable"}
        try:
            t = await self._client.time()
            now_ms = int(t[0]) * 1000 + int(t[1]) // 1000

            await self._client.zremrangebyscore(self._k_global(), "-inf", now_ms)
            global_running = int(await self._client.zcard(self._k_global()))

            per_tenant: dict[str, int] = {}
            for raw_tenant in (await self._client.smembers(self._k_tenants()) or set()):
                tenant = _as_str(raw_tenant)
                await self._client.zremrangebyscore(self._k_tenant(tenant), "-inf", now_ms)
                count = int(await self._client.zcard(self._k_tenant(tenant)))
                if count > 0:
                    per_tenant[tenant] = count

            held_hosts: dict[str, str] = {}
            host_buckets: dict[str, dict] = {}
            for raw_host in (await self._client.smembers(self._k_hosts()) or set()):
                host = _as_str(raw_host)
                holder = await self._client.get(self._k_mutex(host))
                mutex_held = holder is not None
                if mutex_held:
                    held_hosts[host] = _as_str(holder)
                bucket = await self._client.hgetall(self._k_bucket(host))
                if bucket:
                    view = _bucket_view(bucket, now_ms, self.burst_factor)
                    view["mutex_held"] = mutex_held
                    host_buckets[host] = view

            last_admit_seq = {
                _as_str(k): int(v)
                for k, v in (await self._client.hgetall(self._k_seq_hash())).items()
            }
            return {
                "global_running": global_running,
                "per_tenant_running": per_tenant,
                "max_global": self.max_global,
                "max_per_tenant": self.max_per_tenant,
                "held_hosts": held_hosts,
                "host_buckets": host_buckets,
                "last_admit_seq": last_admit_seq,
            }
        except Exception as exc:  # noqa: BLE001 - observability must never crash
            logger.warning("qec.admission.redis.snapshot_failed",
                           extra={"error": str(exc)[:300]})
            return {**empty, "error": str(exc)[:300]}


def _as_str(value) -> str:
    """Decode a redis value to str (tolerates both ``decode_responses`` modes)."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _bucket_view(bucket: dict, now_ms: int, burst_factor: float) -> dict:
    """Reconstruct the in-memory ``host_buckets`` entry from a stored bucket hash
    (read-only refill to ``now`` — the ``peek`` semantics, consumes nothing)."""
    b = {_as_str(k): _as_str(v) for k, v in bucket.items()}
    try:
        tokens = float(b.get("t", 0.0))
        updated = float(b.get("u", now_ms))
        rate = float(b.get("r", 0.0))
    except (TypeError, ValueError):
        tokens, updated, rate = 0.0, float(now_ms), 0.0
    capacity = max(1.0, rate * burst_factor) if rate > 0 else 0.0
    elapsed = (now_ms - updated) / 1000.0
    if elapsed > 0 and rate > 0:
        tokens = min(capacity, tokens + elapsed * rate)
    return {
        "tokens": round(tokens, 4),
        "capacity": round(capacity, 4),
        "rate": rate,
        "mutex_held": False,  # overwritten by the caller when the mutex is held
    }


# ══════════════════════ the backend factory ════════════════════════════════
def _build_redis_client(url: str):
    """Build a ``redis.asyncio`` client from ``url`` or return ``None``.

    ``None`` (library absent, empty DSN, or construction error) makes the backend
    fail-closed — the honest, safe posture.  Short socket timeouts mean a Redis
    outage denies FAST (a deferred cycle) instead of hanging the loop.
    """
    if not _REDIS_AVAILABLE:
        logger.error(
            "qec.admission.redis.library_missing",
            extra={"detail": "redis backend requested but the 'redis' library "
                             "is not importable — admission is FAIL-CLOSED"},
        )
        return None
    url = (url or "").strip()
    if not url:
        logger.error(
            "qec.admission.redis.url_missing",
            extra={"detail": "QEC_ADMISSION_BACKEND=redis but QEC_REDIS_URL is "
                             "empty — admission is FAIL-CLOSED"},
        )
        return None
    try:
        return _aioredis.from_url(
            url,
            encoding="utf-8",
            decode_responses=True,
            socket_timeout=2.0,
            socket_connect_timeout=2.0,
            health_check_interval=30,
        )
    except Exception as exc:  # noqa: BLE001 - any construction error ⇒ fail-closed
        logger.error(
            "qec.admission.redis.client_build_failed",
            extra={"error": str(exc)[:300]},
        )
        return None


def get_admission_backend(*, settings_obj=None) -> AdmissionBackend:
    """Return the configured admission backend (drop-in for the in-memory one).

    ``QEC_ADMISSION_BACKEND=memory`` (the default) returns the process-local
    :data:`ADMISSION` singleton — today's behavior, unchanged.  ``redis`` returns
    a :class:`RedisAdmissionBackend` built from ``QEC_REDIS_URL`` (a ``None``
    client when the library/DSN is absent ⇒ the backend is fail-closed).  The
    concurrency caps + burst factor come from the SAME env knobs the in-memory
    controller reads (``QEC_MAX_GLOBAL_CYCLES`` / ``QEC_MAX_PER_TENANT_CYCLES`` /
    ``QEC_HOST_BURST_FACTOR``) so the two backends are tuned identically.
    """
    if settings_obj is None:
        from ...config import settings as settings_obj  # lazy: import-safe

    backend = str(getattr(settings_obj, "qec_admission_backend", "memory") or "memory").strip().lower()
    if backend != "redis":
        return ADMISSION

    client = _build_redis_client(getattr(settings_obj, "qec_redis_url", "") or "")
    return RedisAdmissionBackend(
        client=client,
        namespace=getattr(settings_obj, "qec_admission_redis_namespace", "qec:adm") or "qec:adm",
        max_global=_env_int("QEC_MAX_GLOBAL_CYCLES", 8),
        max_per_tenant=_env_int("QEC_MAX_PER_TENANT_CYCLES", 2),
        burst_factor=_env_float("QEC_HOST_BURST_FACTOR", 2.0),
        lease_ttl_seconds=float(getattr(settings_obj, "qec_admission_lease_ttl_seconds", 7200) or 7200),
        unavailable_backoff_seconds=float(
            getattr(settings_obj, "qec_admission_unavailable_backoff_seconds", 1.0) or 1.0
        ),
    )


__all__ = [
    "AdmissionBackend",
    "RedisAdmissionBackend",
    "get_admission_backend",
    "REASON_LIMITER_UNAVAILABLE",
    "ADMIT_LUA",
    "RELEASE_LUA",
]
