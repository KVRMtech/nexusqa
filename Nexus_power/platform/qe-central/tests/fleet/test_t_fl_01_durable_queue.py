"""M3.3 / T-FL-01 — the durable queue, wired to the production dispatch path.

THE DEFECT
==========
When every worker was busy, dispatch marked the exploration row ``failed`` and
raised. A crawl was recorded as FAILED because the fleet was BUSY —
indistinguishable, in the row and in the UI, from a crawl that failed because
the customer's application is broken — and the work was lost entirely.

THE REQUIRED PROOF, run end to end below:

    crawl #1 running
    crawl #2 submitted   → crawl #2 = queued   (NOT failed)
    crawl #1 completes   → crawl #2 drains

Plus the fairness property: multiple tenants, and one tenant flooding the queue
cannot starve another tenant's single crawl.

These run against the production-like RLS database, so every queue read, claim
and requeue below is subject to the same tenant isolation production enforces.
"""
from __future__ import annotations

import os
import uuid

import pytest

QEC_DB_URL = os.environ.get("QEC_TEST_QEC_DATABASE_URL", "")
SUBSTRATE_DB_URL = os.environ.get("QEC_TEST_SUBSTRATE_DATABASE_URL", "")
if QEC_DB_URL:
    os.environ["QEC_DATABASE_URL"] = QEC_DB_URL
    os.environ["QEC_TEST_DB_NULLPOOL"] = "1"
if SUBSTRATE_DB_URL:
    os.environ["NEXUS_DATABASE_URL_SUBSTRATE"] = SUBSTRATE_DB_URL

from sqlalchemy import text  # noqa: E402

from app.controlplane.scheduling import crawl_queue, queue_store  # noqa: E402

needs_db = pytest.mark.skipif(
    not (QEC_DB_URL and SUBSTRATE_DB_URL),
    # The reason must NAME the variables. The A27.1 no-silent-skip gate
    # recognises an infrastructure skip by the environment variable in its
    # reason, so "needs the ... test DSNs" was invisible to it: had the CI
    # database failed to start, these tests would have skipped under
    # QEC_REQUIRE_DB and the build would still have gone green. Exactly the
    # hole that let six T-FL-03 object-storage tests never run.
    reason=("QEC_TEST_QEC_DATABASE_URL / QEC_TEST_SUBSTRATE_DATABASE_URL "
            "not set — T-FL-01 needs the qecentral + substrate test DSNs"),
)
pytestmark = [needs_db, pytest.mark.asyncio]


# ─── helpers ────────────────────────────────────────────────────────────────

async def _register_tenant(tenant: str) -> None:
    from app.db import substrate_engine
    async with substrate_engine.begin() as conn:
        await conn.execute(text(
            # `tenants.name` AND `tenants.domain` are both NOT NULL
            # (nexus_sdk.db.models.TenantRow), and domain is UNIQUE as well, so
            # an insert naming only tenant_id cannot succeed against the real
            # substrate schema. Deriving the domain from the tenant id keeps it
            # unique for free. It went unnoticed because the fleet suite had
            # never been run against a database built from the migration chain
            # — Gate 3 / A20 pushed the chain to CI for the first time and all
            # 19 of these tests failed on this one line.
            "INSERT INTO tenants (tenant_id, name, domain) "
            "VALUES (:t, :t, :t || '.test') "
            "ON CONFLICT (tenant_id) DO NOTHING"), {"t": tenant})


async def _mint(tenant: str, app_id: str, *, status: str, host: str = "app.example",
                created_offset_s: int = 0) -> str:
    """Insert an exploration row directly, in a given state."""
    from app.db import qec_engine
    from app.controlplane.tenant_scope import scope_to_tenant
    eid = "e_" + uuid.uuid4().hex[:16]
    async with qec_engine.begin() as conn:
        await scope_to_tenant(conn, tenant)
        await conn.execute(text(
            "INSERT INTO qe_explorations "
            "(exploration_id, tenant_id, app_id, status, extractor_version, "
            " started_at, created_at, updated_at, stats) "
            "VALUES (:eid, :t, :a, :st, 'qec-test', now(), "
            "        now() + make_interval(secs => :off), now(), "
            "        jsonb_build_object('target_host', CAST(:h AS text)))"
        ), {"eid": eid, "t": tenant, "a": app_id, "st": status, "h": host,
            "off": created_offset_s})
    return eid


