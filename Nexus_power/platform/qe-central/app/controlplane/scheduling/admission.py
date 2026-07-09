"""QE-Central S5 — the admission controller (design §3.5).

A VENDORED, HealScheduler-shaped admission gate (platform.api.app is not
importable across containers — SDK-lifting it is a future seam) fused with the
per-customer-host politeness controls the control plane needs so a FLEET of
cycles never:

  * **starves a tenant** — a global concurrency cap plus a per-tenant cap means
    no single tenant can occupy all global slots (the per-tenant cap is the
    non-blocking equivalent of HealScheduler's round-robin release: with
    ``max_per_tenant < max_global`` at least ``max_global - max_per_tenant``
    slots are ALWAYS reachable by other tenants, so nobody is starved); and
  * **reads as an attack on a CUSTOMER's app** — a per-``canonical_host`` token
    bucket (rate = ``fences.max_rps``, burst 2×) caps how FREQUENTLY cycles may
    start against one host, and a per-host mutex enforces one active cycle per
    host at a time.

EVERYTHING is IN-MEMORY and the token buckets start EMPTY: a process restart can
never manufacture a politeness burst (the first cycle after (re)start must wait
one refill interval).  ``try_admit`` is NON-BLOCKING — it returns an honest
decision so the cycle driver can DEFER and retry on the next tick rather than
park a coroutine holding no real work.

Politeness is FAIL-CLOSED: a cycle with no positive ``max_rps`` or no
``canonical_host`` is refused (``rate_unconfigured`` / ``host_unconfigured``) —
we never crawl a customer host we cannot rate-key.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger(__name__)


# ── Decision reasons ───────────────────────────────────────────────────────
REASON_ADMITTED = ""
REASON_GLOBAL_CAP = "global_cap"
REASON_PER_TENANT_CAP = "per_tenant_cap"
REASON_HOST_BUSY = "host_busy"
REASON_RATE_LIMITED = "rate_limited"
REASON_RATE_UNCONFIGURED = "rate_unconfigured"
REASON_HOST_UNCONFIGURED = "host_unconfigured"

#: Reasons a caller MAY retry later (transient — capacity/tokens will free up).
RETRYABLE_REASONS = frozenset({
    REASON_GLOBAL_CAP, REASON_PER_TENANT_CAP, REASON_HOST_BUSY, REASON_RATE_LIMITED,
})
#: Reasons that will NOT clear without operator action (misconfiguration).
FAIL_CLOSED_REASONS = frozenset({REASON_RATE_UNCONFIGURED, REASON_HOST_UNCONFIGURED})


# ── Per-host token bucket (politeness rate limiter) ────────────────────────
class TokenBucket:
    """A monotonic-clock token bucket, starting EMPTY (no restart burst).

    Capacity (max burst) = ``rate * burst_factor``.  Tokens refill continuously
    at ``rate`` tokens/second, clamped to capacity, so accumulated idle time can
    never let more than ``capacity`` cycles fire back-to-back.  A fresh bucket
    holds ZERO tokens, so the first consume must wait ``1 / rate`` seconds — the
    deliberate anti-burst posture.
    """

    def __init__(
        self,
        rate: float,
        *,
        burst_factor: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.rate = float(rate)
        self.burst_factor = max(1.0, float(burst_factor))
        self.capacity = max(1.0, self.rate * self.burst_factor) if self.rate > 0 else 0.0
        self._clock = clock
        self.tokens = 0.0  # START EMPTY — the anti-burst invariant.
        self._updated = clock()

    def reconfigure(self, rate: float, *, burst_factor: float) -> None:
        """Adopt a new ``rate``/``burst_factor`` (fence change) without a refund.

        Tokens are clamped to the new capacity; we NEVER top the bucket up on a
        reconfigure, so lowering the rate cannot hand out a burst.
        """
        self._refill()
        self.rate = float(rate)
        self.burst_factor = max(1.0, float(burst_factor))
        self.capacity = max(1.0, self.rate * self.burst_factor) if self.rate > 0 else 0.0
        self.tokens = min(self.tokens, self.capacity)

    def _refill(self) -> None:
        now = self._clock()
        elapsed = now - self._updated
        if elapsed > 0 and self.rate > 0:
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        # Advance the clock even when rate<=0 so a later reconfigure is honest.
        self._updated = now

    def try_consume(self, amount: float = 1.0) -> bool:
        """Consume ``amount`` tokens if available; return whether it succeeded."""
        if self.rate <= 0:
            return False
        self._refill()
        if self.tokens + 1e-9 >= amount:
            self.tokens -= amount
            return True
        return False

    def retry_after(self, amount: float = 1.0) -> float:
        """Seconds until ``amount`` tokens would be available (0 when ready)."""
        if self.rate <= 0:
            return 0.0
        self._refill()
        deficit = amount - self.tokens
        return max(0.0, deficit / self.rate) if deficit > 0 else 0.0

    def peek(self) -> float:
        """Current token count after refill (observability; consumes nothing)."""
        self._refill()
        return round(self.tokens, 4)


# ── Decision + lease value objects ─────────────────────────────────────────
@dataclass(frozen=True)
class AdmissionLease:
    """An opaque grant returned by :meth:`AdmissionController.try_admit`.

    It MUST be handed back to :meth:`AdmissionController.release` (typically in a
    ``finally``) to free the concurrency slot and the per-host mutex.  A lost
    lease strands a slot/mutex until process restart — deliberately visible in
    :meth:`AdmissionController.snapshot` so a leak is diagnosable.
    """

    lease_id: str
    tenant_id: str
    canonical_host: str
    max_rps: float
    acquired_at: float


@dataclass(frozen=True)
class AdmissionDecision:
    """The (non-blocking) outcome of an admission attempt.

    ``admitted`` ⇒ ``lease`` is set and ``reason == ""``.  Otherwise ``lease`` is
    None, ``reason`` names WHY (one of the ``REASON_*`` constants), and
    ``retry_after_seconds`` is a hint for rate-limited denials.
    """

    admitted: bool
    reason: str = REASON_ADMITTED
    lease: Optional[AdmissionLease] = None
    retry_after_seconds: float = 0.0

    @property
    def retryable(self) -> bool:
        """True when the denial is transient (capacity/tokens, not misconfig)."""
        return self.reason in RETRYABLE_REASONS


@dataclass
class _HostState:
    bucket: TokenBucket
    lease_id: Optional[str] = None  # the mutex holder; None = free


# ── The controller ─────────────────────────────────────────────────────────
class AdmissionController:
    """In-memory, restart-safe admission control for the cycle fleet.

    Concurrency (vendored HealScheduler shape): a global cap + a per-tenant cap.
    Politeness: a per-``canonical_host`` :class:`TokenBucket` (rate =
    ``fences.max_rps``, burst 2×) + a per-host mutex.  All state is process-local
    and starts empty.

    ``max_global <= 0`` disables the global cap; ``max_per_tenant <= 0`` disables
    the per-tenant cap (mirrors HealScheduler); the per-host politeness controls
    are ALWAYS on (a customer's app is protected regardless of cap tuning).
    """

    def __init__(
        self,
        *,
        max_global: int = 8,
        max_per_tenant: int = 2,
        burst_factor: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_global = int(max_global)
        self.max_per_tenant = int(max_per_tenant)
        self.burst_factor = max(1.0, float(burst_factor))
        self._clock = clock

        self._global_running = 0
        self._per_tenant: dict[str, int] = {}
        self._hosts: dict[str, _HostState] = {}
        # Round-robin observability: order in which tenants were last admitted.
        self._admit_seq = 0
        self._last_admit_seq: dict[str, int] = {}
        self._lock = asyncio.Lock()

    # ── capacity predicate (vendored heal_scheduler.py:78-84) ──────────────
    def _has_global_capacity(self) -> bool:
        return not (self.max_global > 0 and self._global_running >= self.max_global)

    def _has_tenant_capacity(self, tenant_id: str) -> bool:
        return not (
            self.max_per_tenant > 0
            and self._per_tenant.get(tenant_id, 0) >= self.max_per_tenant
        )

    def _bucket_for(self, host: str, rate: float) -> TokenBucket:
        state = self._hosts.get(host)
        if state is None:
            state = _HostState(
                bucket=TokenBucket(rate, burst_factor=self.burst_factor, clock=self._clock)
            )
            self._hosts[host] = state
        elif state.bucket.rate != float(rate):
            # The app's fence changed its max_rps — adopt it without a refund.
            state.bucket.reconfigure(rate, burst_factor=self.burst_factor)
        return state.bucket

    async def try_admit(
        self, *, tenant_id: str, canonical_host: str, max_rps: float,
    ) -> AdmissionDecision:
        """Attempt to admit ONE cycle (non-blocking).

        Checks, in order (cheap + reversible first; the token is consumed LAST so
        a denial never wastes politeness budget):
          1. ``canonical_host`` present            → else ``host_unconfigured``;
          2. ``max_rps > 0``                       → else ``rate_unconfigured``;
          3. per-host mutex free                   → else ``host_busy``;
          4. global concurrency capacity           → else ``global_cap``;
          5. per-tenant concurrency capacity       → else ``per_tenant_cap``;
          6. a host token available (consume it)   → else ``rate_limited``.

        On success returns ``admitted=True`` with an :class:`AdmissionLease`.
        """
        tenant = str(tenant_id or "")
        host = str(canonical_host or "").strip().lower()

        if not host:
            return AdmissionDecision(False, REASON_HOST_UNCONFIGURED)
        if max_rps is None or float(max_rps) <= 0:
            # FAIL-CLOSED politeness: never crawl a host we cannot rate-key.
            return AdmissionDecision(False, REASON_RATE_UNCONFIGURED)

        async with self._lock:
            host_state = self._hosts.get(host)
            if host_state is not None and host_state.lease_id is not None:
                return AdmissionDecision(False, REASON_HOST_BUSY)

            if not self._has_global_capacity():
                return AdmissionDecision(False, REASON_GLOBAL_CAP)
            if not self._has_tenant_capacity(tenant):
                return AdmissionDecision(False, REASON_PER_TENANT_CAP)

            bucket = self._bucket_for(host, float(max_rps))
            if not bucket.try_consume(1.0):
                return AdmissionDecision(
                    False, REASON_RATE_LIMITED,
                    retry_after_seconds=round(bucket.retry_after(1.0), 4),
                )

            # ── Grant ──────────────────────────────────────────────────────
            lease = AdmissionLease(
                lease_id=uuid.uuid4().hex,
                tenant_id=tenant,
                canonical_host=host,
                max_rps=float(max_rps),
                acquired_at=self._clock(),
            )
            self._global_running += 1
            self._per_tenant[tenant] = self._per_tenant.get(tenant, 0) + 1
            self._hosts[host].lease_id = lease.lease_id
            self._admit_seq += 1
            self._last_admit_seq[tenant] = self._admit_seq

        logger.info(
            "qec.admission.admitted",
            extra={"tenant_id": tenant, "canonical_host": host,
                   "lease_id": lease.lease_id, "max_rps": float(max_rps)},
        )
        return AdmissionDecision(True, REASON_ADMITTED, lease=lease)

    async def release(self, lease: Optional[AdmissionLease]) -> None:
        """Return a lease's global slot, per-tenant slot, and per-host mutex.

        Idempotent + safe: a ``None`` lease is ignored, and a stale release never
        frees another cycle's host mutex (the mutex is freed only when the stored
        holder matches this lease).
        """
        if lease is None:
            return
        async with self._lock:
            self._global_running = max(0, self._global_running - 1)
            remaining = max(0, self._per_tenant.get(lease.tenant_id, 0) - 1)
            if remaining == 0:
                self._per_tenant.pop(lease.tenant_id, None)
            else:
                self._per_tenant[lease.tenant_id] = remaining
            state = self._hosts.get(lease.canonical_host)
            if state is not None and state.lease_id == lease.lease_id:
                state.lease_id = None
        logger.info(
            "qec.admission.released",
            extra={"tenant_id": lease.tenant_id, "canonical_host": lease.canonical_host,
                   "lease_id": lease.lease_id},
        )

    def snapshot(self) -> dict:
        """Observability snapshot (mirrors HealScheduler.snapshot; no secrets).

        Best-effort read WITHOUT the async lock (like the vendored original) —
        counts/tokens may be momentarily stale under contention, which is fine
        for a metrics/diagnostics surface.
        """
        held_hosts = {
            host: state.lease_id
            for host, state in self._hosts.items()
            if state.lease_id is not None
        }
        host_buckets = {
            host: {
                "tokens": state.bucket.peek(),
                "capacity": round(state.bucket.capacity, 4),
                "rate": state.bucket.rate,
                "mutex_held": state.lease_id is not None,
            }
            for host, state in self._hosts.items()
        }
        return {
            "global_running": self._global_running,
            "per_tenant_running": dict(self._per_tenant),
            "max_global": self.max_global,
            "max_per_tenant": self.max_per_tenant,
            "held_hosts": held_hosts,
            "host_buckets": host_buckets,
            "last_admit_seq": dict(self._last_admit_seq),
        }


# ── Process-wide singleton (env-tuned, like heal_scheduler.SCHEDULER) ──────
def build_default_controller() -> AdmissionController:
    """Build the controller with caps from the environment (§3.5 env knobs).

    ``QEC_MAX_GLOBAL_CYCLES`` / ``QEC_MAX_PER_TENANT_CYCLES`` mirror
    ``NEXUS_HEAL_MAX_*``; ``QEC_HOST_BURST_FACTOR`` tunes the token-bucket burst
    (default 2× per §3.5).  Defaults are dev-safe and MUST be tuned per deploy.
    """
    return AdmissionController(
        max_global=int(os.getenv("QEC_MAX_GLOBAL_CYCLES", "8") or 8),
        max_per_tenant=int(os.getenv("QEC_MAX_PER_TENANT_CYCLES", "2") or 2),
        burst_factor=float(os.getenv("QEC_HOST_BURST_FACTOR", "2.0") or 2.0),
    )


#: Import as ``from app.controlplane.scheduling import ADMISSION``.
ADMISSION = build_default_controller()


__all__ = [
    "TokenBucket",
    "AdmissionLease",
    "AdmissionDecision",
    "AdmissionController",
    "build_default_controller",
    "ADMISSION",
    "REASON_ADMITTED",
    "REASON_GLOBAL_CAP",
    "REASON_PER_TENANT_CAP",
    "REASON_HOST_BUSY",
    "REASON_RATE_LIMITED",
    "REASON_RATE_UNCONFIGURED",
    "REASON_HOST_UNCONFIGURED",
    "RETRYABLE_REASONS",
    "FAIL_CLOSED_REASONS",
]
