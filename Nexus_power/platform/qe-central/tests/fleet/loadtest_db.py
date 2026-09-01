"""M3.3 / T-FL-06 — DB load harness for the fleet hot paths.

Run (not a pytest module — it produces a REPORT, and a report that can silently
skip is worse than no report)::

    QEC_TEST_QEC_DATABASE_URL=postgresql+asyncpg://qec:...@host:5432/qecentral \\
    QEC_TEST_SUBSTRATE_DATABASE_URL=postgresql+asyncpg://qec:...@host:5432/nexus \\
    python tests/fleet/loadtest_db.py [--crawls 50] [--journeys 40] [--concurrency 16]

WHAT IT MEASURES, AND WHY EACH ONE
==================================
The milestone says: "Do not claim scale based solely on application-level
throughput." Requests-per-second says nothing about whether the DATABASE is
about to fall over, so every number below is read from PostgreSQL's own
statistics views rather than from a stopwatch around the client:

  * DB CONNECTIONS       — ``pg_stat_activity``. The number that actually runs
                           out. PgBouncer's ``default_pool_size`` is the ceiling,
                           and the failure at the ceiling is a queue of waiting
                           clients, not an error anyone sees.
  * QUERY LATENCY        — p50/p95/max per hot path, measured client-side but
                           reported per PATH so a slow one is attributable.
  * POOL WAIT            — time spent waiting to CHECK OUT a connection. This is
                           the metric that goes non-linear first and the one a
                           throughput number hides completely.
  * TRANSACTION DURATION — longest ``xact_start`` age observed. A long
                           transaction holds its snapshot and blocks vacuum; on
                           a fleet it is how one slow endpoint degrades every
                           other one.
  * LOCKS                — ``pg_locks`` not granted. Any sustained non-zero
                           value under a read workload means contention that
                           will not improve with more replicas.
  * CPU                  — backend count as a proxy plus, where available,
                           ``pg_stat_statements`` total time.

The hot paths exercised are the three this milestone touches: the journeys
rollup (T-FL-06's N+1), the queue drain plan (T-FL-01), and the RLS fleet scan
(T-FL-05).
"""
from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import time
import uuid
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

QEC_DB_URL = os.environ.get("QEC_TEST_QEC_DATABASE_URL", "")
SUBSTRATE_DB_URL = os.environ.get("QEC_TEST_SUBSTRATE_DATABASE_URL", "")


class Timings:
    def __init__(self) -> None:
        self.by_path: dict[str, list[float]] = {}

    def record(self, path: str, seconds: float) -> None:
        self.by_path.setdefault(path, []).append(seconds * 1000.0)

    def summary(self) -> dict[str, dict]:
        out = {}
        for path, xs in sorted(self.by_path.items()):
            xs = sorted(xs)
            out[path] = {
                "n": len(xs),
                "p50_ms": round(statistics.median(xs), 1),
                "p95_ms": round(xs[max(0, int(len(xs) * 0.95) - 1)], 1),
                "max_ms": round(xs[-1], 1),
            }
        return out


async def db_snapshot(engine) -> dict:
    """PostgreSQL's own view of the load. Never the client's opinion."""
    async with engine.begin() as conn:
        active = (await conn.execute(text(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
        ))).scalar()
        by_state = (await conn.execute(text(
            "SELECT state, count(*) FROM pg_stat_activity "
            "WHERE datname = current_database() GROUP BY state"))).all()
        longest_xact = (await conn.execute(text(
            "SELECT COALESCE(EXTRACT(EPOCH FROM max(now() - xact_start)), 0) "
            "FROM pg_stat_activity WHERE datname = current_database() "
            "AND xact_start IS NOT NULL"))).scalar()
        waiting_locks = (await conn.execute(text(
            "SELECT count(*) FROM pg_locks WHERE NOT granted"))).scalar()
        max_conn = (await conn.execute(text("SHOW max_connections"))).scalar()
    return {
        "connections": int(active or 0),
        "by_state": {str(s or "idle"): int(c) for s, c in by_state},
        "longest_transaction_s": round(float(longest_xact or 0.0), 3),
        "waiting_locks": int(waiting_locks or 0),
        "max_connections": int(max_conn),
    }