async def _status_of(tenant: str, eid: str) -> str:
    from app.db import qec_engine
    from app.controlplane.tenant_scope import scope_to_tenant
    async with qec_engine.begin() as conn:
        await scope_to_tenant(conn, tenant)
        row = (await conn.execute(text(
            "SELECT status FROM qe_explorations WHERE exploration_id = :e"),
            {"e": eid})).first()
    return str(row[0]) if row else ""


async def _set_status(tenant: str, eid: str, status: str) -> None:
    from app.db import qec_engine
    from app.controlplane.tenant_scope import scope_to_tenant
    async with qec_engine.begin() as conn:
        await scope_to_tenant(conn, tenant)
        await conn.execute(text(
            "UPDATE qe_explorations SET status = :s, updated_at = now() "
            "WHERE exploration_id = :e"), {"s": status, "e": eid})


# ══════════════════════════════════════════════════════════════════════════
# THE REQUIRED SCENARIO
# ══════════════════════════════════════════════════════════════════════════

async def test_second_crawl_is_queued_not_failed_then_drains(monkeypatch):
    """crawl #1 running → #2 submitted → #2 QUEUED → #1 completes → #2 drains."""
    tenant = "tfl01_" + uuid.uuid4().hex[:8]
    await _register_tenant(tenant)
    monkeypatch.setenv(queue_store.ENV_TENANT_CAP, "1")

    # crawl #1 is RUNNING and occupies the tenant's single slot.
    running = await _mint(tenant, "app1", status="running")

    # crawl #2 arrives: the pure core must say QUEUE, not reject.
    verdict, reason = await queue_store.admission_verdict(
        tenant_id=tenant, host="app.example")
    assert verdict == crawl_queue.QUEUE, (
        "a second crawl was ADMITTED while the tenant was at its cap")
    assert reason == "per_tenant_concurrency_cap"

    # …and it is durably enqueued, NOT failed.
    queued = await _mint(tenant, "app2", status="pending")
    position = await queue_store.enqueue(
        tenant_id=tenant, exploration_id=queued, reason=reason)
    assert await _status_of(tenant, queued) == crawl_queue.STATUS_QUEUED, (
        "THE DEFECT: a crawl was not queued when the fleet was busy")
    assert position >= 1, "a queued crawl was given no position in the queue"

    # It must NOT be failed — that is the whole point.
    assert await _status_of(tenant, queued) != "failed"

    # crawl #1 completes → the tenant is under cap again.
    await _set_status(tenant, running, "completed")
    verdict2, _ = await queue_store.admission_verdict(
        tenant_id=tenant, host="app.example")
    assert verdict2 == crawl_queue.ADMIT, (
        "capacity freed by a completed crawl was not released to the queue")

    # …and the queued crawl is claimable and drains.
    plan = await queue_store.plan_drain(free_slots=5)
    assert any(r["exploration_id"] == queued for r in plan), (
        "the queued crawl was not planned for drain once capacity freed")
    assert await queue_store.claim(tenant_id=tenant, exploration_id=queued) is True
    assert await _status_of(tenant, queued) == crawl_queue.STATUS_CLAIMED


async def test_a_claim_is_exactly_once():
    """Two drainers cannot both take the same crawl (SKIP LOCKED + status guard)."""
    tenant = "tfl01_claim_" + uuid.uuid4().hex[:8]
    await _register_tenant(tenant)
    eid = await _mint(tenant, "app1", status="pending")
    await queue_store.enqueue(tenant_id=tenant, exploration_id=eid, reason="test")

    first = await queue_store.claim(tenant_id=tenant, exploration_id=eid)
    second = await queue_store.claim(tenant_id=tenant, exploration_id=eid)
    assert first is True and second is False, (
        "the same queued crawl was claimed twice — duplicate work would run")


