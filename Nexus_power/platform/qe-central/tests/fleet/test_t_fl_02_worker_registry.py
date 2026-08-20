"""M3.3 / T-FL-02 — the worker registry replaces static pool scheduling.

Proves the acceptance list end to end:

  * a worker JOINS and becomes schedulable;
  * HEARTBEAT updates liveness;
  * a worker becomes UNAVAILABLE (stops heartbeating);
  * a STALE worker stops receiving work;
  * work is REASSIGNED to a healthy worker;
  * CAPACITY is respected — the registry never hands out more slots than exist,
    including under concurrent acquisition;
  * scheduling prefers the LEAST-LOADED eligible worker;
  * tenant affinity is honoured in BOTH directions.

The pure-core tests run with no database. The store tests run against the
production-like DB and are gated on ``QEC_TEST_QEC_DATABASE_URL``.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import timedelta

import pytest

QEC_DB_URL = os.environ.get("QEC_TEST_QEC_DATABASE_URL", "")
if QEC_DB_URL:
    os.environ["QEC_DATABASE_URL"] = QEC_DB_URL
    os.environ["QEC_TEST_DB_NULLPOOL"] = "1"

from app.controlplane.scheduling import worker_registry as wr  # noqa: E402

NOW = wr.utc_now()
TTL = 90.0


def _w(wid, *, cap=1, inflight=0, status=wr.STATUS_ACTIVE, affinity="",
       age_s=0.0):
    return {
        "worker_id": wid, "url": "http://" + wid, "allowlist_path": "/a/" + wid,
        "capacity": cap, "in_flight": inflight, "status": status,
        "tenant_affinity": affinity,
        "last_heartbeat_at": NOW - timedelta(seconds=age_s),
    }


# ══════════════════════════════════════════════════════════════════════════
# PURE CORE — the scheduling decisions, no database
# ══════════════════════════════════════════════════════════════════════════

def test_fresh_worker_is_not_stale():
    assert wr.is_stale(_w("a", age_s=5), now=NOW, ttl_s=TTL) is False


def test_worker_past_ttl_is_stale():
    assert wr.is_stale(_w("a", age_s=TTL + 1), now=NOW, ttl_s=TTL) is True


def test_worker_with_no_heartbeat_is_stale():
    """Absence of evidence of life is not evidence of life."""
    assert wr.is_stale({"worker_id": "a"}, now=NOW, ttl_s=TTL) is True


def test_stale_worker_is_never_eligible():
    """THE T-FL-02 headline: a dead worker stops receiving work."""
    workers = [_w("dead", age_s=TTL + 10)]
    assert wr.eligible_workers(workers, tenant_id="t1", now=NOW, ttl_s=TTL) == []
    assert wr.choose_worker(workers, tenant_id="t1", now=NOW, ttl_s=TTL) is None


def test_work_is_reassigned_to_the_healthy_worker():
    """A stale worker and a live one: the live one is chosen, every time."""
    workers = [_w("dead", age_s=TTL + 10), _w("live", age_s=1)]
    chosen = wr.choose_worker(workers, tenant_id="t1", now=NOW, ttl_s=TTL)
    assert chosen is not None and chosen["worker_id"] == "live"


def test_full_worker_is_not_eligible():
    assert wr.choose_worker([_w("full", cap=2, inflight=2)],
                            tenant_id="t1", now=NOW, ttl_s=TTL) is None


def test_draining_and_disabled_workers_receive_no_work():
    for status in (wr.STATUS_DRAINING, wr.STATUS_DISABLED):
        assert wr.choose_worker([_w("w", status=status)],
                                tenant_id="t1", now=NOW, ttl_s=TTL) is None, status


def test_least_loaded_is_chosen_by_utilisation_not_raw_count():
    """A big worker at 2/8 beats a small one at 1/2.

    Ranking on the RAW in-flight count would pick the small worker (1 < 2) and
    pack small workers while large ones idle. Utilisation is the correct signal.
    """
    workers = [_w("small", cap=2, inflight=1), _w("big", cap=8, inflight=2)]
    chosen = wr.choose_worker(workers, tenant_id="t1", now=NOW, ttl_s=TTL)
    assert chosen["worker_id"] == "big", "did not rank on utilisation"


def test_choice_is_deterministic_across_replicas():
    """Two replicas deciding from the same snapshot make the SAME choice."""
    workers = [_w("b", cap=4), _w("a", cap=4), _w("c", cap=4)]
    picks = {wr.choose_worker(list(reversed(workers)), tenant_id="t",
                              now=NOW, ttl_s=TTL)["worker_id"]
             for _ in range(5)}
    assert picks == {"a"}, "tie-break is not deterministic"


def test_tenant_affinity_is_exclusive_in_both_directions():
    dedicated = _w("ded", affinity="tenant_a")
    shared = _w("shared")
    # tenant_a prefers its OWN dedicated hardware over shared capacity
    chosen = wr.choose_worker([shared, dedicated], tenant_id="tenant_a",
                              now=NOW, ttl_s=TTL)
    assert chosen["worker_id"] == "ded", "dedicated worker not preferred"
    # tenant_b must NEVER land on tenant_a's dedicated worker
    chosen_b = wr.choose_worker([dedicated], tenant_id="tenant_b",
                                now=NOW, ttl_s=TTL)
    assert chosen_b is None, (
        "TENANT LEAK: tenant B was scheduled onto a worker dedicated to tenant A")


def test_fleet_capacity_excludes_stale_and_draining():
    """Counting a dead worker would tell the queue there is room that is gone."""
    workers = [_w("live", cap=4, inflight=1),
               _w("dead", cap=8, age_s=TTL + 5),
               _w("draining", cap=8, status=wr.STATUS_DRAINING)]
    cap = wr.fleet_capacity(workers, now=NOW, ttl_s=TTL)
    assert cap == {"workers_alive": 1, "capacity": 4, "in_flight": 1, "free": 3}


def test_unavailability_is_explained_not_asserted():
    """Three different incidents must not produce one opaque string."""
    empty = wr.explain_unavailable([], tenant_id="t", now=NOW, ttl_s=TTL)
    assert "empty" in empty

    dead = wr.explain_unavailable([_w("a", age_s=TTL + 5)], tenant_id="t",
                                  now=NOW, ttl_s=TTL)
    assert "STALE" in dead

    busy = wr.explain_unavailable([_w("a", cap=2, inflight=2)], tenant_id="t",
                                  now=NOW, ttl_s=TTL)
    assert "at capacity" in busy and "2/2" in busy

    parked = wr.explain_unavailable([_w("a", status=wr.STATUS_DISABLED)],
                                    tenant_id="t", now=NOW, ttl_s=TTL)
    assert "non-active" in parked

    foreign = wr.explain_unavailable([_w("a", affinity="other")],
                                     tenant_id="t", now=NOW, ttl_s=TTL)
    assert "dedicated to a different tenant" in foreign


# ══════════════════════════════════════════════════════════════════════════
# STORE — against the production-like database
# ══════════════════════════════════════════════════════════════════════════

needs_db = pytest.mark.skipif(
    not QEC_DB_URL,
    reason="QEC_TEST_QEC_DATABASE_URL not set — the registry store proof needs a DB",
)


@needs_db
@pytest.mark.asyncio
async def test_worker_joins_heartbeats_and_goes_unavailable():
    """The full lifecycle, through the real table."""
    wid = "w_" + uuid.uuid4().hex[:10]
    await wr.register_worker(worker_id=wid, url="http://x:1",
                             allowlist_path="/a/1", capacity=3)

    rows = {w["worker_id"]: w for w in await wr.list_workers()}
    assert wid in rows, "worker did not join the registry"
    assert rows[wid]["capacity"] == 3 and rows[wid]["in_flight"] == 0
    assert rows[wid]["status"] == wr.STATUS_ACTIVE

    # It is schedulable while fresh.
    now = wr.utc_now()
    assert wr.choose_worker([rows[wid]], tenant_id="t", now=now, ttl_s=TTL)

    # A heartbeat carrying the worker's own in-flight count reconciles drift.
    assert await wr.heartbeat(worker_id=wid, in_flight=2) is True
    rows = {w["worker_id"]: w for w in await wr.list_workers()}
    assert rows[wid]["in_flight"] == 2

    # It stops heartbeating → judged stale against its OWN row timestamp.
    stale_now = rows[wid]["last_heartbeat_at"] + timedelta(seconds=TTL + 30)
    assert wr.is_stale(rows[wid], now=stale_now, ttl_s=TTL) is True
    assert wr.choose_worker([rows[wid]], tenant_id="t",
                            now=stale_now, ttl_s=TTL) is None, (
        "a stale worker was still offered work")


@needs_db
@pytest.mark.asyncio
async def test_heartbeat_from_unknown_worker_is_refused():
    """An unknown worker must RE-REGISTER, not be resurrected with defaults."""
    assert await wr.heartbeat(worker_id="never_registered_" + uuid.uuid4().hex[:8],
                              in_flight=0) is False


@needs_db
@pytest.mark.asyncio
async def test_reregistration_resets_in_flight():
    """A restarted pod is running nothing; carrying the count forward would
    permanently strand capacity that no crawl occupies."""
    wid = "w_" + uuid.uuid4().hex[:10]
    await wr.register_worker(worker_id=wid, url="http://x:1", capacity=4)
    await wr.heartbeat(worker_id=wid, in_flight=3)
    await wr.register_worker(worker_id=wid, url="http://x:1", capacity=4)
    rows = {w["worker_id"]: w for w in await wr.list_workers()}
    assert rows[wid]["in_flight"] == 0, "restart did not reset in_flight"


@needs_db
@pytest.mark.asyncio
async def test_capacity_is_respected_under_concurrent_acquisition():
    """THE over-subscription proof.

    Ten concurrent dispatches race for a worker with capacity 3. Exactly 3 may
    win. A read-then-write implementation has a window between the check and the
    write, and this is precisely where a fleet over-subscribes under load.
    """
    wid = "w_" + uuid.uuid4().hex[:10]
    await wr.register_worker(worker_id=wid, url="http://x:1", capacity=3)

    results = await asyncio.gather(*[wr.acquire_slot(worker_id=wid)
                                     for _ in range(10)])
    assert sum(1 for r in results if r) == 3, (
        "capacity was violated: " + str(sum(1 for r in results if r))
        + " of 10 concurrent acquisitions won a worker with capacity 3")

    rows = {w["worker_id"]: w for w in await wr.list_workers()}
    assert rows[wid]["in_flight"] == 3

    # Releasing frees exactly one slot, and never goes negative however many
    # times it is called (a failure path and a callback can both release).
    assert await wr.release_slot(worker_id=wid) is True
    for _ in range(6):
        await wr.release_slot(worker_id=wid)
    rows = {w["worker_id"]: w for w in await wr.list_workers()}
    assert rows[wid]["in_flight"] == 0, "double-release manufactured capacity"


@needs_db
@pytest.mark.asyncio
async def test_slot_cannot_be_acquired_on_a_disabled_worker():
    wid = "w_" + uuid.uuid4().hex[:10]
    await wr.register_worker(worker_id=wid, url="http://x:1", capacity=2)
    await wr.heartbeat(worker_id=wid, status=wr.STATUS_DISABLED)
    assert await wr.acquire_slot(worker_id=wid) is False


@needs_db
@pytest.mark.asyncio
async def test_registry_falls_back_to_static_pool_when_empty(monkeypatch):
    """An empty registry must reproduce today's behaviour, never zero capacity.

    Queueing every crawl on a fresh deploy — before any explorer had time to
    register — would be a worse failure than not having a registry at all.
    """
    async def _empty():
        return []
    monkeypatch.setattr(wr, "list_workers", _empty)
    workers, source = await wr.schedulable_workers(tenant_id="t")
    assert source == "static_pool"
    assert workers and workers[0]["url"], "static fallback produced no worker"


@needs_db
@pytest.mark.asyncio
async def test_registry_read_failure_falls_back_not_fails(monkeypatch):
    async def _boom():
        raise RuntimeError("database unreachable")
    monkeypatch.setattr(wr, "list_workers", _boom)
    workers, source = await wr.schedulable_workers(tenant_id="t")
    assert source == "static_pool" and workers, (
        "a registry read failure must degrade to the static pool, not to zero "
        "capacity — the latter would halt the whole fleet on a DB blip")


@needs_db
@pytest.mark.asyncio
async def test_stale_worker_row_is_reaped_after_retention(monkeypatch):
    wid = "w_" + uuid.uuid4().hex[:10]
    await wr.register_worker(worker_id=wid, url="http://x:1")
    monkeypatch.setenv(wr.ENV_WORKER_RETENTION, "0")
    await wr.reap_stale_workers(now=wr.utc_now() + timedelta(seconds=5))
    rows = {w["worker_id"] for w in await wr.list_workers()}
    assert wid not in rows, "a long-dead worker row was never reaped"
