"""QE-Central S5 — admission-controller pure-logic tests (no DB, no network).

Pins the three properties the Phase-4 honesty/fairness gate requires (design
§3.5 / §6 exit metrics):

  * **no starvation** — with ``max_per_tenant < max_global`` one tenant can never
    occupy all global slots, so a second tenant always gets its turn;
  * **per-host rate cap honored** — the per-canonical-host token bucket admits at
    ``max_rps`` with a 2× burst ceiling and no more;
  * **buckets start EMPTY on construct** — a fresh controller admits NOTHING on a
    host until a refill interval passes (a restart can never burst a customer).

A deterministic injected clock replaces the wall clock so the token-bucket
behaviour is exact and fast.  The concurrency dimension is isolated from the
rate limiter with a ``warm`` helper that pre-creates + fills a host's bucket.
"""
from __future__ import annotations

import asyncio

from app.controlplane.scheduling.admission import (
    REASON_GLOBAL_CAP,
    REASON_HOST_BUSY,
    REASON_HOST_UNCONFIGURED,
    REASON_PER_TENANT_CAP,
    REASON_RATE_LIMITED,
    REASON_RATE_UNCONFIGURED,
    AdmissionController,
)


class FakeClock:
    """A hand-cranked monotonic clock for deterministic token-bucket tests."""

    def __init__(self, t: float = 0.0) -> None:
        self.t = float(t)

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += float(dt)


def run(coro):
    return asyncio.run(coro)


async def _warm(ctrl: AdmissionController, clock: FakeClock, host: str,
                *, rps: float = 1000.0, fill: float = 10.0) -> None:
    """Create ``host``'s bucket (starts EMPTY ⇒ denied) then let it refill.

    A denied admit consumes NO slot/mutex/token — it only lazily creates the
    bucket — so this isolates the concurrency dimension from the rate limiter.
    """
    decision = await ctrl.try_admit(tenant_id="warm", canonical_host=host, max_rps=rps)
    assert decision.admitted is False
    assert decision.reason == REASON_RATE_LIMITED
    clock.advance(fill)


# ── Fairness: no tenant starvation ─────────────────────────────────────────
class TestFairness:
    def test_per_tenant_cap_prevents_monopoly_and_starvation(self):
        clock = FakeClock()

        async def body():
            ctrl = AdmissionController(max_global=2, max_per_tenant=1, clock=clock)
            for h in ("a1.example", "a2.example", "b1.example"):
                await _warm(ctrl, clock, h)

            # Tenant A takes one slot.
            a1 = await ctrl.try_admit(tenant_id="A", canonical_host="a1.example", max_rps=1000)
            assert a1.admitted is True

            # Tenant A cannot grab a SECOND global slot (per-tenant cap = 1) even
            # though global capacity remains — this is the anti-monopoly rule.
            a2 = await ctrl.try_admit(tenant_id="A", canonical_host="a2.example", max_rps=1000)
            assert a2.admitted is False
            assert a2.reason == REASON_PER_TENANT_CAP

            # Tenant B is NOT starved — the reserved slot is available to it.
            b1 = await ctrl.try_admit(tenant_id="B", canonical_host="b1.example", max_rps=1000)
            assert b1.admitted is True

            # Releasing A frees its slot; A may admit again.
            await ctrl.release(a1.lease)
            a3 = await ctrl.try_admit(tenant_id="A", canonical_host="a2.example", max_rps=1000)
            assert a3.admitted is True

        run(body())

    def test_global_cap_enforced(self):
        clock = FakeClock()

        async def body():
            ctrl = AdmissionController(max_global=2, max_per_tenant=5, clock=clock)
            for h in ("h1", "h2", "h3"):
                await _warm(ctrl, clock, h)

            d1 = await ctrl.try_admit(tenant_id="A", canonical_host="h1", max_rps=1000)
            d2 = await ctrl.try_admit(tenant_id="B", canonical_host="h2", max_rps=1000)
            d3 = await ctrl.try_admit(tenant_id="C", canonical_host="h3", max_rps=1000)
            assert d1.admitted and d2.admitted
            assert d3.admitted is False
            assert d3.reason == REASON_GLOBAL_CAP

        run(body())


