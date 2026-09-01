"""QE-Central S4 — governance persistence tests (DB-gated end-to-end).

Behind ``QEC_TEST_QEC_DATABASE_URL`` (a DISPOSABLE qecentral-shaped Postgres at
alembic head — the 9 S4 tables + RLS present).  Exercises the REAL persistence
paths of :mod:`app.services.approval` and :mod:`app.services.coverage`:

  * append → reload → verify chain; a raw DB tamper on a prior event breaks the
    reloaded chain;
  * a signed universe baseline persists, is the ``latest``, and chains;
  * the shrinkage guard: approve {a,b,c}, then a fresh crawl of {a,b} raises
    EXACTLY one P0 possible_deletion gap and flips the verdict to
    ``blocked_on_p0_gaps``; waiving it returns to ``ok``; expiry re-blocks;
  * a carry-forward append persists but is NOT a human touch;
  * an unsigned approve persists NO row.

The session factory is repointed at the test DB via a module fixture (the
qecentral engine is created from ``settings`` at import time; QE-Central's
bounded-context rule keeps this the only DB seam).
"""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool  # test-only: no cross-event-loop pooling

from app.services import approval, coverage
from app.services.approval import SUBJECT_SCENARIO, SUBJECT_UNIVERSE
from app.services.coverage import AtomInput

DB_URL = os.environ.get("QEC_TEST_QEC_DATABASE_URL", "")

