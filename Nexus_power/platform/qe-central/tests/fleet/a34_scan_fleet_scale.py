"""A34 — how much does ``driver._scan_fleet`` actually cost per tenant?

WHY THIS EXISTS
===============
``_scan_fleet`` enumerates the tenant registry and then opens ONE transaction
per tenant, issuing THREE queries inside it (active cycles, unprocessed change
events, active apps).  That is ``O(tenants)`` transactions and ``O(3·tenants)``
round trips on the cycle daemon's discovery path, and it runs on every tick.

The bound is documented (M3_3_FLEET_CONCURRENCY_EVIDENCE.md §6.5) but it has
never been MEASURED.  "Documented" and "acceptable" are different claims, and
the Architecture Council cannot rule on the second one from the first: without a
number, "fine at current fleet size" is a belief.  This harness produces the
number, so the decision recorded in A34 is a decision about measured behaviour.

WHAT IS REAL HERE
=================
A real PostgreSQL server, the real ``client_apps`` / ``app_cycles`` /
``change_events`` tables built by the migration chain, the real
``_scan_fleet`` function imported from the driver (not a re-implementation),
and the production RLS posture: every statement runs as the ``qec`` role, which
is NOSUPERUSER / NOBYPASSRLS, so ``FORCE ROW LEVEL SECURITY`` is enforcing on
every read exactly as it is in production.

``SET ROLE`` is used rather than a separate login because the property under
test is the ROLE's RLS posture, not its password; the policies key off
``current_user``, which ``SET ROLE`` changes.  The harness asserts that the
posture actually took effect (``current_user = qec`` and BYPASSRLS off) before
recording a single timing, because a measurement taken as a superuser would be
measuring a query plan production never runs.

WHAT IS SIMULATED
=================
The FLEET SIZE.  Tenants and their apps are synthesised, because a 1000-tenant
production fleet does not exist to measure.  Every synthesised tenant is given
one active app with a non-empty ``latest_artifact_id`` so it is VISIBLE to the
scan — the worst case for row volume, and the case that actually costs.

Rows are written under a reserved ``a34-`` prefix and deleted afterwards, so
this can be run against a shared development database without disturbing it.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
import uuid
from pathlib import Path

_TESTS = Path(__file__).resolve().parents[1]
_ROOT = _TESTS.parent
for _p in (str(_ROOT), str(_ROOT.parent.parent / "sdk" / "nexus-sdk")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

QEC_DB_URL = os.environ.get("QEC_TEST_QEC_DATABASE_URL", "")
SUBSTRATE_DB_URL = os.environ.get("QEC_TEST_SUBSTRATE_DATABASE_URL", "")
if not (QEC_DB_URL and SUBSTRATE_DB_URL):
    print("FATAL: set QEC_TEST_QEC_DATABASE_URL and QEC_TEST_SUBSTRATE_DATABASE_URL",
          file=sys.stderr)
    raise SystemExit(2)

os.environ["QEC_DATABASE_URL"] = QEC_DB_URL
os.environ["NEXUS_DATABASE_URL_SUBSTRATE"] = SUBSTRATE_DB_URL
# Deliberately NOT NULLPOOL: production runs a pool, and a fresh TCP+auth
# handshake per checkout would dominate the number and make it meaningless.
os.environ.pop("QEC_TEST_DB_NULLPOOL", None)
os.environ.setdefault("NEXUS_ENV", "test")
os.environ.setdefault("NEXUS_JWT_SECRET", "a34-harness")
os.environ.setdefault("QEC_EXPLORER_TOKEN", "a34-harness")

from sqlalchemy import event, text  # noqa: E402

#: The reserved namespace this harness owns. Nothing outside it is touched.
PREFIX = "a34-"
#: The role whose RLS posture production runs under.
PROD_ROLE = "qec"


def _install_set_role(engine, role: str) -> None:
    """Force every pooled connection onto ``role`` for its whole lifetime.

    Attached to the SYNC engine's ``connect`` event (the asyncpg dialect drives
    that under the async facade), so it fires once per physical connection —
    including connections the pool opens later, which a one-shot SET ROLE on a
    single checkout would miss.
    """
    @event.listens_for(engine.sync_engine, "connect")
    def _set_role(dbapi_conn, _rec):  # noqa: ANN001
        dbapi_conn.await_(dbapi_conn.driver_connection.execute(f'SET ROLE "{role}"'))


async def _pick_substrate_role(engine) -> str:
    """The least-privilege substrate role, or a loud refusal to fake one."""
    async with engine.connect() as conn:
        have = {r[0] for r in (await conn.execute(text(
            "SELECT rolname FROM pg_roles WHERE rolname = ANY(:names)"),
            {"names": ["qec_substrate", PROD_ROLE]})).all()}
        readable = {}
        for role in ("qec_substrate", PROD_ROLE):
            if role in have:
                readable[role] = (await conn.execute(text(
                    "SELECT has_table_privilege(:r,'tenants','SELECT')"),
                    {"r": role})).scalar()
    for role in ("qec_substrate", PROD_ROLE):
        if readable.get(role):
            return role
    raise SystemExit(
        f"REFUSING TO MEASURE: no least-privilege role can read the tenant "
        f"registry (checked qec_substrate, {PROD_ROLE}; found {readable}). "
        f"fleet_tenant_ids() would fail-soft to [PLATFORM_SCOPE] and the run "
        f"would report a one-tenant scan as a fleet scan.")


async def _assert_production_posture(engine) -> dict:
    """Refuse to measure unless RLS is genuinely enforcing."""
    async with engine.connect() as conn:
        who = (await conn.execute(text("SELECT current_user"))).scalar()
        bypass = (await conn.execute(text(
            "SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user"))).scalar()
        forced = (await conn.execute(text(
            "SELECT bool_and(relforcerowsecurity) FROM pg_class "
            "WHERE relname IN ('client_apps','app_cycles','change_events')"))).scalar()
    posture = {"current_user": who, "bypassrls": bool(bypass), "force_rls": bool(forced)}
    if who != PROD_ROLE or bypass:
        raise SystemExit(
            f"REFUSING TO MEASURE: posture is {posture}. A scan measured as a "
            f"superuser (or a BYPASSRLS role) does not run the plan production "
            f"runs, so its timings would not describe production.")
    return posture


async def _seed(sub_engine, qec_engine_, n_tenants: int, seeded: set[str]) -> None:
    """Add tenants until the registry holds ``n_tenants`` harness tenants.

    Incremental: a run measuring 1 → 1024 seeds the DELTA at each step rather
    than tearing down and rebuilding, so the row population is monotonic and the
    later measurements include the earlier tenants' rows.
    """
    want = [f"{PREFIX}{i:05d}" for i in range(n_tenants)]
    todo = [t for t in want if t not in seeded]
    if not todo:
        return
    async with sub_engine.begin() as conn:
        for t in todo:
            await conn.execute(text(
                "INSERT INTO tenants (tenant_id, name) VALUES (:t, :n) "
                "ON CONFLICT (tenant_id) DO NOTHING"), {"t": t, "n": f"A34 {t}"})
    async with qec_engine_.begin() as conn:
        for t in todo:
            await conn.execute(text("SELECT set_config('nexus.current_tenant_id', :t, true)"),
                               {"t": t})
            await conn.execute(text(
                "INSERT INTO client_apps (tenant_id, app_id, name, base_url, status, "
                "schedule, fences, latest_artifact_id) "
                "VALUES (:t, :a, :n, 'https://a34.invalid', 'active', "
                "'{}'::jsonb, '{}'::jsonb, :art) "
                "ON CONFLICT DO NOTHING"),
                {"t": t, "a": f"{PREFIX}app-{t}", "n": f"A34 app {t}",
                 "art": uuid.uuid4().hex})
    seeded.update(todo)


async def _cleanup(sub_engine, qec_engine_) -> None:
    async with qec_engine_.begin() as conn:
        await conn.execute(text("SET LOCAL row_security = off"))
        await conn.execute(text("DELETE FROM client_apps WHERE tenant_id LIKE :p"),
                           {"p": PREFIX + "%"})
    async with sub_engine.begin() as conn:
        await conn.execute(text("DELETE FROM tenants WHERE tenant_id LIKE :p"),
                           {"p": PREFIX + "%"})


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--steps", default="1,8,32,64,128,256,512,1024",
                    help="tenant counts to measure, ascending")
    ap.add_argument("--repeats", type=int, default=5)
    ap.add_argument("--limit", type=int, default=50, help="the discovery limit")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    steps = [int(s) for s in args.steps.split(",") if s.strip()]

    from sqlalchemy.ext.asyncio import create_async_engine

    from app.controlplane.cycle.driver import _scan_fleet
    from app.db import qec_engine, substrate_engine

    # The production posture, applied to the engines the driver itself uses.
    #
    # SUBSTRATE ROLE.  Production splits the least-privilege roles in two —
    # ``qec`` on qecentral, ``qec_substrate`` on nexus.  A development database
    # bootstrapped without the second one must NOT silently fall back to the
    # superuser: ``fleet_tenant_ids`` catches its own failure and returns
    # ``[PLATFORM_SCOPE]``, so a SET ROLE to a non-existent role turns the whole
    # measurement into a one-tenant scan that still prints a plausible number.
    # That is exactly the shape of a green-washed benchmark, so the role is
    # resolved against ``pg_roles`` and the choice is recorded in the evidence.
    substrate_role = await _pick_substrate_role(substrate_engine)
    _install_set_role(qec_engine, PROD_ROLE)
    _install_set_role(substrate_engine, substrate_role)

    # A separate privileged engine does the seeding: the harness must be able to
    # write rows the scan is not permitted to write.
    admin_qec = create_async_engine(QEC_DB_URL)
    admin_sub = create_async_engine(SUBSTRATE_DB_URL)

    posture = await _assert_production_posture(qec_engine)
    print(f"posture: {posture}")

    # PURGE FIRST, not just at the end. A run killed mid-sweep leaves its
    # tenants behind, and the next run then measures a fleet it did not build.
    await _cleanup(admin_sub, admin_qec)
    from app.controlplane.tenant_scope import fleet_tenant_ids
    baseline_tenants = len(await fleet_tenant_ids())
    print(f"pre-existing fleet: {baseline_tenants} tenant(s) "
          f"(harness rows purged before measuring)")

    # Warm the pool so the first measurement is not a TCP handshake.
    await _scan_fleet(args.limit)

    seeded: set[str] = set()
    results = []
    try:
        for n in steps:
            await _seed(admin_sub, admin_qec, n, seeded)
            enumerated = len(await fleet_tenant_ids())
            samples = []
            for _ in range(args.repeats):
                t0 = time.perf_counter()
                active, deferred, changes, apps = await _scan_fleet(args.limit)
                samples.append(time.perf_counter() - t0)
            row = {
                "tenants_seeded": n,
                "tenants_enumerated": enumerated,
                "p50_s": round(statistics.median(samples), 4),
                "min_s": round(min(samples), 4),
                "max_s": round(max(samples), 4),
                "apps_returned": len(apps),
                "active_returned": len(active),
                "per_tenant_ms": round(statistics.median(samples) / max(1, n) * 1000, 3),
                "round_trips": 3 * n,
            }
            # THE ANTI-GREEN-WASH CHECK, IN BOTH DIRECTIONS.
            #
            # It originally only asserted `apps_returned >= n` — that the scan
            # had not under-reached the fleet. That is half the property, and
            # the missing half produced a completely invalid dataset:
            #
            #   an earlier run was KILLED mid-sweep (a Docker outage), so its
            #   `finally` cleanup never ran and 1024 harness tenants stayed in
            #   the registry. The next run's "1 tenant" step therefore scanned
            #   1024, as did every other step. Every row reported
            #   apps_returned=1024, the `>= n` guard was satisfied at each one,
            #   and the sweep printed a per-tenant cost that fell from 32,175ms
            #   to 13.6ms — an "efficiency curve" that was pure artefact.
            #
            # So the scan must see EXACTLY the fleet this step intends: the
            # tenants that were here before, plus this step's n, and no more.
            expected = baseline_tenants + n
            if row["tenants_enumerated"] != expected:
                raise SystemExit(
                    f"REFUSING TO RECORD: step n={n} intended a fleet of "
                    f"{expected} tenants ({baseline_tenants} pre-existing + {n}) "
                    f"but fleet_tenant_ids() returned "
                    f"{row['tenants_enumerated']}. A timing taken against a "
                    f"different fleet than the one it is labelled with is not a "
                    f"scalability measurement.")
            if row["apps_returned"] < n:
                raise SystemExit(
                    f"REFUSING TO RECORD: seeded {n} tenants but the scan "
                    f"returned only {row['apps_returned']} apps. The timing "
                    f"would describe a scan that never reached the fleet.")
            results.append(row)
            print(f"  tenants={row['tenants_enumerated']:>5}  "
                  f"p50={row['p50_s']:>8.4f}s  "
                  f"per-tenant={row['per_tenant_ms']:>7.3f}ms  "
                  f"apps={row['apps_returned']:>5}  round_trips={row['round_trips']}")
    finally:
        await _cleanup(admin_sub, admin_qec)
        await admin_qec.dispose()
        await admin_sub.dispose()

    payload = {"posture": posture, "limit": args.limit,
               "repeats": args.repeats, "results": results}
    if args.out:
        Path(args.out).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
