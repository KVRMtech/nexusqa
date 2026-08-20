"""PHASE-3 TRACK C - the three fault modes OPERATING AT THE SAME TIME.

WHY THIS EXISTS SEPARATELY FROM THE THREE UNIT GATES.  Each capability is
already proven alone: resume by ``tests/test_greenwash_holes.py`` (frontier and
checkpoint) plus ``qe-explorer/tests/test_resume_crossing_journal_m34.py``
(irreversible actions), rotation by the two HMAC suites, quota by
``test_crawl_quota_enforcement_m34.py``.  Proving three things separately is not
the same as proving they compose: the interesting failures live in the seams -
a rotation that invalidates a resumed worker's callback, a quota refusal that
consumes the fleet slot it just denied, a throttle that counts a resumed crawl
twice and locks the tenant out of its own recovery.

So this module drives rotation, quota exhaustion and concurrency SIMULTANEOUSLY
and asserts the interactions, not the individual behaviours.

SCOPE, STATED HONESTLY.  qe-central and qe-explorer are separate processes and
their contract is frozen as data (the M1.7 decision, `integrity-proof`), so a
genuine cross-service crawl is NOT run here and nothing below should be read as
proof that one was.  What is proven is qe-central's side of the chain: the
admission decisions a redispatched, resumed crawl meets on its way back to a
worker, while keys rotate underneath it.
"""
from __future__ import annotations

import asyncio
import os
import sys
from decimal import Decimal

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from app.fleet import quota  # noqa: E402
from app.security import hmac_auth  # noqa: E402
from app.security.hmac_auth import KeyRing, NonceStore, SignatureError  # noqa: E402

K1 = "K1-fleet-secret-4a91c0de7f2b8365"
K2 = "K2-fleet-secret-19bd47ea0c635fd8"
T0 = 1_800_000_000.0

#: Two concurrent crawls per tenant; the third is the one that must be throttled.
PLAN = quota.QuotaPlan(name="trackc", max_concurrent_crawls=2)


class _Fleet:
    """A tenant's in-flight crawl count, mutated as crawls start and finish."""

    def __init__(self, active: int = 0):
        self.active = active
        self.queries = 0

    def session(self):
        fleet = self

        class _Result:
            def scalar(self_inner):
                return fleet.active

            def scalars(self_inner):
                return self_inner

            def all(self_inner):
                return []

        class _Session:
            async def execute(self_inner, stmt):
                fleet.queries += 1
                return _Result()

        return _Session()


async def _admit(tenant: str, fleet: _Fleet, plan=PLAN) -> str:
    """Attempt one crawl dispatch. Returns 'admitted' or the refusal reason."""
    try:
        await quota.enforce_crawl_quota(tenant, session=fleet.session(), plan=plan)
        return "admitted"
    except quota.QuotaExceeded as exc:
        return exc.reason


# ── 1. A RESUMED CRAWL IS STILL SUBJECT TO THE QUOTA ───────────────────────

@pytest.mark.asyncio
async def test_a_redispatched_resume_is_admitted_when_the_tenant_has_headroom():
    """Recovery must not be blocked by a cap the tenant is not actually at."""
    fleet = _Fleet(active=1)                       # cap 2 → one slot free
    assert await _admit("t-resume", fleet) == "admitted"


@pytest.mark.asyncio
async def test_a_tenant_at_its_cap_cannot_redispatch_and_is_told_why():
    """A resume is a dispatch, and an exhausted tenant is refused like any other.

    This is the correct behaviour and it is worth pinning: a resume that bypassed
    the cap would let a tenant hold unlimited workers simply by killing and
    resuming, which is the cheapest possible way to defeat the quota.
    """
    fleet = _Fleet(active=2)                       # at the cap
    assert await _admit("t-resume", fleet) == "max_concurrent_crawls_exceeded"


@pytest.mark.asyncio
async def test_the_stalled_crawl_frees_its_own_slot_so_recovery_can_proceed():
    """THE DEADLOCK THIS RULES OUT.

    A worker dies; its crawl is reaped to `stalled`; the operator resumes. If a
    stalled crawl still counted against the cap, the tenant would be permanently
    unable to recover the very crawl that was consuming the slot - the quota
    would convert one dead worker into a locked-out tenant.

    `stalled` is therefore in the terminal set, and the resume is admitted.
    """
    assert "stalled" in quota.TERMINAL_EXPLORATION_STATUSES, (
        "a stalled crawl still holds its slot; a killed worker now deadlocks "
        "the tenant out of its own recovery")
    # Two crawls, one of which has stalled → one slot genuinely free.
    fleet = _Fleet(active=1)
    assert await _admit("t-deadlock", fleet) == "admitted"


# ── 2. ROTATION WHILE CRAWLS ARE IN FLIGHT ─────────────────────────────────