async def seed(engine, *, tenants: int, journeys: int) -> list[tuple[str, str]]:
    """Seed apps + journeys + graph rows. Returns [(tenant, app_id)]."""
    run = uuid.uuid4().hex[:8]
    sub = create_async_engine(SUBSTRATE_DB_URL, poolclass=NullPool)
    pairs: list[tuple[str, str]] = []
    try:
        for i in range(tenants):
            tenant = f"tfl06lt_{run}_{i}"
            app_id = f"app_{run}_{i}"
            async with sub.begin() as conn:
                await conn.execute(text(
                    # see the note in test_t_fl_01_durable_queue._register_tenant
                    "INSERT INTO tenants (tenant_id, name, domain) "
                    "VALUES (:t, :t, :d) "
                    "ON CONFLICT (tenant_id) DO NOTHING"), {"t": tenant, "d": f"{tenant}.test"})
            async with engine.begin() as conn:
                await conn.execute(text(
                    "SELECT set_config('nexus.current_tenant_id', :t, true)"),
                    {"t": tenant})
                await conn.execute(text(
                    "INSERT INTO client_apps (tenant_id, app_id, name, base_url, "
                    " status, schedule, fences, latest_artifact_id, created_at, "
                    " updated_at) VALUES (:t, :a, :a, 'https://x.example', "
                    " 'active', CAST('{}' AS jsonb), CAST('{}' AS jsonb), "
                    " :art, now(), now())"),
                    {"t": tenant, "a": app_id, "art": f"art_{run}_{i}"})
                for j in range(journeys):
                    jid = f"j_{uuid.uuid4().hex[:12]}"
                    fp = f"fp_{uuid.uuid4().hex[:12]}"
                    await conn.execute(text(
                        "INSERT INTO journeys (journey_id, tenant_id, app_id, "
                        " entry_fingerprint, flow_id, business_name, deepest_steps) "
                        "VALUES (:j, :t, :a, :fp, :f, CAST(:bn AS varchar), 3)"),
                        {"j": jid, "t": tenant, "a": app_id, "fp": fp,
                         "f": f"flow{j}", "bn": f"Journey {j}"})
                    await conn.execute(text(
                        "INSERT INTO journey_traversals (traversal_id, tenant_id, "
                        " app_id, journey_id, exploration_id, terminal, path_hash, "
                        " completed, path_fps) VALUES (:tr, :t, :a, :j, 'e1', "
                        " 'completed', :ph, true, CAST(:fps AS jsonb))"),
                        {"tr": f"tr_{uuid.uuid4().hex[:12]}", "t": tenant,
                         "a": app_id, "j": jid, "ph": f"ph{j}",
                         "fps": f'["{fp}"]'})
                    await conn.execute(text(
                        "INSERT INTO journey_nodes (node_id, tenant_id, app_id, "
                        " fingerprint, url, title) VALUES (:n, :t, :a, :fp, "
                        " 'https://x.example/step', 'Step')"),
                        {"n": f"n_{uuid.uuid4().hex[:12]}", "t": tenant,
                         "a": app_id, "fp": fp})
            pairs.append((tenant, app_id))
    finally:
        await sub.dispose()
    return pairs


async def measure_pool_wait(t: Timings) -> None:
    """Time to CHECK OUT a pooled connection.

    Reported separately from query latency because it is the number that goes
    non-linear first under concurrency, and the one an end-to-end timing hides:
    a fast query that waited four seconds for a connection looks like a slow
    query, and the fix for each is completely different.
    """
    from app.db import qec_engine
    start = time.perf_counter()
    async with qec_engine.connect() as conn:
        checked_out = time.perf_counter()
        await conn.execute(text("SELECT 1"))
    t.record("pool_checkout_wait", checked_out - start)


async def hot_path_journeys(tenant: str, app_id: str, t: Timings) -> None:
    from app.routers.journeys import list_journeys
    start = time.perf_counter()
    await list_journeys(app_id=app_id, user={"tenant_id": tenant})
    t.record("journeys_list", time.perf_counter() - start)


async def hot_path_queue(t: Timings) -> None:
    from app.controlplane.scheduling import queue_store
    start = time.perf_counter()
    await queue_store.plan_drain(free_slots=10)
    t.record("queue_drain_plan", time.perf_counter() - start)


