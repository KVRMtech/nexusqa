"""M3.4 / T-RS-03 - PER-TENANT QUOTAS ENFORCED ON THE CRAWL DISPATCH PATH.

THE BYPASS THIS GATES.  Quota enforcement was not missing - it was HALF-WIRED.
``enforce_cycle_quota`` is called from ``run_cycle``, so the SCHEDULED door was
capped; but ``POST /explorations`` and ``POST /explorations/{id}/resume`` both
reach a worker through ``_dispatch_explorer`` WITHOUT creating a cycle, so from
that direction a tenant sitting at its monthly browser-second ceiling could keep
dispatching crawls forever.  A cap that one of two doors ignores is not a cap.

These proofs run WITHOUT a database.  The counting queries are exercised against
a fake session, which is the honest boundary for a unit proof: what is being
gated here is the DECISION and the WIRING, and the wiring is the half that was
actually broken.  ``tests/fleet/`` covers the live-Postgres counting.

The last test is the one that matters most: it asserts the enforcement sits at
the SHARED choke point rather than on the individual routes, so "another dispatch
path" is a contradiction rather than a gap to be re-audited every release.
"""
from __future__ import annotations

import ast
import inspect
import os
import sys
from decimal import Decimal

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from app.fleet import quota  # noqa: E402

PLAN = quota.QuotaPlan(
    name="m34", max_concurrent_crawls=3,
    monthly_browser_seconds=Decimal("1000"),
)
UNLIMITED = quota.DEFAULT_PLAN


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value

    def scalars(self):
        return self

    def all(self):
        return list(self._value or ())


class _FakeSession:
    """Answers the count query with a canned number and records the calls."""

    def __init__(self, count=0, ledger_rows=()):
        self.count = count
        self.ledger_rows = ledger_rows
        self.executed = 0

    async def execute(self, stmt):
        self.executed += 1
        text = str(stmt).lower()
        if "count(" in text:
            return _FakeResult(self.count)
        return _FakeResult(self.ledger_rows)


# ── 1. below quota → allowed ────────────────────────────────────────────────
def test_a_tenant_below_its_crawl_quota_is_allowed():
    decision = quota.check_quota(PLAN, quota.RESOURCE_CONCURRENT_CRAWLS, 1)
    assert decision.allowed and not decision.denied
    assert decision.reason == quota.REASON_ALLOWED


@pytest.mark.asyncio
async def test_enforcement_admits_a_tenant_under_the_cap():
    session = _FakeSession(count=2)          # cap is 3
    await quota.enforce_crawl_quota("t-under", session=session, plan=PLAN)
    assert session.executed >= 1, "the cap was declared but never actually counted"


# ── 2. approaching quota → measured, not yet refused ────────────────────────
def test_the_last_admissible_crawl_is_measured_and_still_allowed():
    """usage == limit-1 is the final admission, and it reports its own headroom."""
    decision = quota.check_quota(PLAN, quota.RESOURCE_CONCURRENT_CRAWLS, 2)
    assert decision.allowed
    assert decision.limit == Decimal("3") and decision.usage == Decimal("2")
    assert decision.as_dict()["plan"] == "m34"


# ── 3. exceeds quota → throttled ────────────────────────────────────────────
def test_a_tenant_at_its_cap_is_refused_fail_closed():
    """AT the cap denies, not merely OVER it: admitting one more needs headroom."""
    decision = quota.check_quota(PLAN, quota.RESOURCE_CONCURRENT_CRAWLS, 3)
    assert decision.denied
    assert decision.reason == "max_concurrent_crawls_exceeded"


@pytest.mark.asyncio
async def test_enforcement_raises_a_429_when_the_crawl_cap_is_reached():
    session = _FakeSession(count=3)          # cap is 3 → at the ceiling
    with pytest.raises(quota.QuotaExceeded) as caught:
        await quota.enforce_crawl_quota("t-over", session=session, plan=PLAN)
    exc = caught.value
    assert exc.status_code == 429
    assert exc.resource == quota.RESOURCE_CONCURRENT_CRAWLS
    detail = exc.as_http_detail()
    # ``reason`` is the generic category the router surfaces; ``quota_reason``
    # carries the specific cap that bit. Both matter: an operator needs to know
    # it was a quota AND which one.
    assert detail["reason"] == "quota_exceeded"
    assert detail["quota_reason"] == "max_concurrent_crawls_exceeded"
    assert detail["refused"] is True


@pytest.mark.asyncio
async def test_monthly_spend_throttles_the_crawl_path_too():
    """The cap a crawl was most able to evade: metered spend with no cycle row."""
    plan = quota.QuotaPlan(name="m34s", monthly_browser_seconds=Decimal("100"))

    class _Rows(_FakeSession):
        async def execute(self, stmt):
            self.executed += 1
            return _FakeResult([])

    session = _Rows()
    # No spend recorded → admitted.
    await quota.enforce_crawl_quota("t-spend", session=session, plan=plan)

    # Spend at the ceiling → refused, without any cycle ever being created.
    def _at_cap(entries, unit):
        return Decimal("100")

    import app.fleet.quota as q
    original = q.sum_unit
    q.sum_unit = _at_cap
    try:
        with pytest.raises(quota.QuotaExceeded) as caught:
            await quota.enforce_crawl_quota("t-spend", session=session, plan=plan)
        assert caught.value.resource == quota.RESOURCE_MONTHLY_BROWSER_SECONDS
    finally:
        q.sum_unit = original