needs_db = pytest.mark.skipif(
    not DB_URL,
    reason=(
        "QEC_TEST_QEC_DATABASE_URL not set — S4 persistence tests need a "
        "disposable qecentral-shaped Postgres at alembic head (9 tables + RLS)"
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


# ── approval chain persistence ────────────────────────────────────────────

@needs_db
class TestApprovalPersistence:
    def test_append_reload_and_verify_chain(self):
        tenant, _ = _ids()
        subject = f"sc-{uuid.uuid4().hex[:8]}"
        run(approval.append_event(
            tenant_id=tenant, subject_kind=SUBJECT_SCENARIO, subject_id=subject,
            action="submit", payload={"v": 1}, actor="jane"))
        rec = run(approval.append_event(
            tenant_id=tenant, subject_kind=SUBJECT_SCENARIO, subject_id=subject,
            action="approve", payload={"v": 1}, signature="Jane Doe", actor="jane"))
        assert rec["is_touch"] is True
        events = run(approval.list_events(
            tenant_id=tenant, subject_kind=SUBJECT_SCENARIO, subject_id=subject))
        assert len(events) == 2
        assert approval.verify_approval_chain(events).ok is True

    def test_raw_db_tamper_breaks_the_reloaded_chain(self):
        tenant, _ = _ids()
        subject = f"sc-{uuid.uuid4().hex[:8]}"
        run(approval.append_event(
            tenant_id=tenant, subject_kind=SUBJECT_SCENARIO, subject_id=subject,
            action="submit", payload={"v": 1}, actor="jane"))
        run(approval.append_event(
            tenant_id=tenant, subject_kind=SUBJECT_SCENARIO, subject_id=subject,
            action="approve", payload={"v": 1}, signature="Jane Doe", actor="jane"))

        async def _tamper():
            async with approval.tenant_scoped_qec_session(tenant) as session:
                await session.execute(
                    text("UPDATE qec_approval_events SET payload = '{\"v\": 999}'::jsonb "
                         "WHERE tenant_id = :t AND action = 'submit'"),
                    {"t": tenant})
                await session.commit()
        run(_tamper())

        events = run(approval.list_events(
            tenant_id=tenant, subject_kind=SUBJECT_SCENARIO, subject_id=subject))
        result = approval.verify_approval_chain(events)
        assert result.ok is False
        assert result.break_index == 0

    def test_unsigned_approve_persists_no_row(self):
        tenant, _ = _ids()
        subject = f"sc-{uuid.uuid4().hex[:8]}"
        with pytest.raises(approval.SignatureRequiredError):
            run(approval.append_event(
                tenant_id=tenant, subject_kind=SUBJECT_SCENARIO, subject_id=subject,
                action="approve", signature=""))
        events = run(approval.list_events(
            tenant_id=tenant, subject_kind=SUBJECT_SCENARIO, subject_id=subject))
        assert events == []

    def test_carry_forward_persists_but_is_not_a_touch(self):
        tenant, _ = _ids()
        subject = f"sc-{uuid.uuid4().hex[:8]}"
        rec = run(approval.append_event(
            tenant_id=tenant, subject_kind=SUBJECT_SCENARIO, subject_id=subject,
            action="carry_forward", carry_forward=True, actor="system"))
        assert rec["is_touch"] is False
        events = run(approval.list_events(
            tenant_id=tenant, subject_kind=SUBJECT_SCENARIO, subject_id=subject))
        assert len(events) == 1
        assert events[0]["is_touch"] is False


# ── universe baseline persistence ─────────────────────────────────────────

@needs_db
class TestBaselinePersistence:
    def test_signed_baseline_is_latest_and_chains(self):
        tenant, app = _ids()
        b1 = run(approval.append_universe_baseline(
            tenant_id=tenant, app_id=app, atoms_hash="h1", atom_count=3,
            signature="Jane Doe", signed_by="jane"))
        b2 = run(approval.append_universe_baseline(
            tenant_id=tenant, app_id=app, atoms_hash="h2", atom_count=4,
            signature="Jane Doe", signed_by="jane"))
        assert b2["prev_hash"] == b1["chain_hash"]
        latest = run(approval.latest_universe_baseline(tenant_id=tenant, app_id=app))
        assert latest["baseline_id"] == b2["baseline_id"]
        chain = run(approval.list_universe_baselines(tenant_id=tenant, app_id=app))
        assert approval.verify_baseline_chain(chain).ok is True


# ── shrinkage guard end-to-end ────────────────────────────────────────────

@needs_db
class TestShrinkageEndToEnd:
    def _seed_and_approve(self, tenant, app, keys):
        run(coverage.upsert_atoms(
            tenant_id=tenant, app_id=app,
            atoms=[AtomInput(canonical_key=k, kind=coverage.ATOM_ROUTE) for k in keys]))
        universe = run(coverage.compute_universe(tenant_id=tenant, app_id=app))
        run(approval.append_universe_baseline(
            tenant_id=tenant, app_id=app, atoms_hash=universe["atoms_hash"],
            atom_count=universe["atom_count"], signature="Jane Doe", signed_by="jane"))
        return universe

    def test_deleted_atom_raises_one_p0_gap_and_blocks(self):
        tenant, app = _ids()
        self._seed_and_approve(tenant, app, ["/a", "/b", "/c"])
        # Fresh crawl no longer sees "/c".
        result = run(coverage.shrinkage_guard(
            tenant_id=tenant, app_id=app, fresh_keys=["/a", "/b"]))
        assert result["hash_verified"] is True
        assert result["missing"] == ["/c"]
        assert result["gaps_created"] == 1
        assert result["verdict"] == coverage.VERDICT_BLOCKED

        gaps = run(coverage.list_gaps(tenant_id=tenant, app_id=app))
        deletions = [g for g in gaps if g["kind"] == coverage.GAP_POSSIBLE_DELETION]
        assert len(deletions) == 1
        assert deletions[0]["band"] == coverage.BAND_P0

    def test_guard_is_idempotent(self):
        tenant, app = _ids()
        self._seed_and_approve(tenant, app, ["/a", "/b", "/c"])
        run(coverage.shrinkage_guard(tenant_id=tenant, app_id=app, fresh_keys=["/a", "/b"]))
        again = run(coverage.shrinkage_guard(
            tenant_id=tenant, app_id=app, fresh_keys=["/a", "/b"]))
        assert again["gaps_created"] == 0  # deterministic gap id → no duplicate
        gaps = run(coverage.list_gaps(tenant_id=tenant, app_id=app))
        assert len([g for g in gaps if g["kind"] == coverage.GAP_POSSIBLE_DELETION]) == 1

    def test_no_baseline_is_ok_never_blocked(self):
        tenant, app = _ids()
        run(coverage.upsert_atoms(
            tenant_id=tenant, app_id=app,
            atoms=[AtomInput(canonical_key="/a", kind=coverage.ATOM_ROUTE)]))
        result = run(coverage.shrinkage_guard(
            tenant_id=tenant, app_id=app, fresh_keys=["/a"]))
        assert result["verdict"] == coverage.VERDICT_OK
        assert result["reason"] == "no_baseline"

    def test_waiver_unblocks_then_expiry_reblocks(self):
        from datetime import datetime, timedelta, timezone

        tenant, app = _ids()
        self._seed_and_approve(tenant, app, ["/a", "/b", "/c"])
        run(coverage.shrinkage_guard(tenant_id=tenant, app_id=app, fresh_keys=["/a", "/b"]))
        gap = [g for g in run(coverage.list_gaps(tenant_id=tenant, app_id=app))
               if g["kind"] == coverage.GAP_POSSIBLE_DELETION][0]

        now = datetime.now(timezone.utc)
        run(coverage.waive_gap(
            tenant_id=tenant, app_id=app, gap_id=gap["gap_id"],
            reason="/c retired by product", actor="jane",
            requested_expires_at=now + timedelta(days=10), now=now))

        cov_now = run(coverage.compute_coverage(tenant_id=tenant, app_id=app, now=now))
        assert cov_now["verdict"] == coverage.VERDICT_OK

        after = now + timedelta(days=11)
        cov_after = run(coverage.compute_coverage(tenant_id=tenant, app_id=app, now=after))
        assert cov_after["verdict"] == coverage.VERDICT_BLOCKED

    def test_adjudication_unblocks(self):
        tenant, app = _ids()
        self._seed_and_approve(tenant, app, ["/a", "/b", "/c"])
        run(coverage.shrinkage_guard(tenant_id=tenant, app_id=app, fresh_keys=["/a", "/b"]))
        gap = [g for g in run(coverage.list_gaps(tenant_id=tenant, app_id=app))
               if g["kind"] == coverage.GAP_POSSIBLE_DELETION][0]
        run(coverage.adjudicate_gap(
            tenant_id=tenant, app_id=app, gap_id=gap["gap_id"], actor="jane",
            note="confirmed intentional removal"))
        cov = run(coverage.compute_coverage(tenant_id=tenant, app_id=app))
        assert cov["verdict"] == coverage.VERDICT_OK
