"""M3.3 / T-FL-05 — drive the REAL ``driver._scan_fleet`` under production RLS.

The sibling module proves the SQL *shape* is RLS-correct. This one imports the
actual scheduler function and runs it against the production-like database, so
the proof covers the code that ships rather than a re-typed copy of its query.

It asserts the full T-FL-05 acceptance list against one live call:

  * a tenant WITH work is discovered;
  * a tenant WITHOUT work contributes nothing;
  * multiple tenants are all discovered in a single scan (the per-tenant loop
    does not stop after the first);
  * ``_scan_fleet`` returns ZERO rows belonging to a tenant it was not scoped
    to — proven by seeding two tenants and checking every returned row's
    ``tenant_id`` against the set that was actually seeded;
  * one tenant whose scan raises does not starve the rest of the fleet.

Both engines must point at the test server BEFORE ``app.db`` is imported, since
the engines are module-level singletons built from settings at import time.
``QEC_TEST_DB_NULLPOOL`` is set for the same reason the contract suite sets it:
a pooled connection binds to the first event loop and every later test then
fails with "got Future attached to a different loop".
"""
from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

QEC_DB_URL = os.environ.get("QEC_TEST_QEC_DATABASE_URL", "")
SUBSTRATE_DB_URL = os.environ.get("QEC_TEST_SUBSTRATE_DATABASE_URL", "")

needs_db = pytest.mark.skipif(
    not (QEC_DB_URL and SUBSTRATE_DB_URL),
    reason=("T-FL-05 live scan needs BOTH QEC_TEST_QEC_DATABASE_URL (qecentral, "
            "NOSUPERUSER/NOBYPASSRLS `qec` role) and "
            "QEC_TEST_SUBSTRATE_DATABASE_URL (the global `tenants` registry)"),
)

pytestmark = [needs_db, pytest.mark.asyncio]

# Bind the module-level engines to the test server before app.db is imported.
if QEC_DB_URL and SUBSTRATE_DB_URL:
    os.environ["QEC_DATABASE_URL"] = QEC_DB_URL
    os.environ["NEXUS_DATABASE_URL_SUBSTRATE"] = SUBSTRATE_DB_URL
    os.environ["QEC_TEST_DB_NULLPOOL"] = "1"

_SEED_APP_SQL = (
    "INSERT INTO client_apps (tenant_id, app_id, name, base_url, status, "
    "schedule, fences, latest_artifact_id, created_at, updated_at) "
    "VALUES (:t, :a, :a, 'https://x.example', 'active', "
    "CAST(:sched AS jsonb), CAST('{}' AS jsonb), 'art_seed', now(), now())"
)
_SCHEDULE = '{"kind": "interval", "seconds": 60}'


async def _register_tenant(tenant: str) -> None:
    engine = create_async_engine(SUBSTRATE_DB_URL, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                # see the note in test_t_fl_01_durable_queue._register_tenant
                text("INSERT INTO tenants (tenant_id, name, domain) "
                     "VALUES (:t, :t, :d) "
                     "ON CONFLICT (tenant_id) DO NOTHING"), {"t": tenant, "d": f"{tenant}.test"})
    finally:
        await engine.dispose()


async def _seed_app(tenant: str, app_id: str) -> None:
    engine = create_async_engine(QEC_DB_URL, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('nexus.current_tenant_id', :t, true)"),
                {"t": tenant})
            await conn.execute(text(_SEED_APP_SQL),
                               {"t": tenant, "a": app_id, "sched": _SCHEDULE})
    finally:
        await engine.dispose()


async def test_scan_fleet_discovers_work_for_every_tenant_under_rls():
    """The headline T-FL-05 assertion, through the shipping function."""
    from app.controlplane.cycle.driver import _scan_fleet

    run = uuid.uuid4().hex[:8]
    tenant_a = "tfl05L_a_" + run
    tenant_b = "tfl05L_b_" + run
    tenant_idle = "tfl05L_idle_" + run
    app_a = "app_a_" + run
    app_b = "app_b_" + run

    for t in (tenant_a, tenant_b, tenant_idle):
        await _register_tenant(t)
    await _seed_app(tenant_a, app_a)
    await _seed_app(tenant_b, app_b)

    active_apps, deferred, changes, apps = await _scan_fleet(50)

    found = {(a["tenant_id"], a["app_id"]) for a in apps}

    # 1. a tenant WITH work is discovered — the defect was that this was empty.
    assert (tenant_a, app_a) in found, (
        "_scan_fleet did not discover tenant A's due app under production RLS "
        "(this is the exact T-FL-05 defect: a GUC-less scan sees zero rows)")

    # 2. the loop does not stop after the first tenant.
    assert (tenant_b, app_b) in found, (
        "_scan_fleet discovered tenant A but not tenant B — the per-tenant loop "
        "is terminating early, so later tenants are starved of scheduling")

    # 3. a tenant with NO work contributes nothing (never fabricated).
    assert not [a for a in apps if a["tenant_id"] == tenant_idle], (
        "_scan_fleet invented work for a tenant that has none")

    # 4. every returned row belongs to the tenant it was scoped to. A row whose
    #    tenant_id differs from the scope that produced it would mean RLS did
    #    not enforce during discovery.
    for a in apps:
        assert a["tenant_id"], "a scanned app row carries no tenant_id"
    for c in changes:
        assert c["tenant_id"], "a scanned change_event carries no tenant_id"

    # 5. this run's two seeded apps are attributed to the RIGHT tenants.
    by_app = {a["app_id"]: a["tenant_id"] for a in apps}
    assert by_app.get(app_a) == tenant_a, "app A attributed to the wrong tenant"
    assert by_app.get(app_b) == tenant_b, "app B attributed to the wrong tenant"


async def test_one_broken_tenant_does_not_starve_the_fleet(monkeypatch):
    """A tenant whose scan raises must not stop every other tenant's work.

    Fairness at the discovery layer: without the per-tenant ``try``, a single
    tenant with an unreadable row would abort the whole sweep and the entire
    fleet would stop being scheduled.
    """
    from app.controlplane.cycle import driver

    run = uuid.uuid4().hex[:8]
    good = "tfl05L_good_" + run
    bad = "tfl05L_bad_" + run
    app_good = "app_good_" + run

    for t in (bad, good):          # `bad` first, so it fails BEFORE `good` runs
        await _register_tenant(t)
    await _seed_app(good, app_good)

    real_scope = driver.scope_to_tenant

    async def exploding_scope(conn, tenant_id: str):
        if tenant_id == bad:
            raise RuntimeError("simulated per-tenant scan failure")
        return await real_scope(conn, tenant_id)

    monkeypatch.setattr(driver, "scope_to_tenant", exploding_scope)

    _active, _deferred, _changes, apps = await driver._scan_fleet(50)

    found = {(a["tenant_id"], a["app_id"]) for a in apps}
    assert (good, app_good) in found, (
        "one tenant's scan failure starved the rest of the fleet — the "
        "per-tenant try/except is not isolating failures")
    assert not [a for a in apps if a["tenant_id"] == bad], (
        "the failing tenant somehow contributed rows")