# ── Per-host politeness: mutex + token bucket ──────────────────────────────
class TestHostPoliteness:
    def test_per_host_mutex_one_cycle_at_a_time(self):
        clock = FakeClock()

        async def body():
            ctrl = AdmissionController(max_global=10, max_per_tenant=10, clock=clock)
            await _warm(ctrl, clock, "shared.example")

            first = await ctrl.try_admit(tenant_id="A", canonical_host="shared.example", max_rps=1000)
            assert first.admitted is True

            # A different tenant targeting the SAME host is blocked by the mutex.
            second = await ctrl.try_admit(tenant_id="B", canonical_host="shared.example", max_rps=1000)
            assert second.admitted is False
            assert second.reason == REASON_HOST_BUSY

            await ctrl.release(first.lease)
            third = await ctrl.try_admit(tenant_id="B", canonical_host="shared.example", max_rps=1000)
            assert third.admitted is True

        run(body())

    def test_rate_cap_honored_at_max_rps(self):
        clock = FakeClock()

        async def body():
            ctrl = AdmissionController(max_global=100, max_per_tenant=100, clock=clock)
            host = "rate.example"

            # Fresh bucket at t=0 is EMPTY ⇒ the very first admit is rate-limited,
            # with a retry hint of one token / rate = 1/2 = 0.5s.
            d0 = await ctrl.try_admit(tenant_id="A", canonical_host=host, max_rps=2)
            assert d0.admitted is False
            assert d0.reason == REASON_RATE_LIMITED
            assert abs(d0.retry_after_seconds - 0.5) < 1e-6

            # After 0.5s exactly one token exists ⇒ one admit succeeds.
            clock.advance(0.5)
            d1 = await ctrl.try_admit(tenant_id="A", canonical_host=host, max_rps=2)
            assert d1.admitted is True
            await ctrl.release(d1.lease)

            # Immediately trying again (no refill) is rate-limited: proves the cap
            # is on FREQUENCY, not just concurrency.
            d2 = await ctrl.try_admit(tenant_id="A", canonical_host=host, max_rps=2)
            assert d2.admitted is False
            assert d2.reason == REASON_RATE_LIMITED

        run(body())

    def test_burst_ceiling_is_two_times_rate(self):
        clock = FakeClock()

        async def body():
            ctrl = AdmissionController(max_global=100, max_per_tenant=100, clock=clock)
            host = "burst.example"
            await _warm(ctrl, clock, host, rps=2, fill=0.0)  # create bucket, rps=2 ⇒ cap 4

            # Idle a very long time — tokens must CAP at burst (2×2 = 4), never 200.
            clock.advance(100.0)

            admitted = 0
            for _ in range(6):
                d = await ctrl.try_admit(tenant_id="A", canonical_host=host, max_rps=2)
                if d.admitted:
                    admitted += 1
                    await ctrl.release(d.lease)  # free the mutex, keep depleting tokens
                else:
                    assert d.reason == REASON_RATE_LIMITED
                    break
            assert admitted == 4  # exactly the burst ceiling, no accumulation beyond 2×

        run(body())

    def test_fresh_controller_never_bursts_on_restart(self):
        clock = FakeClock()

        async def body():
            ctrl = AdmissionController(max_global=100, max_per_tenant=100, clock=clock)
            host = "cold.example"
            # At t=0, on a brand-new controller, NOTHING is admitted on the host —
            # every attempt is rate-limited because the bucket starts empty.
            for _ in range(5):
                d = await ctrl.try_admit(tenant_id="A", canonical_host=host, max_rps=50)
                assert d.admitted is False
                assert d.reason == REASON_RATE_LIMITED
                assert d.retry_after_seconds > 0
            assert ctrl.snapshot()["global_running"] == 0

        run(body())


# ── Fail-closed politeness + release safety ────────────────────────────────
class TestFailClosedAndRelease:
    def test_missing_rate_is_fail_closed(self):
        clock = FakeClock()

        async def body():
            ctrl = AdmissionController(clock=clock)
            d = await ctrl.try_admit(tenant_id="A", canonical_host="h.example", max_rps=0)
            assert d.admitted is False
            assert d.reason == REASON_RATE_UNCONFIGURED
            assert d.retryable is False  # will not clear without operator action

        run(body())

    def test_missing_host_is_fail_closed(self):
        clock = FakeClock()

        async def body():
            ctrl = AdmissionController(clock=clock)
            d = await ctrl.try_admit(tenant_id="A", canonical_host="", max_rps=5)
            assert d.admitted is False
            assert d.reason == REASON_HOST_UNCONFIGURED

        run(body())

    def test_release_is_idempotent_and_never_frees_foreign_mutex(self):
        clock = FakeClock()

        async def body():
            ctrl = AdmissionController(max_global=10, max_per_tenant=10, clock=clock)
            await _warm(ctrl, clock, "h.example")
            d = await ctrl.try_admit(tenant_id="A", canonical_host="h.example", max_rps=1000)
            assert d.admitted is True

            await ctrl.release(None)              # no-op, never raises
            await ctrl.release(d.lease)           # real release
            await ctrl.release(d.lease)           # double release must not underflow

            snap = ctrl.snapshot()
            assert snap["global_running"] == 0
            assert snap["per_tenant_running"] == {}
            assert snap["held_hosts"] == {}

        run(body())

    def test_snapshot_shape(self):
        clock = FakeClock()

        async def body():
            ctrl = AdmissionController(max_global=4, max_per_tenant=2, clock=clock)
            await _warm(ctrl, clock, "h.example")
            d = await ctrl.try_admit(tenant_id="A", canonical_host="h.example", max_rps=1000)
            snap = ctrl.snapshot()
            assert snap["max_global"] == 4
            assert snap["max_per_tenant"] == 2
            assert snap["global_running"] == 1
            assert snap["per_tenant_running"]["A"] == 1
            assert "h.example" in snap["held_hosts"]
            assert snap["host_buckets"]["h.example"]["mutex_held"] is True
            await ctrl.release(d.lease)

        run(body())