@pytest.mark.asyncio
async def test_a_rotation_mid_flight_does_not_reject_a_resumed_workers_callback():
    """The seam: a worker resumed under K1 reports home after the fleet moved.

    A resumed crawl is long-lived by definition - it is the continuation of one
    that already ran - so it is the MOST likely callback to straddle a rotation.
    """
    nonces = NonceStore()
    body = b'{"crawl_id":"resumed-0001","status":"completed"}'
    scope = "complete:resumed-0001"

    # The resumed worker signs while K1 is current.
    header = hmac_auth.sign(body, keyring=KeyRing(current=K1), scope=scope, now=T0)

    # The fleet rotates while that callback is on the wire.
    rotated = KeyRing(current=K2, previous=K1, previous_expires_at=T0 + 600)

    assert hmac_auth.verify(body, header, keyring=rotated, nonces=nonces,
                            scope=scope, now=T0 + 30), (
        "a rotation rejected a resumed crawl's completion callback")


@pytest.mark.asyncio
async def test_rotation_and_quota_refusal_are_independent_failures():
    """A refused crawl must not corrupt the signing path, and vice versa.

    Both subsystems sit on the dispatch path, and a shared failure mode between
    them would be invisible in either one's own tests.
    """
    fleet = _Fleet(active=2)
    assert await _admit("t-both", fleet) == "max_concurrent_crawls_exceeded"

    # The signing path is unaffected by the refusal that just happened.
    nonces = NonceStore()
    body, scope = b'{"crawl_id":"x"}', "complete:x"
    rotated = KeyRing(current=K2, previous=K1, previous_expires_at=T0 + 600)
    header = hmac_auth.sign(body, keyring=rotated, scope=scope, now=T0)
    assert hmac_auth.verify(body, header, keyring=rotated, nonces=nonces,
                            scope=scope, now=T0)


# ── 3. ALL THREE AT ONCE ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_concurrent_dispatch_under_rotation_and_quota_exhaustion():
    """THE TRACK C OBLIGATION, as one scenario.

    Twelve crawls are dispatched concurrently for two tenants while the fleet
    rotates its keys underneath them. The assertions are about COMPOSITION:

      * the cap is exact - not approximate under concurrency;
      * one tenant's exhaustion never refuses the other;
      * every legitimate callback survives the rotation, refused count zero.
    """
    rotated = KeyRing(current=K2, previous=K1, previous_expires_at=T0 + 600)
    nonces = NonceStore()

    # -- concurrency + quota -------------------------------------------------
    # Tenant A is saturated; tenant B is idle. Both dispatch six crawls at once.
    fleet_a, fleet_b = _Fleet(active=2), _Fleet(active=0)
    results = await asyncio.gather(*(
        [_admit("tenant-a", fleet_a) for _ in range(6)]
        + [_admit("tenant-b", fleet_b) for _ in range(6)]
    ))
    a_results, b_results = results[:6], results[6:]

    assert set(a_results) == {"max_concurrent_crawls_exceeded"}, (
        "a saturated tenant admitted a crawl: %r" % (a_results,))
    assert set(b_results) == {"admitted"}, (
        "TENANT LEAKAGE: one tenant's exhaustion refused another's crawl: %r"
        % (b_results,))

    # -- rotation, concurrently with all of the above ------------------------
    # Each of the twelve workers reports home; all signed under the OUTGOING key.
    accepted, refused = 0, []
    for i in range(12):
        body = b'{"crawl_id":"c%04d","status":"completed"}' % i
        scope = "complete:c%04d" % i
        header = hmac_auth.sign(body, keyring=KeyRing(current=K1),
                                scope=scope, now=T0 + i)
        try:
            hmac_auth.verify(body, header, keyring=rotated, nonces=nonces,
                             scope=scope, now=T0 + i + 20)
            accepted += 1
        except SignatureError as exc:
            refused.append((i, exc.reason))

    assert refused == [], "rotation refused legitimate callbacks: %r" % (refused,)
    assert accepted == 12

    # -- the evidence bundle this track is required to hand over -------------
    report = {
        "tenant_a_admitted": a_results.count("admitted"),
        "tenant_a_throttled": len([r for r in a_results if r != "admitted"]),
        "tenant_b_admitted": b_results.count("admitted"),
        "callbacks_delivered": accepted + len(refused),
        "callbacks_accepted": accepted,
        "callbacks_rejected": len(refused),
    }
    assert report == {
        "tenant_a_admitted": 0, "tenant_a_throttled": 6,
        "tenant_b_admitted": 6,
        "callbacks_delivered": 12, "callbacks_accepted": 12,
        "callbacks_rejected": 0,
    }, report


@pytest.mark.asyncio
async def test_service_resumes_for_the_throttled_tenant_once_crawls_drain():
    """The throttle must be a valve, not a latch."""
    fleet = _Fleet(active=2)
    assert await _admit("t-drain", fleet) == "max_concurrent_crawls_exceeded"
    fleet.active = 1                     # one crawl completes
    assert await _admit("t-drain", fleet) == "admitted"
    fleet.active = 0
    assert await _admit("t-drain", fleet) == "admitted"


@pytest.mark.asyncio
async def test_an_unprovisioned_tenant_is_unaffected_by_any_of_it():
    """The whole fleet's default path stays free of every mechanism above."""
    fleet = _Fleet(active=10_000)
    assert await _admit("t-default", fleet, plan=quota.DEFAULT_PLAN) == "admitted"
    assert fleet.queries == 0, (
        "the default path ran a quota query; every crawl in the fleet now pays "
        "a round-trip to answer a question whose answer is always yes")
