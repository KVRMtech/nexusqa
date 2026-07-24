"""QE-Central S5 — control-plane persistence tests (DB-gated end-to-end).

Behind ``QEC_TEST_QEC_DATABASE_URL`` (a DISPOSABLE qecentral-shaped Postgres at
alembic head — the S5 tables + RLS + the partial unique index present).  Proves
the ORM matches the deployed schema and the honesty invariants hold on real DDL:

  * ``cost_ledger`` round-trips through :func:`meter.record_cost` /
    :func:`meter.load_cycle_ledger`, and :func:`meter.aggregate_cost` sums it;
  * :func:`meter.record_run_cost` is IDEMPOTENT (a retried run never double-bills)
    and records an ``unmetered_run`` gap — never invented ``browser_seconds`` —
    for an uncorrelated run;
  * the ``uq_app_cycles_one_active_per_app`` PARTIAL unique index rejects a second
    ACTIVE cycle for an app, and a terminal cycle frees the slot;
  * ``change_events`` UNIQUE(app_id, dedupe_key) coalesces duplicate signals.

Mirrors ``test_governance_db.py``: the qecentral session factory is repointed at
the test DB for the module (the bounded-context rule keeps this the only seam).
"""
from __future__ import annotations

import asyncio
import os
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool  # test-only: no cross-event-loop pooling

from app.controlplane.cost import meter
from app.controlplane.cost.meter import (
    UNIT_BROWSER_SECONDS,
    UNIT_RUNNER_RUNS,
    UNIT_UNMETERED_RUN,
    aggregate_cost,
)
from app.db import tenant_scoped_qec_session
from app.db.controlplane_models import (
    CYCLE_STATE_DONE,
    AppCycleRow,
    ChangeEventRow,
)

DB_URL = os.environ.get("QEC_TEST_QEC_DATABASE_URL", "")

needs_db = pytest.mark.skipif(
    not DB_URL,
    reason=(
        "QEC_TEST_QEC_DATABASE_URL not set — S5 persistence tests need a "
        "disposable qecentral-shaped Postgres at alembic head (S5 tables + RLS "
        "+ the partial unique index)"
    ),
)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(scope="module", autouse=True)
def _repoint_qec_engine():
    """Repoint the qecentral session factory at the test DB for the module."""
    if not DB_URL:
        yield
        return
    import app.db as qdb

    engine = create_async_engine(DB_URL, pool_pre_ping=True, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    original = qdb._qec_session_factory
    qdb._qec_session_factory = factory
    try:
        yield
    finally:
        qdb._qec_session_factory = original
        run(engine.dispose())


def _ids():
    tag = uuid.uuid4().hex[:12]
    return f"t-{tag}", f"app-{tag}"


def _cid(prefix="c"):
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


async def _insert_cycle(tenant, app, state, cycle_id):
    async with tenant_scoped_qec_session(tenant) as session:
        session.add(AppCycleRow(
            cycle_id=cycle_id, tenant_id=tenant, app_id=app,
            trigger="manual", state=state,
        ))
        await session.flush()


async def _set_cycle_state(tenant, cycle_id, state):
    async with tenant_scoped_qec_session(tenant) as session:
        await session.execute(
            text("UPDATE app_cycles SET state = :s WHERE tenant_id = :t AND cycle_id = :c"),
            {"s": state, "t": tenant, "c": cycle_id},
        )


async def _insert_change(tenant, app, dedupe_key):
    async with tenant_scoped_qec_session(tenant) as session:
        session.add(ChangeEventRow(
            event_id=uuid.uuid4().hex, tenant_id=tenant, app_id=app,
            source="repo_sha", dedupe_key=dedupe_key,
        ))
        await session.flush()


# ── cost_ledger persistence ────────────────────────────────────────────────
@needs_db
class TestCostLedgerPersistence:
    def test_record_cost_roundtrips_and_aggregates(self):
        tenant, app = _ids()
        cyc = _cid("cyc")
        run(meter.record_cost(
            tenant_id=tenant, app_id=app, cycle_id=cyc,
            units={UNIT_BROWSER_SECONDS: Decimal("4.2"), UNIT_RUNNER_RUNS: 1},
            source_ref="run-1",
        ))
        rows = run(meter.load_cycle_ledger(tenant_id=tenant, cycle_id=cyc))
        agg = aggregate_cost(meter.rows_to_entry_dicts(rows), group_by="cycle_id")
        assert agg[cyc].units[UNIT_BROWSER_SECONDS] == Decimal("4.2")
        assert agg[cyc].units[UNIT_RUNNER_RUNS] == Decimal("1")

    def test_record_run_cost_is_idempotent(self):
        tenant, app = _ids()
        cyc = _cid("cyc")
        record = {"run_id": "r1", "duration_ms": 2000}
        run(meter.record_run_cost(tenant_id=tenant, app_id=app, cycle_id=cyc, run_record=record))
        # A retried cycle posts the SAME run again — must NOT double-bill.
        run(meter.record_run_cost(tenant_id=tenant, app_id=app, cycle_id=cyc, run_record=record))
        rows = run(meter.load_cycle_ledger(tenant_id=tenant, cycle_id=cyc))
        browser = [r for r in rows if r.unit == UNIT_BROWSER_SECONDS]
        assert len(browser) == 1
        assert browser[0].quantity == Decimal("2")

    def test_uncorrelated_run_records_gap_never_invents_seconds(self):
        tenant, app = _ids()
        cyc = _cid("cyc")
        sample = run(meter.record_run_cost(
            tenant_id=tenant, app_id=app, cycle_id=cyc,
            run_record={"duration_ms": 9999},  # no run_id ⇒ uncorrelated
        ))
        assert sample.unmetered is True
        rows = run(meter.load_cycle_ledger(tenant_id=tenant, cycle_id=cyc))
        assert any(r.unit == UNIT_UNMETERED_RUN for r in rows)
        assert not any(r.unit == UNIT_BROWSER_SECONDS for r in rows)


# ── app_cycles partial unique index ────────────────────────────────────────
@needs_db
class TestOneActiveCyclePerApp:
    def test_second_active_cycle_rejected(self):
        tenant, app = _ids()
        run(_insert_cycle(tenant, app, "running", _cid()))
        with pytest.raises(IntegrityError):
            run(_insert_cycle(tenant, app, "pending", _cid()))

    def test_terminal_cycle_frees_the_slot(self):
        tenant, app = _ids()
        first = _cid()
        run(_insert_cycle(tenant, app, "running", first))
        run(_set_cycle_state(tenant, first, CYCLE_STATE_DONE))
        # The app slot is free again — a new active cycle is accepted.
        run(_insert_cycle(tenant, app, "pending", _cid()))

    def test_blackout_deferred_still_holds_the_slot(self):
        tenant, app = _ids()
        run(_insert_cycle(tenant, app, "blackout_deferred", _cid()))
        # blackout_deferred is NON-terminal — it still owns the app slot.
        with pytest.raises(IntegrityError):
            run(_insert_cycle(tenant, app, "pending", _cid()))


# ── change_events dedupe ───────────────────────────────────────────────────
@needs_db
class TestChangeEventDedupe:
    def test_duplicate_dedupe_key_rejected(self):
        tenant, app = _ids()
        run(_insert_change(tenant, app, "sha-abc"))
        with pytest.raises(IntegrityError):
            run(_insert_change(tenant, app, "sha-abc"))

    def test_distinct_dedupe_keys_accepted(self):
        tenant, app = _ids()
        run(_insert_change(tenant, app, "sha-1"))
        run(_insert_change(tenant, app, "sha-2"))