async def test_requeue_returns_a_failed_dispatch_to_the_queue():
    """A claimed crawl that could not be dispatched must not evaporate."""
    tenant = "tfl01_rq_" + uuid.uuid4().hex[:8]
    await _register_tenant(tenant)
    eid = await _mint(tenant, "app1", status="pending")
    await queue_store.enqueue(tenant_id=tenant, exploration_id=eid, reason="test")
    await queue_store.claim(tenant_id=tenant, exploration_id=eid)

    assert await queue_store.requeue(
        tenant_id=tenant, exploration_id=eid, reason="worker died") is True
    assert await _status_of(tenant, eid) == crawl_queue.STATUS_QUEUED, (
        "a failed dispatch lost the crawl instead of requeueing it")


async def test_enqueue_is_status_guarded():
    """A crawl that already left `pending` is never dragged back into the queue."""
    tenant = "tfl01_guard_" + uuid.uuid4().hex[:8]
    await _register_tenant(tenant)
    eid = await _mint(tenant, "app1", status="completed")
    await queue_store.enqueue(tenant_id=tenant, exploration_id=eid, reason="test")
    assert await _status_of(tenant, eid) == "completed", (
        "a completed crawl was resurrected into the queue")


# ══════════════════════════════════════════════════════════════════════════
# FAIRNESS — multiple tenants, no starvation
# ══════════════════════════════════════════════════════════════════════════

async def test_a_flooding_tenant_cannot_starve_another_tenant():
    """THE fairness proof, on real rows read through RLS.

    A tenant submits 20 crawls before another tenant submits 1. Under global
    FIFO the single crawl would be served 21st. The fair drain order must serve
    it within the first round.
    """
    run = uuid.uuid4().hex[:8]
    flooder = "tfl01_flood_" + run
    small = "tfl01_small_" + run
    for t in (flooder, small):
        await _register_tenant(t)

    flood_ids = []
    for i in range(20):
        eid = await _mint(flooder, "app_f", status="pending", created_offset_s=i)
        await queue_store.enqueue(tenant_id=flooder, exploration_id=eid,
                                  reason="cap")
        flood_ids.append(eid)
    # …submitted LAST, so FIFO would put it 21st.
    lone = await _mint(small, "app_s", status="pending", created_offset_s=100)
    await queue_store.enqueue(tenant_id=small, exploration_id=lone, reason="cap")

    # Enough slots for a full first round across every tenant that currently
    # has queued work (the queue is fleet-wide, so other tenants may be present;
    # the invariant below is stated relationally so it does not depend on how
    # many).
    ordered = await queue_store.plan_drain(free_slots=queue_store.drain_batch())
    ids = [r["exploration_id"] for r in ordered]
    assert lone in ids, (
        "STARVATION: the flooding tenant's 20 crawls monopolised the drain and "
        "the other tenant's single crawl was not served in the first round")

    # THE INVARIANT: round-robin means the lone crawl is served before the
    # flooding tenant's SECOND crawl. Under global FIFO it would come 21st.
    flood_positions = [ids.index(e) for e in flood_ids if e in ids]
    assert len(flood_positions) >= 2, "the flooder did not get enough slots to test"
    assert ids.index(lone) < sorted(flood_positions)[1], (
        "STARVATION: the lone crawl was served after the flooding tenant's "
        "second crawl — the drain order is not round-robin across tenants")
    # And it beats the flooder's 20th outright, which is the FIFO comparison.
    assert ids.index(lone) < max(flood_positions), (
        "the lone crawl lost to the flooder's backlog")


async def test_drain_is_fifo_within_one_tenant():
    """Fairness across tenants must not scramble order WITHIN a tenant."""
    tenant = "tfl01_fifo_" + uuid.uuid4().hex[:8]
    await _register_tenant(tenant)
    ids = []
    for i in range(3):
        eid = await _mint(tenant, "app1", status="pending", created_offset_s=i)
        await queue_store.enqueue(tenant_id=tenant, exploration_id=eid, reason="cap")
        ids.append(eid)
    ordered = [r["exploration_id"] for r in await queue_store.plan_drain(free_slots=10)
               if r["tenant_id"] == tenant]
    assert ordered == ids, "within one tenant the queue is not FIFO"


