"""Phase 0 WS-B — DB-gated integration test for the stale-crawl reaper write path.

Behind ``QEC_TEST_QEC_DATABASE_URL`` (a disposable qecentral-shaped Postgres at
alembic head). Proves the fleet-wide conditional UPDATE actually transitions a stale
row to ``stalled`` with an honest reason, leaves a within-budget row untouched, and —
via the status-guarded UPDATE — never clobbers a row a completion callback just
finished (no lost artifact).
"""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool  # test-only: no cross-event-loop pooling

from app.controlplane import reaper
from app.routers.internal import _TERMINAL_STATES

DB_URL = os.environ.get("QEC_TEST_QEC_DATABASE_URL", "")

needs_db = pytest.mark.skipif(
    not DB_URL,
    reason="QEC_TEST_QEC_DATABASE_URL not set — reaper write test needs a disposable Postgres",
)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(scope="module", autouse=True)
def _repoint_engine():
    if not DB_URL:
        yield
        return
    engine = create_async_engine(DB_URL, pool_pre_ping=True, poolclass=NullPool)
    original = reaper.qec_engine
    reaper.qec_engine = engine  # the reaper reads/writes fleet-wide via this engine
    try:
        yield
    finally:
        reaper.qec_engine = original
        run(engine.dispose())


async def _register_tenant(tenant: str) -> None:
    """Register the test tenant in the SUBSTRATE tenants registry via the SAME
    production bootstrap statement creator.py uses — the reaper's fleet sweep
    enumerates tenants from this registry (reaper._tenant_ids), so an
    unregistered tenant's rows are honestly invisible to it. Real path, no
    mocks; ON CONFLICT keeps it idempotent."""
    async with reaper.substrate_engine.begin() as conn:
        await conn.execute(text(
            "INSERT INTO tenants (tenant_id, name, domain, plan, status) "
            "VALUES (:tid, :name, :domain, 'starter', 'active') "
            "ON CONFLICT (tenant_id) DO NOTHING"
        ), {"tid": tenant, "name": f"reaper-test {tenant}", "domain": f"{tenant}.test"})


async def _insert(engine, *, status, started_at, stats, tenant, eid):
    async with engine.begin() as conn:
        # The qec role runs under FORCE RLS: the WITH CHECK policy rejects any
        # write whose row tenant does not match the transaction GUC — exactly
        # like every production write path, the test must scope its transaction.
        await conn.execute(
            text("SELECT set_config('nexus.current_tenant_id', :t, true)"),
            {"t": tenant},
        )
        await conn.execute(text(
            "INSERT INTO qe_explorations "
            "(exploration_id, tenant_id, app_id, status, stats, error, started_at, created_at, updated_at) "
            "VALUES (:eid, :t, :a, :s, CAST(:st AS JSONB), '', :sa, :ca, :ca)"
        ), {
            "eid": eid, "t": tenant, "a": "app-x", "s": status,
            "st": __import__("json").dumps(stats),
            "sa": started_at, "ca": started_at,
        })


async def _status(engine, eid, tenant):
    async with engine.begin() as conn:
        # Same RLS discipline on the READ side: a GUC-less SELECT under the
        # FORCEd policy is filtered to zero rows (that silence would look like
        # a missing row, not a permissions issue).
        await conn.execute(
            text("SELECT set_config('nexus.current_tenant_id', :t, true)"),
            {"t": tenant},
        )
        row = (await conn.execute(text(
            "SELECT status, error, finished_at FROM qe_explorations WHERE exploration_id=:e"
        ), {"e": eid})).mappings().first()
        return row


@needs_db
def test_stale_row_is_reaped_fresh_row_untouched():
    engine = reaper.qec_engine
    now = datetime.now(timezone.utc)
    tenant = f"t-{uuid.uuid4().hex[:10]}"
    stale_id = f"exp-stale-{uuid.uuid4().hex[:8]}"
    fresh_id = f"exp-fresh-{uuid.uuid4().hex[:8]}"

    # The reaper enumerates tenants from the substrate registry — register the
    # test tenant through the SAME bootstrap path production uses.
    run(_register_tenant(tenant))
    # Stale: running, 10 min old, 5-min wall budget → past 5m+3m window.
    run(_insert(engine, status="running", started_at=now - timedelta(seconds=600),
                stats={"budget_wall_ms": 300_000}, tenant=tenant, eid=stale_id))
    # Fresh: running, 1 min old, same budget → within window.
    run(_insert(engine, status="running", started_at=now - timedelta(seconds=60),
                stats={"budget_wall_ms": 300_000}, tenant=tenant, eid=fresh_id))

    reaped = run(reaper.reap_stale_explorations(now=now))
    assert reaped >= 1

    stale = run(_status(engine, stale_id, tenant))
    assert stale["status"] == "stalled"
    assert "no completion callback" in stale["error"]
    assert stale["finished_at"] is not None

    fresh = run(_status(engine, fresh_id, tenant))
    assert fresh["status"] == "running"  # untouched — within budget


@needs_db
def test_stalled_is_not_a_callback_terminal_state():
    # A late-but-valid completion callback must still be able to supersede a reaped
    # row — so 'stalled' must NOT be in the callback idempotency terminal set.
    assert "stalled" not in _TERMINAL_STATES


@needs_db
def test_reaper_is_idempotent_across_sweeps():
    engine = reaper.qec_engine
    now = datetime.now(timezone.utc)
    tenant = f"t-{uuid.uuid4().hex[:10]}"
    eid = f"exp-{uuid.uuid4().hex[:8]}"
    run(_register_tenant(tenant))
    run(_insert(engine, status="running", started_at=now - timedelta(seconds=3600),
                stats={"budget_wall_ms": 300_000}, tenant=tenant, eid=eid))
    run(reaper.reap_stale_explorations(now=now))
    first = run(_status(engine, eid, tenant))
    finished_after_first = first["finished_at"]
    # Second sweep: the row is now 'stalled' (terminal-for-UI), no longer in the
    # active set, so it is not touched again.
    run(reaper.reap_stale_explorations(now=now + timedelta(seconds=30)))
    second = run(_status(engine, eid, tenant))
    assert second["status"] == "stalled"
    assert second["finished_at"] == finished_after_first
