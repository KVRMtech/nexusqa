"""QE-Central S4 — synthesis persistence tests (DB-gated on QEC_TEST_DATABASE_URL).

Exercises the qecentral-DB write/read path of :mod:`app.services.synthesis`
against a DISPOSABLE Postgres (``QEC_TEST_DATABASE_URL``).  The qec-owned
tables are self-contained (no cross-DB FKs), so ``QecBase.metadata.create_all``
materialises them on any fresh database — no nexus substrate is needed here
(the substrate READ path is covered by the pure tests via injected nodes).

Pins:
  * a fresh crawl upserts scenario rows with the diff verdict + banding;
  * an UNCHANGED re-crawl carries a prior approval forward UNTOUCHED (the
    ``approved_snapshot`` / ``review`` fields synthesis must never disturb) —
    zero human touch;
  * a vanished scenario is flagged ``missing``, never deleted;
  * a linked certified invariant bands its scenario P0 (presence signal).
"""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.gov_models import CertifiedInvariantRow, ScenarioRow
from app.db.models import QecBase
from app.services import synthesis as S
from app.services.synthesis import PageNode, build_journeys, compute_diff

DB_URL = os.environ.get("QEC_TEST_DATABASE_URL", "")

needs_db = pytest.mark.skipif(
    not DB_URL,
    reason="QEC_TEST_DATABASE_URL not set — synthesis persistence needs a "
           "disposable Postgres (QecBase tables are created in-test)",
)

NODES = [
    PageNode(1, "acme.example", "/login", ("type", "click"),
             ("Username", "Password"), ("Log in",), False),
    PageNode(2, "acme.example", "/transfer", ("type", "select"),
             ("Amount", "To account"), ("Continue",), False),
    PageNode(3, "acme.example", "/confirm", ("click", "submit"),
             (), ("Confirm",), True),
]


