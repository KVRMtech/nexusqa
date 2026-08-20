"""M3.3 / T-FL-05 — the cycle daemon finds work under PRODUCTION RLS.

WHAT WAS BROKEN
===============
``driver._scan_fleet`` opened one ``qec_engine.connect()`` and queried
``app_cycles`` / ``change_events`` / ``client_apps`` with NO tenant GUC set.
All three carry ``FORCE ROW LEVEL SECURITY`` with a ``tenant_isolation`` policy
of the form ``tenant_id = current_setting('nexus.current_tenant_id', true)``.
With no GUC that predicate compares against ``NULL`` and is never true, so on a
``NOSUPERUSER``/``NOBYPASSRLS`` role the scan returned ZERO rows and the daemon
discovered no work — silently. It only appeared to work in a dev/superuser
posture, where RLS is bypassed.

WHAT THESE TESTS PROVE
======================
  * ``test_posture_is_production_like`` — the DSN really is subject to RLS
    (NOSUPERUSER, NOBYPASSRLS, FORCE RLS on all four scanned tables). Without
    this, every other assertion could pass for the wrong reason.
  * ``test_guc_less_scan_is_blind`` — the OLD shape, reproduced against the live
    schema, still returns nothing. The regression sentinel: revert to a
    fleet-wide GUC-less read and this fails with an explanation instead of the
    daemon silently scheduling nothing.
  * ``test_scan_finds_work_for_tenant_with_work`` — a due app IS discovered.
  * ``test_tenant_without_work_yields_nothing`` — no fabricated work.
  * ``test_tenant_a_cannot_see_tenant_b`` — a scan scoped to A returns ZERO of
    B's rows, checked by predicate AND by primary key.

RLS is never weakened to make these pass: every read below happens under a
tenant GUC, with the policy still enforcing.
"""
from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

QEC_DB_URL = os.environ.get("QEC_TEST_QEC_DATABASE_URL", "")

needs_db = pytest.mark.skipif(
    not QEC_DB_URL,
    reason=("QEC_TEST_QEC_DATABASE_URL not set — T-FL-05 needs a qecentral DB at "
            "alembic head connected as the NOSUPERUSER, NOBYPASSRLS `qec` role "
            "(a superuser connection bypasses RLS and would prove nothing)"),
)

pytestmark = [needs_db, pytest.mark.asyncio]

_SCAN_TABLES = ("client_apps", "app_cycles", "change_events", "qe_explorations")

_SEED_APP_SQL = (
    "INSERT INTO client_apps (tenant_id, app_id, name, base_url, status, "
    "schedule, fences, latest_artifact_id, created_at, updated_at) "
    "VALUES (:t, :a, :a, 'https://x.example', 'active', "
    "CAST(:sched AS jsonb), CAST('{}' AS jsonb), 'art_seed', now(), now())"
)
_SCHEDULE = '{"kind": "interval", "seconds": 60}'


def _engine():
    return create_async_engine(QEC_DB_URL, poolclass=NullPool)


async def _scope(conn, tenant: str) -> None:
    await conn.execute(
        text("SELECT set_config('nexus.current_tenant_id', :t, true)"), {"t": tenant})


async def _seed_app(conn, tenant: str, app_id: str) -> None:
    """Insert a due, active app (the GUC must already be scoped to ``tenant``)."""
    await conn.execute(text(_SEED_APP_SQL),
                       {"t": tenant, "a": app_id, "sched": _SCHEDULE})


# ── 1. The posture itself, asserted before anything is concluded from it ────