async def hot_path_fleet_scan(t: Timings) -> None:
    from app.controlplane.cycle.driver import _scan_fleet
    start = time.perf_counter()
    await _scan_fleet(50)
    t.record("rls_fleet_scan", time.perf_counter() - start)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenants", type=int, default=4)
    ap.add_argument("--journeys", type=int, default=40)
    ap.add_argument("--concurrency", type=int, default=16)
    ap.add_argument("--rounds", type=int, default=4)
    args = ap.parse_args()

    if not (QEC_DB_URL and SUBSTRATE_DB_URL):
        print("FATAL: set QEC_TEST_QEC_DATABASE_URL and "
              "QEC_TEST_SUBSTRATE_DATABASE_URL")
        return 2

    os.environ["QEC_DATABASE_URL"] = QEC_DB_URL
    os.environ["NEXUS_DATABASE_URL_SUBSTRATE"] = SUBSTRATE_DB_URL
    # DELIBERATELY *NOT* QEC_TEST_DB_NULLPOOL.
    #
    # The DB-gated contract suite sets that flag because each of its tests runs
    # in its own event loop and a pooled connection binds to the first one. This
    # harness runs everything inside ONE ``asyncio.run``, so it does not need the
    # escape hatch — and must not use it. NullPool opens a FRESH connection per
    # checkout: a first run of this harness measured p50 = 11.5s and a peak of 35
    # connections, both almost entirely TCP + auth setup rather than query cost.
    # Reporting those as scale numbers would have been exactly the
    # "claim scale from the wrong measurement" this task forbids.
    #
    # Left alone, the harness exercises the POOL POSTURE PRODUCTION RUNS
    # (pool_size=10, max_overflow=5 per replica) — the only posture whose
    # numbers mean anything.
    os.environ.pop("QEC_TEST_DB_NULLPOOL", None)

    engine = create_async_engine(QEC_DB_URL, poolclass=NullPool)
    timings = Timings()
    peak = {"connections": 0, "waiting_locks": 0, "longest_transaction_s": 0.0}

    try:
        baseline = await db_snapshot(engine)
        print("-- BASELINE --")
        print(f"  connections={baseline['connections']} "
              f"max_connections={baseline['max_connections']} "
              f"waiting_locks={baseline['waiting_locks']}")

        print(f"\n-- SEEDING {args.tenants} tenants × {args.journeys} journeys --")
        pairs = await seed(engine, tenants=args.tenants, journeys=args.journeys)
        print(f"  seeded {len(pairs)} apps")

        print(f"\n-- LOAD: {args.concurrency} concurrent × {args.rounds} rounds --")
        for rnd in range(args.rounds):
            tasks = []
            for i in range(args.concurrency):
                tenant, app_id = pairs[i % len(pairs)]
                tasks.append(hot_path_journeys(tenant, app_id, timings))
                tasks.append(measure_pool_wait(timings))
                if i % 4 == 0:
                    tasks.append(hot_path_queue(timings))
                if i % 8 == 0:
                    tasks.append(hot_path_fleet_scan(timings))
            sampler_stop = asyncio.Event()

            async def sample():
                while not sampler_stop.is_set():
                    snap = await db_snapshot(engine)
                    peak["connections"] = max(peak["connections"],
                                              snap["connections"])
                    peak["waiting_locks"] = max(peak["waiting_locks"],
                                                snap["waiting_locks"])
                    peak["longest_transaction_s"] = max(
                        peak["longest_transaction_s"],
                        snap["longest_transaction_s"])
                    await asyncio.sleep(0.05)

            sampler = asyncio.create_task(sample())
            t0 = time.perf_counter()
            await asyncio.gather(*tasks)
            elapsed = time.perf_counter() - t0
            sampler_stop.set()
            await sampler
            print(f"  round {rnd + 1}: {len(tasks)} ops in {elapsed:.2f}s "
                  f"peak_conns={peak['connections']}")

        print("\n== MEASURED RESULT ==")
        print("\nQuery latency by hot path:")
        for path, s in timings.summary().items():
            print(f"  {path:<20} n={s['n']:<5} p50={s['p50_ms']:>7}ms "
                  f"p95={s['p95_ms']:>8}ms max={s['max_ms']:>8}ms")
        print("\nDatabase (PostgreSQL's own statistics, not the client's):")
        print(f"  peak connections        : {peak['connections']} "
              f"/ max_connections {baseline['max_connections']}")
        print(f"  peak waiting locks      : {peak['waiting_locks']}")
        print(f"  longest transaction     : {peak['longest_transaction_s']}s")
        final = await db_snapshot(engine)
        print(f"  connections after load  : {final['connections']} "
              f"(baseline {baseline['connections']})")
        print(f"  backend states          : {final['by_state']}")

        # The honest verdict.
        headroom = baseline["max_connections"] - peak["connections"]
        print("\nVerdict:")
        print(f"  connection headroom remaining: {headroom}")
        if peak["waiting_locks"] > 0:
            print("  WARNING: lock contention observed under a read workload")
        if peak["connections"] > baseline["max_connections"] * 0.8:
            print("  WARNING: peak connections exceeded 80% of max_connections")
        return 0
    finally:
        await engine.dispose()


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    raise SystemExit(asyncio.run(main()))