async def test_drain_never_exceeds_free_capacity():
    """The queue must not simply relocate over-subscription one layer down."""
    tenant = "tfl01_cap_" + uuid.uuid4().hex[:8]
    await _register_tenant(tenant)
    for i in range(6):
        eid = await _mint(tenant, "app1", status="pending", created_offset_s=i)
        await queue_store.enqueue(tenant_id=tenant, exploration_id=eid, reason="cap")
    assert len(await queue_store.plan_drain(free_slots=2)) == 2
    assert await queue_store.plan_drain(free_slots=0) == [], (
        "a drain was planned with zero free slots")


async def test_queued_crawls_are_tenant_isolated():
    """One tenant's queue read can never return another tenant's crawl."""
    run = uuid.uuid4().hex[:8]
    a, b = "tfl01_iso_a_" + run, "tfl01_iso_b_" + run
    for t in (a, b):
        await _register_tenant(t)
    eid_a = await _mint(a, "app_a", status="pending")
    eid_b = await _mint(b, "app_b", status="pending")
    await queue_store.enqueue(tenant_id=a, exploration_id=eid_a, reason="cap")
    await queue_store.enqueue(tenant_id=b, exploration_id=eid_b, reason="cap")

    a_rows = {r["exploration_id"] for r in await queue_store.read_queued_for_tenant(a)}
    assert eid_a in a_rows, "tenant A cannot see its OWN queued crawl"
    assert eid_b not in a_rows, (
        "TENANT LEAK: tenant A's queue read returned tenant B's crawl")

    # …and A cannot CLAIM B's crawl even naming it exactly.
    assert await queue_store.claim(tenant_id=a, exploration_id=eid_b) is False, (
        "TENANT LEAK: tenant A claimed tenant B's queued crawl")
    assert await _status_of(b, eid_b) == crawl_queue.STATUS_QUEUED, (
        "tenant B's crawl was mutated by a claim scoped to tenant A")


# ══════════════════════════════════════════════════════════════════════════
# THE CAPACITY-vs-ERROR DISTINCTION
# ══════════════════════════════════════════════════════════════════════════

async def test_only_capacity_failures_are_queued():
    """A deterministic error must still fail fast, not wait out a queue timeout."""
    assert queue_store.queue_verdict_is_capacity(409) is True   # busy
    assert queue_store.queue_verdict_is_capacity(502) is True   # unreachable
    for code in (400, 401, 403, 404, 422, 500, 503):
        assert queue_store.queue_verdict_is_capacity(code) is False, (
            "status " + str(code) + " would be QUEUED — a deterministic error "
            "must fail immediately, not become an hour of silence followed by a "
            "timeout naming the wrong cause")


async def test_no_configured_cap_admits_exactly_as_before(monkeypatch):
    """An un-provisioned tenant must behave byte-identically to pre-M3.3."""
    tenant = "tfl01_nocap_" + uuid.uuid4().hex[:8]
    await _register_tenant(tenant)
    monkeypatch.delenv(queue_store.ENV_TENANT_CAP, raising=False)
    monkeypatch.delenv(queue_store.ENV_HOST_CAP, raising=False)
    for i in range(5):
        await _mint(tenant, "app1", status="running")
    verdict, _ = await queue_store.admission_verdict(
        tenant_id=tenant, host="app.example")
    assert verdict == crawl_queue.ADMIT, (
        "an un-provisioned tenant was queued — the queue must never fail-close "
        "a tenant nobody configured")


async def test_per_host_cap_counts_only_that_host(monkeypatch):
    tenant = "tfl01_host_" + uuid.uuid4().hex[:8]
    await _register_tenant(tenant)
    monkeypatch.setenv(queue_store.ENV_HOST_CAP, "2")
    monkeypatch.delenv(queue_store.ENV_TENANT_CAP, raising=False)
    for _ in range(2):
        await _mint(tenant, "app1", status="running", host="busy.example")

    over, reason = await queue_store.admission_verdict(
        tenant_id=tenant, host="busy.example")
    assert over == crawl_queue.QUEUE and reason == "per_host_concurrency_cap"

    other, _ = await queue_store.admission_verdict(
        tenant_id=tenant, host="idle.example")
    assert other == crawl_queue.ADMIT, (
        "a busy host throttled an unrelated host — the cap is not per-host")