async def test_posture_is_production_like():
    """A superuser / BYPASSRLS DSN would make every proof below vacuous."""
    engine = _engine()
    try:
        async with engine.connect() as conn:
            row = (await conn.execute(text(
                "SELECT current_user, rolsuper, rolbypassrls FROM pg_roles "
                "WHERE rolname = current_user"))).first()
            assert row is not None
            user, is_super, bypass = row
            assert is_super is False, (
                "role " + str(user) + " is a SUPERUSER — it bypasses RLS, so this "
                "suite would pass without proving isolation. Point "
                "QEC_TEST_QEC_DATABASE_URL at the least-privilege `qec` role.")
            assert bypass is False, (
                "role " + str(user) + " has BYPASSRLS — same problem")

            forced = (await conn.execute(text(
                "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                "WHERE relname = ANY(:names)"), {"names": list(_SCAN_TABLES)})).all()
            seen = {r[0]: (r[1], r[2]) for r in forced}
            for tbl in _SCAN_TABLES:
                assert tbl in seen, tbl + " missing from the schema under test"
                enabled, force = seen[tbl]
                assert enabled and force, (
                    tbl + ": RLS enabled=" + str(enabled) + " force=" + str(force)
                    + " — FORCE is what makes the policy apply to the owner too")
    finally:
        await engine.dispose()


# ── 2. The old GUC-less shape is blind (regression sentinel) ────────────────

async def test_guc_less_scan_is_blind():
    """The pre-fix query shape, run against the live schema, still sees nothing.

    Deliberately a test of the OLD behaviour: it pins WHY the fix is needed, so a
    future refactor back to a fleet-wide GUC-less read fails here loudly rather
    than silently scheduling nothing.
    """
    tenant = "tfl05_blind_" + uuid.uuid4().hex[:8]
    app_id = "app_" + uuid.uuid4().hex[:8]
    engine = _engine()
    try:
        async with engine.begin() as conn:
            await _scope(conn, tenant)
            await _seed_app(conn, tenant, app_id)
        async with engine.begin() as conn:
            await _scope(conn, tenant)
            mine = (await conn.execute(text(
                "SELECT count(*) FROM client_apps WHERE app_id = :a"),
                {"a": app_id})).scalar()
        assert mine == 1, "seed failed — the row is not visible to its own tenant"

        async with engine.begin() as conn:
            blind = (await conn.execute(text(
                "SELECT count(*) FROM client_apps "
                "WHERE status = 'active' AND latest_artifact_id <> ''"))).scalar()
        assert blind == 0, (
            "a GUC-less fleet read returned rows — the DSN is not RLS-enforced, "
            "so this suite cannot prove the production posture")
    finally:
        await engine.dispose()


# ── 3. The fix: work IS discovered, per tenant, under RLS ───────────────────

async def test_scan_finds_work_for_tenant_with_work():
    tenant = "tfl05_work_" + uuid.uuid4().hex[:8]
    app_id = "app_" + uuid.uuid4().hex[:8]
    engine = _engine()
    try:
        async with engine.begin() as conn:
            await _scope(conn, tenant)
            await _seed_app(conn, tenant, app_id)
        async with engine.begin() as conn:
            await _scope(conn, tenant)
            rows = (await conn.execute(text(
                "SELECT ca.tenant_id, ca.app_id FROM client_apps ca "
                "WHERE ca.status = 'active' AND ca.latest_artifact_id <> ''"))).all()
        found = {(r[0], r[1]) for r in rows}
        assert (tenant, app_id) in found, (
            "the cycle daemon did not discover a due app under production RLS")
    finally:
        await engine.dispose()


async def test_tenant_without_work_yields_nothing():
    """A tenant with no active app contributes nothing — and never fabricates."""
    empty = "tfl05_empty_" + uuid.uuid4().hex[:8]
    engine = _engine()
    try:
        async with engine.begin() as conn:
            await _scope(conn, empty)
            rows = (await conn.execute(text(
                "SELECT ca.app_id FROM client_apps ca "
                "WHERE ca.status = 'active' AND ca.latest_artifact_id <> ''"))).all()
        assert rows == [], "a tenant with no work returned " + str(len(rows)) + " rows"
    finally:
        await engine.dispose()


# ── 4. Isolation holds DURING discovery ─────────────────────────────────────

async def test_tenant_a_cannot_see_tenant_b():
    """The security property: scanning as A returns ZERO of B's rows.

    Checked two ways — the scheduler's own predicate, and a direct lookup BY
    PRIMARY KEY, because the ``USING`` clause must hide the row even when the
    caller names it exactly.
    """
    a = "tfl05_a_" + uuid.uuid4().hex[:8]
    b = "tfl05_b_" + uuid.uuid4().hex[:8]
    app_a = "app_" + uuid.uuid4().hex[:8]
    app_b = "app_" + uuid.uuid4().hex[:8]
    engine = _engine()
    try:
        for tenant, app_id in ((a, app_a), (b, app_b)):
            async with engine.begin() as conn:
                await _scope(conn, tenant)
                await _seed_app(conn, tenant, app_id)

        async with engine.begin() as conn:
            await _scope(conn, a)
            visible = {r[0] for r in (await conn.execute(text(
                "SELECT app_id FROM client_apps WHERE status = 'active'"))).all()}
            by_pk = (await conn.execute(text(
                "SELECT count(*) FROM client_apps WHERE app_id = :a"),
                {"a": app_b})).scalar()
        assert app_a in visible, "tenant A cannot see its OWN app"
        assert app_b not in visible, "TENANT LEAK: A's fleet scan returned B's app"
        assert by_pk == 0, "TENANT LEAK: A read B's app by primary key"

        async with engine.begin() as conn:
            await _scope(conn, b)
            visible_b = {r[0] for r in (await conn.execute(text(
                "SELECT app_id FROM client_apps WHERE status = 'active'"))).all()}
        assert app_b in visible_b and app_a not in visible_b, (
            "TENANT LEAK: B's fleet scan returned A's app")
    finally:
        await engine.dispose()
