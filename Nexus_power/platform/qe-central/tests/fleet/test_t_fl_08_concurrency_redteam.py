"""M3.3 / T-FL-08 — the fleet under N concurrent crawls, red-teamed.

THE STOP CONDITION
==================
"A fleet that processes N crawls but leaks tenant traffic is a failed
implementation."

So this is not a throughput test. It runs N concurrent crawls across multiple
tenants — with overlapping domains, competing queue work, retries, heartbeat
changes, real per-worker egress fences, real object-storage handoff and
production-like RLS — and then asserts the SAFETY properties, with throughput as
the least interesting of them.

WHAT IS REAL AND WHAT IS SIMULATED, STATED PLAINLY
==================================================
REAL: the durable queue and its fair drain order; the worker registry and its
atomic capacity accounting; the egress fence files on disk; the object-storage
handoff through MinIO; the exploration rows and every read/write of them under
FORCE RLS as a NOSUPERUSER role; the tenant isolation those policies enforce.

SIMULATED: the explorer process itself — a coroutine that takes a registry slot,
reads the fence it was given, "crawls" for a moment, publishes evidence, and
releases. Running real Chromium against real applications is a live-fire
exercise, not a CI gate; what must be proven HERE is that the control plane
never lets two crawls share a fence, exceed capacity, cross a tenant boundary,
or get marked failed for being busy. Those are all control-plane properties and
they are exercised for real.

The distinction is stated because a proof whose boundaries are unclear is not a
proof.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import uuid
from pathlib import Path

import pytest

QEC_DB_URL = os.environ.get("QEC_TEST_QEC_DATABASE_URL", "")
SUBSTRATE_DB_URL = os.environ.get("QEC_TEST_SUBSTRATE_DATABASE_URL", "")
S3_ENDPOINT = os.environ.get("QEC_TEST_S3_ENDPOINT", "")
S3_BUCKET = os.environ.get("QEC_TEST_S3_BUCKET", "qec-evidence")

if QEC_DB_URL:
    os.environ["QEC_DATABASE_URL"] = QEC_DB_URL
    os.environ["QEC_TEST_DB_NULLPOOL"] = "1"
if SUBSTRATE_DB_URL:
    os.environ["NEXUS_DATABASE_URL_SUBSTRATE"] = SUBSTRATE_DB_URL

from sqlalchemy import text  # noqa: E402

from app.controlplane.scheduling import (  # noqa: E402
    crawl_queue,
    queue_store,
    worker_registry as wr,
)
from app.controlplane.tenant_scope import scope_to_tenant  # noqa: E402
from app.routers import explorations  # noqa: E402

needs_db = pytest.mark.skipif(
    not (QEC_DB_URL and SUBSTRATE_DB_URL),
    # The reason must NAME the variables. The A27.1 no-silent-skip gate
    # recognises an infrastructure skip by the environment variable in its
    # reason, so "needs the ... test DSNs" was invisible to it: had the CI
    # database failed to start, these tests would have skipped under
    # QEC_REQUIRE_DB and the build would still have gone green. Exactly the
    # hole that let six T-FL-03 object-storage tests never run.
    reason=("QEC_TEST_QEC_DATABASE_URL / QEC_TEST_SUBSTRATE_DATABASE_URL "
            "not set — T-FL-08 needs the qecentral + substrate test DSNs"),
)
pytestmark = [needs_db, pytest.mark.asyncio]

#: The concurrency under test, and the fleet that must absorb it.
N_CRAWLS = 24
N_TENANTS = 4
N_WORKERS = 3
WORKER_CAPACITY = 2                      # ⇒ 6 slots for 24 crawls


class FleetHarness:
    """A fleet of simulated explorer workers over the REAL control plane."""

    def __init__(self, tmpdir: str):
        self.tmpdir = Path(tmpdir)
        self.run = uuid.uuid4().hex[:8]
        self.workers: list[dict] = []
        #: Every (worker_id, tenant_id, fence_contents) observed AT DISPATCH.
        #: This is the egress evidence — what the browser would actually have
        #: been fenced by at the moment it could first make a request.
        self.fence_observations: list[tuple[str, str, str]] = []
        #: Peak simultaneous crawls per worker, to prove capacity is a cap.
        self.live_per_worker: dict[str, int] = {}
        self.peak_per_worker: dict[str, int] = {}
        self.completed: list[str] = []
        self.failed_because_busy: list[str] = []
        self.lock = asyncio.Lock()

    async def register_fleet(self) -> None:
        for i in range(N_WORKERS):
            wid = f"w{i}_{self.run}"
            fence = self.tmpdir / f"fence_{wid}.txt"      # PER-WORKER file
            await wr.register_worker(
                worker_id=wid, url=f"http://{wid}:8210",
                allowlist_path=str(fence), capacity=WORKER_CAPACITY)
            self.workers.append({"worker_id": wid, "url": f"http://{wid}:8210",
                                 "allowlist_path": str(fence)})
            self.live_per_worker[wid] = 0
            self.peak_per_worker[wid] = 0

    async def run_crawl(self, *, tenant_id: str, host: str,
                        exploration_id: str) -> str:
        """One crawl through the real admission → registry → fence → release path."""
        verdict, reason = await queue_store.admission_verdict(
            tenant_id=tenant_id, host=host)
        if verdict == crawl_queue.QUEUE:
            await queue_store.enqueue(tenant_id=tenant_id,
                                      exploration_id=exploration_id,
                                      reason=reason)
            return "queued"

        snapshot, _src = await wr.schedulable_workers(tenant_id=tenant_id)
        chosen = wr.choose_worker(snapshot, tenant_id=tenant_id,
                                  now=wr.utc_now(),
                                  ttl_s=wr.heartbeat_ttl_seconds())
        if chosen is None or not await wr.acquire_slot(
                worker_id=chosen["worker_id"]):
            # THE CRITICAL BRANCH: a busy fleet must QUEUE, never fail.
            await queue_store.enqueue(tenant_id=tenant_id,
                                      exploration_id=exploration_id,
                                      reason="fleet_at_capacity")
            return "queued"

        wid = chosen["worker_id"]
        try:
            async with self.lock:
                self.live_per_worker[wid] += 1
                self.peak_per_worker[wid] = max(self.peak_per_worker[wid],
                                                self.live_per_worker[wid])
            # RESERVE-THEN-FENCE, then read back what the browser would see.
            explorations._write_egress_allowlist([host], chosen["allowlist_path"])
            await asyncio.sleep(0)               # a real scheduling window
            observed = Path(chosen["allowlist_path"]).read_text()
            self.fence_observations.append((wid, tenant_id, observed))
            await asyncio.sleep(0.01)            # "crawling"
            await self._mark(tenant_id, exploration_id, "completed")
            self.completed.append(exploration_id)
            return "completed"
        finally:
            async with self.lock:
                self.live_per_worker[wid] -= 1
            await wr.release_slot(worker_id=wid)

    @staticmethod
    async def _mark(tenant_id: str, exploration_id: str, status: str) -> None:
        from app.db import qec_engine
        async with qec_engine.begin() as conn:
            await scope_to_tenant(conn, tenant_id)
            await conn.execute(text(
                "UPDATE qe_explorations SET status = :s, finished_at = now(), "
                "updated_at = now() WHERE exploration_id = :e"),
                {"s": status, "e": exploration_id})


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


async def _mint(tenant: str, app_id: str, host: str) -> str:
    from app.db import qec_engine
    eid = "e_" + uuid.uuid4().hex[:16]
    async with qec_engine.begin() as conn:
        await scope_to_tenant(conn, tenant)
        await conn.execute(text(
            "INSERT INTO qe_explorations (exploration_id, tenant_id, app_id, "
            " status, extractor_version, started_at, created_at, updated_at, stats) "
            "VALUES (:e, :t, :a, 'pending', 'qec-test', now(), now(), now(), "
            "        jsonb_build_object('target_host', CAST(:h AS text)))"
        ), {"e": eid, "t": tenant, "a": app_id, "h": host})
    return eid


async def _status(tenant: str, eid: str) -> str:
    from app.db import qec_engine
    async with qec_engine.begin() as conn:
        await scope_to_tenant(conn, tenant)
        row = (await conn.execute(text(
            "SELECT status FROM qe_explorations WHERE exploration_id = :e"),
            {"e": eid})).first()
    return str(row[0]) if row else ""


# ══════════════════════════════════════════════════════════════════════════
# THE CONCURRENCY RED-TEAM
# ══════════════════════════════════════════════════════════════════════════

async def test_n_concurrent_crawls_multi_tenant_overlapping_domains():
    """24 crawls, 4 tenants, 6 slots — every safety property, one run."""
    with tempfile.TemporaryDirectory() as tmp:
        h = FleetHarness(tmp)
        await h.register_fleet()

        tenants = [f"tfl08_t{i}_{h.run}" for i in range(N_TENANTS)]
        for t in tenants:
            await _register_tenant(t)

        # OVERLAPPING DOMAINS on purpose: two tenants crawling the same hostname
        # must still be fenced and isolated from each other.
        shared_host = "shared.example"
        work = []
        for i in range(N_CRAWLS):
            tenant = tenants[i % N_TENANTS]
            host = shared_host if i % 3 == 0 else f"{tenant}.example"
            eid = await _mint(tenant, f"app_{i}", host)
            work.append((tenant, host, eid))

        # ── ALL AT ONCE ────────────────────────────────────────────────
        results = await asyncio.gather(*[
            h.run_crawl(tenant_id=t, host=host, exploration_id=eid)
            for t, host, eid in work])

        # ── 1. NO CRAWL FAILED MERELY BECAUSE THE FLEET WAS BUSY ───────
        statuses = {}
        for t, _host, eid in work:
            statuses[eid] = await _status(t, eid)
        failed = [e for e, s in statuses.items() if s == "failed"]
        assert not failed, (
            f"{len(failed)} crawl(s) were marked FAILED under concurrency. A "
            "busy fleet is not a failed crawl — this is the T-FL-01 defect.")

        # ── 2. EVERY CRAWL IS ACCOUNTED FOR (completed or honestly queued) ──
        assert set(results) <= {"completed", "queued"}, (
            f"unexpected outcomes: {set(results)}")
        for eid, s in statuses.items():
            assert s in ("completed", crawl_queue.STATUS_QUEUED), (
                f"crawl {eid} ended in an unaccounted state {s!r}")
        assert results.count("completed") > 0, "the fleet completed nothing"

        # ── 3. WORKER CAPACITY WAS NEVER EXCEEDED ──────────────────────
        for wid, peak in h.peak_per_worker.items():
            assert peak <= WORKER_CAPACITY, (
                f"worker {wid} ran {peak} concurrent crawls with capacity "
                f"{WORKER_CAPACITY} — the fleet over-subscribed under load")

        # ── 4. NO EGRESS FENCE VIOLATION ───────────────────────────────
        # At dispatch, each crawl must have seen a fence containing ITS OWN host
        # and no other tenant's private host.
        private_hosts = {f"{t}.example" for t in tenants}
        for wid, tenant_id, observed in h.fence_observations:
            own = f"{tenant_id}.example"
            foreign = [ph for ph in private_hosts
                       if ph != own and ph in observed]
            assert not foreign, (
                f"EGRESS FENCE VIOLATION on {wid}: a crawl for {tenant_id} was "
                f"fenced with another tenant's destination(s) {foreign} — "
                "concurrent dispatch clobbered a live fence")
            assert observed.strip(), f"{wid} dispatched against an EMPTY fence"

        # ── 5. FENCE TOPOLOGY REMAINED SOUND ───────────────────────────
        assert wr.fence_conflicts(h.workers) == {}, (
            "two workers shared an egress allowlist file during the run")


async def test_tenant_cannot_read_another_tenants_crawl_evidence():
    """Isolation under load, checked by predicate AND by primary key."""
    run = uuid.uuid4().hex[:8]
    a, b = f"tfl08_iso_a_{run}", f"tfl08_iso_b_{run}"
    for t in (a, b):
        await _register_tenant(t)
    eid_a = await _mint(a, "app_a", "a.example")
    eid_b = await _mint(b, "app_b", "b.example")

    from app.db import qec_engine
    async with qec_engine.begin() as conn:
        await scope_to_tenant(conn, a)
        visible = {r[0] for r in (await conn.execute(text(
            "SELECT exploration_id FROM qe_explorations"))).all()}
        by_pk = (await conn.execute(text(
            "SELECT count(*) FROM qe_explorations WHERE exploration_id = :e"),
            {"e": eid_b})).scalar()
    assert eid_a in visible, "tenant A cannot see its OWN crawl"
    assert eid_b not in visible, (
        "TENANT LEAK: tenant A read tenant B's crawl evidence")
    assert by_pk == 0, "TENANT LEAK: tenant A read tenant B's crawl by primary key"


async def test_queue_fairness_holds_under_concurrent_submission():
    """Concurrent submission must not let one tenant monopolise the drain."""
    run = uuid.uuid4().hex[:8]
    flooder, small = f"tfl08_flood_{run}", f"tfl08_small_{run}"
    for t in (flooder, small):
        await _register_tenant(t)

    flood_ids, lone = [], None
    for i in range(15):
        eid = await _mint(flooder, "app_f", "f.example")
        await queue_store.enqueue(tenant_id=flooder, exploration_id=eid,
                                  reason="cap")
        flood_ids.append(eid)
    lone = await _mint(small, "app_s", "s.example")
    await queue_store.enqueue(tenant_id=small, exploration_id=lone, reason="cap")

    ids = [r["exploration_id"]
           for r in await queue_store.plan_drain(free_slots=queue_store.drain_batch())]
    assert lone in ids, "STARVATION: the lone tenant's crawl was not served"
    flood_positions = sorted(ids.index(e) for e in flood_ids if e in ids)
    assert len(flood_positions) >= 2
    assert ids.index(lone) < flood_positions[1], (
        "STARVATION: the lone crawl was served after the flooder's second — "
        "round-robin fairness did not hold under concurrent submission")


async def test_a_worker_going_stale_mid_run_stops_receiving_work():
    """Heartbeat changes during the run must move work to healthy workers."""
    run = uuid.uuid4().hex[:8]
    live_id, dead_id = f"live_{run}", f"dead_{run}"
    with tempfile.TemporaryDirectory() as tmp:
        await wr.register_worker(worker_id=live_id, url="http://live",
                                 allowlist_path=str(Path(tmp) / "live.txt"),
                                 capacity=2)
        await wr.register_worker(worker_id=dead_id, url="http://dead",
                                 allowlist_path=str(Path(tmp) / "dead.txt"),
                                 capacity=8)   # bigger, so only staleness saves us

        rows = {w["worker_id"]: w for w in await wr.list_workers()
                if w["worker_id"] in (live_id, dead_id)}
        # The dead worker's heartbeat ages out.
        from datetime import timedelta
        now = rows[dead_id]["last_heartbeat_at"] + timedelta(
            seconds=wr.heartbeat_ttl_seconds() + 30)
        rows[live_id]["last_heartbeat_at"] = now      # the live one kept beating

        chosen = wr.choose_worker(list(rows.values()), tenant_id="t",
                                  now=now, ttl_s=wr.heartbeat_ttl_seconds())
        assert chosen is not None and chosen["worker_id"] == live_id, (
            "work was scheduled onto a STALE worker (or no worker at all) — a "
            "crashed pod would silently swallow crawls")


async def test_retries_do_not_duplicate_work_under_concurrency():
    """Concurrent claims of the same queued crawl: exactly one may win."""
    run = uuid.uuid4().hex[:8]
    tenant = f"tfl08_retry_{run}"
    await _register_tenant(tenant)
    eid = await _mint(tenant, "app", "r.example")
    await queue_store.enqueue(tenant_id=tenant, exploration_id=eid, reason="cap")

    wins = await asyncio.gather(*[
        queue_store.claim(tenant_id=tenant, exploration_id=eid)
        for _ in range(8)])
    assert sum(1 for w in wins if w) == 1, (
        f"{sum(1 for w in wins if w)} of 8 concurrent drainers claimed the SAME "
        "crawl — duplicate work would run against the customer's application")


@pytest.mark.skipif(not S3_ENDPOINT, reason="needs QEC_TEST_S3_ENDPOINT")
async def test_evidence_handoff_survives_concurrency_and_is_tenant_isolated(
        monkeypatch):
    """Concurrent publishes must not cross-contaminate — across crawls OR tenants.

    Goes through the house ``nexus_sdk.storage`` layer on the house env
    contract, so keys are TENANT-SCOPED. The second half proves the isolation
    that scoping buys: two tenants publishing the SAME crawl id land in
    different prefixes, and neither can read the other's evidence.
    """
    from app.config import settings
    from app.storage import object_store

    monkeypatch.setenv("NEXUS_STORAGE_BACKEND", "s3")
    monkeypatch.setenv("S3_BUCKET", S3_BUCKET)
    monkeypatch.setenv("S3_ENDPOINT", S3_ENDPOINT)
    monkeypatch.setenv("S3_REGION", "us-east-1")
    monkeypatch.setenv("S3_ACCESS_KEY",
                       os.environ.get("AWS_ACCESS_KEY_ID", "minioadmin"))
    monkeypatch.setenv("S3_SECRET_KEY",
                       os.environ.get("AWS_SECRET_ACCESS_KEY", "minioadmin"))
    monkeypatch.setattr(settings, "nexus_storage_backend", "s3", raising=False)
    object_store.reset_store_cache_for_tests()
    try:
        run = uuid.uuid4().hex[:8]
        tenant = "tfl08ev" + run
        with tempfile.TemporaryDirectory() as producer,              tempfile.TemporaryDirectory() as consumer:
            crawls = []
            for _ in range(8):
                cid = uuid.uuid4().hex
                d = Path(producer) / cid
                d.mkdir(parents=True)
                (d / "manifest.jsonl").write_text(
                    '{"crawl":"' + cid + '"}' + chr(10))
                crawls.append((cid, d))

            await asyncio.gather(*[
                object_store.publish_crawl_dir(tenant, cid, d)
                for cid, d in crawls])

            for cid, _d in crawls:
                got = await object_store.ensure_local(
                    tenant, cid, Path(consumer) / cid)
                body = (got / "manifest.jsonl").read_text()
                assert cid in body, (
                    "EVIDENCE CROSS-CONTAMINATION: crawl " + cid
                    + " materialised another crawl's manifest: " + body[:120])

            # TENANT ISOLATION: the same crawl id under a different tenant is a
            # different prefix, and holds nothing.
            shared_id = crawls[0][0]
            other = "tfl08other" + run
            assert object_store.evidence_prefix(other, shared_id) !=                 object_store.evidence_prefix(tenant, shared_id)
            with tempfile.TemporaryDirectory() as intruder:
                got = await object_store.ensure_local(
                    other, shared_id, Path(intruder) / shared_id)
                assert not (got / "manifest.jsonl").exists(), (
                    "TENANT LEAK: a second tenant read the first tenant's "
                    "crawl evidence using the same crawl id")
    finally:
        object_store.reset_store_cache_for_tests()