async def _make_sessionmaker():
    engine = create_async_engine(DB_URL)
    async with engine.begin() as conn:
        await conn.run_sync(QecBase.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _open(factory, tenant_id: str):
    """A tenant-scoped session with the RLS GUC set (mirrors tenant_scoped)."""
    session = factory()
    await session.execute(
        text("SELECT set_config('nexus.current_tenant_id', :t, true)"),
        {"t": tenant_id},
    )
    return session


@needs_db
def test_persist_upsert_and_unchanged_carry_forward():
    asyncio.run(_run_persist())


async def _run_persist():
    engine, factory = await _make_sessionmaker()
    tenant = f"qec-syn-{uuid.uuid4().hex[:10]}"
    app_id = f"app-{uuid.uuid4().hex[:8]}"
    try:
        journeys = build_journeys(NODES)
        result = compute_diff(app_id, journeys, {})

        # ── first crawl: everything NEW → rows upserted ──────────────────
        session = await _open(factory, tenant)
        try:
            await S._persist(
                session, tenant_id=tenant, app_id=app_id,
                artifact_id="art-1", result=result, stored_rows={},
            )
            await session.commit()
        finally:
            await session.close()

        session = await _open(factory, tenant)
        try:
            stored = await S._load_stored_scenarios(session, tenant, app_id)
            assert len(stored) == len(result.scenarios) >= 1
            trunk_sid = next(
                s.scenario_id for s in result.scenarios if s.kind == S.KIND_TRUNK
            )
            row = stored[trunk_sid]
            assert row.diff_state == S.DIFF_NEW
            assert row.criticality_band == "P0"          # /transfer money route
            assert row.fingerprint and row.journey
            assert row.approved_snapshot is None
            # Simulate a human approval landing on the trunk scenario.
            row.approved_snapshot = {"approved": True, "at": "t0"}
            row.review = {"state": "approved"}
            await session.commit()
        finally:
            await session.close()

        # ── identical re-crawl: UNCHANGED must NOT disturb the approval ──
        session = await _open(factory, tenant)
        try:
            stored = await S._load_stored_scenarios(session, tenant, app_id)
            stored_fp = {sid: r.fingerprint for sid, r in stored.items()}
            recrawl = compute_diff(app_id, build_journeys(NODES), stored_fp)
            assert recrawl.counts[S.DIFF_UNCHANGED] == recrawl.counts["total"]
            assert recrawl.queue == []
            await S._persist(
                session, tenant_id=tenant, app_id=app_id,
                artifact_id="art-2", result=recrawl, stored_rows=stored,
            )
            await session.commit()
        finally:
            await session.close()

        session = await _open(factory, tenant)
        try:
            stored = await S._load_stored_scenarios(session, tenant, app_id)
            row = stored[trunk_sid]
            assert row.diff_state == S.DIFF_UNCHANGED
            assert row.source_artifact_id == "art-2"      # refreshed provenance
            # The approval survived the unchanged re-crawl (zero touch).
            assert row.approved_snapshot == {"approved": True, "at": "t0"}
            assert row.review == {"state": "approved"}
        finally:
            await session.close()
    finally:
        await engine.dispose()


@needs_db
def test_missing_scenario_is_flagged_not_deleted():
    asyncio.run(_run_missing())


async def _run_missing():
    engine, factory = await _make_sessionmaker()
    tenant = f"qec-syn-{uuid.uuid4().hex[:10]}"
    app_id = f"app-{uuid.uuid4().hex[:8]}"
    try:
        # Seed with the full crawl.
        result = compute_diff(app_id, build_journeys(NODES), {})
        session = await _open(factory, tenant)
        try:
            await S._persist(
                session, tenant_id=tenant, app_id=app_id,
                artifact_id="art-1", result=result, stored_rows={},
            )
            await session.commit()
        finally:
            await session.close()

        # Re-crawl a SHORTER journey (the last page vanished) → the original
        # trunk scenario_id is now absent → flagged missing.
        shorter = NODES[:2]
        session = await _open(factory, tenant)
        try:
            stored = await S._load_stored_scenarios(session, tenant, app_id)
            stored_fp = {sid: r.fingerprint for sid, r in stored.items()}
            recrawl = compute_diff(app_id, build_journeys(shorter), stored_fp)
            assert recrawl.counts[S.DIFF_MISSING] >= 1
            await S._persist(
                session, tenant_id=tenant, app_id=app_id,
                artifact_id="art-2", result=recrawl, stored_rows=stored,
            )
            await session.commit()
        finally:
            await session.close()

        session = await _open(factory, tenant)
        try:
            rows = (await session.execute(
                select(ScenarioRow).where(
                    ScenarioRow.tenant_id == tenant, ScenarioRow.app_id == app_id,
                )
            )).scalars().all()
            missing = [r for r in rows if r.diff_state == S.DIFF_MISSING]
            assert len(missing) >= 1  # flagged, still present (not deleted)
        finally:
            await session.close()
    finally:
        await engine.dispose()


@needs_db
def test_certified_invariant_link_bands_scenario_p0():
    asyncio.run(_run_invariant())


async def _run_invariant():
    engine, factory = await _make_sessionmaker()
    tenant = f"qec-syn-{uuid.uuid4().hex[:10]}"
    app_id = f"app-{uuid.uuid4().hex[:8]}"
    try:
        # A non-critical journey that would otherwise fail-up to P1.
        plain = [
            PageNode(1, "h", "/dashboard", ("click",), (), ("Open",), False),
            PageNode(2, "h", "/reports", ("click",), (), ("View",), False),
        ]
        sid = build_journeys(plain)[0].scenario_id(app_id)

        session = await _open(factory, tenant)
        try:
            session.add(CertifiedInvariantRow(
                invariant_id=uuid.uuid4().hex,
                tenant_id=tenant, app_id=app_id,
                statement="reports must reconcile to the ledger",
                linked_scenario_ids=[sid], status="certified",
            ))
            await session.commit()
        finally:
            await session.close()

        session = await _open(factory, tenant)
        try:
            links = await S._invariant_links(session, tenant, app_id)
            assert sid in links
            result = compute_diff(
                app_id, build_journeys(plain), {},
                invariant_links_by_scenario=links,
            )
            banded = next(s for s in result.scenarios if s.scenario_id == sid)
            assert banded.criticality_band == "P0"
        finally:
            await session.close()
    finally:
        await engine.dispose()
