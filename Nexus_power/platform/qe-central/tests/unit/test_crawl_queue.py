"""Phase 2 — tests for the durable-queue admission + fair drain (pure logic).

Pins the two guarantees: a per-host CONCURRENCY cap (not the cycle mutex), so several
same-host crawls run at once; and round-robin drain fairness, so a flooding tenant
cannot starve another's single crawl (the headline adversarial proof). Also: an
un-provisioned tenant (no caps) is always admitted — byte-identical to today.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.controlplane.scheduling import crawl_queue as q


T0 = datetime(2026, 7, 16, 9, 0, 0, tzinfo=timezone.utc)


def _row(tenant, i, seconds):
    return {"tenant_id": tenant, "exploration_id": f"{tenant}-{i}",
            "created_at": T0 + timedelta(seconds=seconds)}


# ── per-host concurrency (NOT a mutex) ────────────────────────────────────────
def test_host_cap_allows_multiple_same_host_crawls():
    # cap=3 → up to three concurrent crawls of the SAME host (the mutex allowed 1).
    assert q.host_admits({"acme.example": 0}, "acme.example", 3)
    assert q.host_admits({"acme.example": 2}, "acme.example", 3)
    assert not q.host_admits({"acme.example": 3}, "acme.example", 3)


def test_unconfigured_cap_always_admits():
    # No cap configured → admit (never fail-closed 422), byte-identical to today.
    assert q.host_admits({"acme.example": 99}, "acme.example", None)
    assert q.host_admits({"acme.example": 99}, "acme.example", 0)
    assert q.tenant_admits({"t": 99}, "t", None)


def test_plan_admission_admits_when_unconfigured():
    verdict, reason = q.plan_admission(
        host="acme.example", tenant="t", active_by_host={"acme.example": 50},
        active_by_tenant={"t": 50}, host_cap=None, tenant_cap=None,
    )
    assert verdict == q.ADMIT and reason == ""


def test_plan_admission_queues_over_host_cap():
    verdict, reason = q.plan_admission(
        host="acme.example", tenant="t", active_by_host={"acme.example": 2},
        active_by_tenant={"t": 2}, host_cap=2, tenant_cap=None,
    )
    assert verdict == q.QUEUE and reason == "per_host_concurrency_cap"


def test_plan_admission_queues_over_tenant_cap_first():
    verdict, reason = q.plan_admission(
        host="acme.example", tenant="t", active_by_host={"acme.example": 0},
        active_by_tenant={"t": 2}, host_cap=None, tenant_cap=2,
    )
    assert verdict == q.QUEUE and reason == "per_tenant_concurrency_cap"


# ── fair drain — the headline anti-starvation proof ───────────────────────────
def test_flooding_tenant_cannot_starve_a_single_crawl():
    # Tenant A floods 20 crawls; tenant B enqueues 1 slightly later.
    a = [_row("A", i, i) for i in range(20)]
    b = [_row("B", 0, 100)]
    order = q.fair_drain_order(a + b)
    # B's single crawl must be served in the FIRST round (position <= 2), NOT stuck
    # behind all 20 of A's — round-robin, not global FIFO.
    pos = [r["exploration_id"] for r in order].index("B-0")
    assert pos <= 1, f"B starved at position {pos}"


def test_drain_is_fifo_within_a_tenant():
    a = [_row("A", 2, 20), _row("A", 0, 5), _row("A", 1, 10)]
    order = [r["exploration_id"] for r in q.fair_drain_order(a)]
    assert order == ["A-0", "A-1", "A-2"]  # by created_at within the tenant


def test_round_robin_interleaves_tenants():
    a = [_row("A", i, i) for i in range(3)]
    b = [_row("B", i, i) for i in range(3)]
    order = [r["tenant_id"] for r in q.fair_drain_order(a + b)]
    # Each round emits one per tenant → alternating tenants.
    assert order.count("A") == 3 and order.count("B") == 3
    assert order[0] != order[1]  # not all of A before any of B


def test_drain_respects_limit():
    a = [_row("A", i, i) for i in range(10)]
    assert len(q.fair_drain_order(a, limit=3)) == 3


def test_queue_positions_are_1_based_fair_order():
    a = [_row("A", i, i) for i in range(3)]
    b = [_row("B", 0, 100)]
    pos = q.queue_positions(a + b)
    assert pos["A-0"] == 1
    assert pos["B-0"] == 2  # served second (round 1, after A's oldest)
    assert set(pos.values()) == {1, 2, 3, 4}


def test_deterministic_order():
    rows = [_row("A", 1, 1), _row("B", 0, 0), _row("A", 0, 0)]
    assert q.fair_drain_order(rows) == q.fair_drain_order(rows)


def test_claim_sql_uses_skip_locked():
    # The drainer must never double-claim a row across racing ticks.
    assert "FOR UPDATE SKIP LOCKED" in q.CLAIM_QUEUED_SQL
    assert "status = 'queued'" in q.CLAIM_QUEUED_SQL