# ── 4. an unrelated tenant is unaffected ────────────────────────────────────
@pytest.mark.asyncio
async def test_an_unprovisioned_tenant_is_untouched_and_costs_no_query():
    """The default plan must open no session and run no query at all.

    This is the backward-compatibility invariant the whole quota module rests
    on: every existing tenant is un-provisioned, so a cap that queried on the
    default path would put a database round-trip in front of every crawl in the
    fleet to answer a question whose answer is always 'yes'.
    """
    session = _FakeSession(count=9999)
    await quota.enforce_crawl_quota("t-other", session=session, plan=UNLIMITED)
    assert session.executed == 0, (
        "an unlimited plan inspected usage; the default path is no longer free")


@pytest.mark.asyncio
async def test_one_tenant_at_its_cap_does_not_refuse_another():
    """Two tenants, one over and one under, enforced through the same helper."""
    with pytest.raises(quota.QuotaExceeded):
        await quota.enforce_crawl_quota(
            "tenant-a", session=_FakeSession(count=3), plan=PLAN)
    await quota.enforce_crawl_quota(
        "tenant-b", session=_FakeSession(count=0), plan=PLAN)


# ── 5. quota reset → service resumes ────────────────────────────────────────
@pytest.mark.asyncio
async def test_service_resumes_once_the_tenant_drops_below_the_cap():
    """A crawl finishing is what resets concurrency - no operator action needed."""
    with pytest.raises(quota.QuotaExceeded):
        await quota.enforce_crawl_quota(
            "t-cycle", session=_FakeSession(count=3), plan=PLAN)
    # A crawl reaches a terminal status → it stops being counted → admitted.
    await quota.enforce_crawl_quota(
        "t-cycle", session=_FakeSession(count=2), plan=PLAN)


def test_terminal_statuses_are_the_ones_that_free_a_slot():
    """A crawl still in flight must keep counting, or the cap leaks.

    Declared as the TERMINAL set rather than the active one so a status added
    later counts as ACTIVE by default: a new state that silently escaped the cap
    is a quota hole, while one briefly over-counted is merely conservative.
    """
    assert "completed" in quota.TERMINAL_EXPLORATION_STATUSES
    assert "failed" in quota.TERMINAL_EXPLORATION_STATUSES
    for in_flight in ("pending", "dispatched", "writing"):
        assert in_flight not in quota.TERMINAL_EXPLORATION_STATUSES, in_flight


# ── 6. THE BYPASS IS CLOSED ─────────────────────────────────────────────────
def test_every_crawl_dispatch_route_funnels_through_the_guarded_choke_point():
    """No route may reach a worker without passing the quota gate.

    Asserted STRUCTURALLY rather than by calling the routes, because the claim
    is about reachability, not about one execution: it is that ``_dispatch_explorer``
    is the only way to a worker and that it is guarded. A test that merely drove
    the two routes we know about today would keep passing on the day a third one
    is added - which is precisely how the original bypass came to exist.
    """
    from app.routers import explorations

    source = inspect.getsource(explorations)
    tree = ast.parse(source)

    guarded = None
    dispatchers = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = ast.dump(node)
        if "enforce_crawl_quota" in body:
            guarded = node.name
        # Anything that hands a crawl to a worker.
        if "dispatch_crawl" in body and node.name != "dispatch_crawl":
            dispatchers.add(node.name)

    assert guarded == "_dispatch_explorer", (
        "the crawl quota gate is not on the shared dispatch path (found on %r)"
        % (guarded,))
    assert dispatchers == {"_dispatch_explorer"}, (
        "a crawl reaches a worker from somewhere other than the guarded choke "
        "point, so the quota can be bypassed from: %s"
        % (sorted(dispatchers - {"_dispatch_explorer"}),))


def test_the_gate_precedes_worker_reservation_and_the_egress_fence():
    """A refused tenant must not consume a fleet slot or leave a fence behind.

    Ordering is the property: enforcing AFTER the reservation would still return
    429, but it would have taken a worker slot and written an allowlist file on
    a worker the tenant was never allowed to use.
    """
    from app.routers import explorations

    src = inspect.getsource(explorations._dispatch_explorer)
    gate = src.index("enforce_crawl_quota")
    assert gate < src.index("reserve_worker"), "quota gate runs after reservation"
    assert gate < src.index("_write_egress_allowlist"), "quota gate runs after fencing"


def test_the_resume_route_is_covered_by_the_same_gate():
    """Resume is a dispatch. It was the likelier bypass, being the newer door."""
    from app.routers import explorations

    src = inspect.getsource(explorations.resume_exploration)
    assert "_dispatch_explorer" in src, (
        "resume no longer funnels through the guarded dispatch path")
