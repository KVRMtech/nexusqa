"""M2.4 / T-GEN-03 — the fold PERSISTS the endpoint map, on both sides of the join.

The pure tests prove the attribution rules are right.  This proves the fold
actually writes them, which is a different claim and the one the API depends on:
``GET /apps/{id}/journeys`` and the compile payload read graph ROWS, so a join
that is only ever computed in memory during a fold is a join no request can see.

Two columns, two different facts, and the test insists on both:

  * ``journey_nodes.observed_endpoints`` — the calls seen while a STATE was open;
  * ``journey_edges.observed_endpoints`` — the calls the crawl RECORDED that
    TRIGGER firing, joined from the M2.5 stamp.

The edge column is the load-bearing one.  "Which click caused this POST" is a
question about the click, and an answer stored against a page cannot answer it.

Skipif-gated on ``QEC_TEST_DATABASE_URL`` (house pattern): the tables are created
in-test against a disposable Postgres.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.services import journey_fold

DB_URL = os.environ.get("QEC_TEST_DATABASE_URL", "")
needs_db = pytest.mark.skipif(
    not DB_URL,
    reason="QEC_TEST_DATABASE_URL not set — the endpoint-persistence round-trip "
           "needs a disposable Postgres (QecBase tables are created in-test)",
)

FP_START, FP_RESULT = "m24-fp-start", "m24-fp-result"


@asynccontextmanager
async def _scoped(factory, tenant):
    session = factory()
    try:
        await session.execute(
            text("SELECT set_config('nexus.current_tenant_id', :t, true)"),
            {"t": tenant},
        )
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


def _coverage() -> dict:
    """A crawl of a two-state quote funnel, with M2.5 network evidence.

    ``coverage.states`` carries the per-state endpoint map; ``endpoint_inventory``
    carries the application-level surface WITH the UI action each endpoint was
    observed firing.  Both are what a real crawl now emits.
    """
    return {
        "flows": [{
            "flow_id": "m" * 24,
            "entry_fingerprint": FP_START,
            "entry_url": "https://a.example/start",
            "entry_title": "Quote Start",
            "terminal": "submit_boundary",
            "completed": True,
            "fully_answered": True,
            "outcome_values": [{"label": "Monthly Premium", "value": "$42.50",
                                "value_type": "currency"}],
            "steps": [
                {"fingerprint": FP_START, "url": "https://a.example/start",
                 "title": "Quote Start", "fields_filled": 0, "fields_unfilled": 0,
                 "advance": {"control_name": "Get Quote", "tier": 1}},
                {"fingerprint": FP_RESULT, "url": "https://a.example/result",
                 "title": "Quote Result", "fields_filled": 0,
                 "fields_unfilled": 0},
            ],
        }],
        "states": [
            {"ax_fingerprint": FP_START, "location": "https://a.example/start",
             "form_snapshot_signals": {}, "controls_total": 1,
             "danger_controls": 0, "danger_names": [],
             "endpoints": [
                 {"method": "GET", "path": "/api/config", "status": "200",
                  "response_mime": "application/json"}]},
            {"ax_fingerprint": FP_RESULT, "location": "https://a.example/result",
             "form_snapshot_signals": {}, "controls_total": 0,
             "danger_controls": 0, "danger_names": [],
             "endpoints": [
                 {"method": "GET", "path": "/api/config", "status": "200",
                  "response_mime": "application/json"},
                 {"method": "POST", "path": "/api/quote", "status": "200",
                  "response_mime": "application/json"}]},
        ],
        # M2.5 — the stamp the causal join reads.  ``/api/config`` carries NO
        # action (it is page-load traffic), so only the commit is attributable.
        "endpoint_inventory": [
            {"method": "GET", "path_template": "/api/config",
             "statuses": {"200": 1}, "actions": [], "response_shape": "object"},
            {"method": "POST", "path_template": "/api/quote",
             "statuses": {"503": 2, "200": 1},
             "actions": [{"verb": "click", "label": "Get Quote",
                          "action_token": "a2"}],
             "response_shape": "object"},
        ],
    }


@needs_db
def test_the_fold_persists_both_sides_of_the_endpoint_join():
    asyncio.run(_run())


async def _run():
    from app.db.journey_models import JourneyEdgeRow, JourneyNodeRow
    from app.db.models import QecBase

    engine = create_async_engine(DB_URL)
    async with engine.begin() as conn:
        await conn.run_sync(QecBase.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant = f"qec-m24-{uuid.uuid4().hex[:10]}"
    app_id = "m24-app"
    original = journey_fold.tenant_scoped_qec_session
    try:
        journey_fold.tenant_scoped_qec_session = lambda tid: _scoped(factory, tid)

        report = await journey_fold.fold_crawl(
            tenant_id=tenant, app_id=app_id, exploration_id="ex-m24",
            coverage=_coverage())
        assert report["journeys"] == 1
        assert report["edges"] == 1

        async with _scoped(factory, tenant) as session:
            nodes = {n.fingerprint: n for n in (await session.execute(
                select(JourneyNodeRow).where(
                    JourneyNodeRow.tenant_id == tenant))).scalars().all()}
            edges = (await session.execute(
                select(JourneyEdgeRow).where(
                    JourneyEdgeRow.tenant_id == tenant))).scalars().all()

        # ── the STATE map ────────────────────────────────────────────────
        start = [(e["method"], e["path"])
                 for e in (nodes[FP_START].observed_endpoints or [])]
        result = [(e["method"], e["path"])
                  for e in (nodes[FP_RESULT].observed_endpoints or [])]
        assert start == [("GET", "/api/config")]
        assert result == [("GET", "/api/config"), ("POST", "/api/quote")]

        # ── the CAUSAL map, which is the one a test step needs ───────────
        assert len(edges) == 1
        edge = edges[0]
        assert edge.trigger_label_norm == "get quote"
        caused = edge.observed_endpoints or []
        assert [(e["method"], e["path"]) for e in caused] == [
            ("POST", "/api/quote")]
        # It is READ from the crawl's stamp, and says so.
        assert caused[0]["attribution"] == "recorded"
        # The endpoint RETRIED (503, 503, 200) and eventually succeeded; the
        # SUCCESS is what a regression test may demand of the application.
        assert caused[0]["status"] == "200"
        # And the page-load read never becomes the click's responsibility.
        assert all(e["path"] != "/api/config" for e in caused)
    finally:
        journey_fold.tenant_scoped_qec_session = original
        await engine.dispose()


@needs_db
def test_a_refold_never_erases_a_previously_observed_endpoint():
    asyncio.run(_run_refold())


async def _run_refold():
    """A crawl that happens not to exercise a call must not delete the evidence
    that the call exists — the same rule the control inventory already follows.
    """
    from app.db.journey_models import JourneyEdgeRow, JourneyNodeRow
    from app.db.models import QecBase

    engine = create_async_engine(DB_URL)
    async with engine.begin() as conn:
        await conn.run_sync(QecBase.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant = f"qec-m24r-{uuid.uuid4().hex[:10]}"
    app_id = "m24-app"
    original = journey_fold.tenant_scoped_qec_session
    try:
        journey_fold.tenant_scoped_qec_session = lambda tid: _scoped(factory, tid)
        await journey_fold.fold_crawl(
            tenant_id=tenant, app_id=app_id, exploration_id="ex-1",
            coverage=_coverage())

        # A second crawl that saw NO network traffic at all.
        quiet = _coverage()
        for state in quiet["states"]:
            state["endpoints"] = []
        quiet["endpoint_inventory"] = []
        await journey_fold.fold_crawl(
            tenant_id=tenant, app_id=app_id, exploration_id="ex-2",
            coverage=quiet)

        async with _scoped(factory, tenant) as session:
            node = (await session.execute(
                select(JourneyNodeRow).where(
                    JourneyNodeRow.tenant_id == tenant,
                    JourneyNodeRow.fingerprint == FP_RESULT,
                ))).scalar_one()
            edge = (await session.execute(
                select(JourneyEdgeRow).where(
                    JourneyEdgeRow.tenant_id == tenant))).scalars().first()
        assert len(node.observed_endpoints or []) == 2
        assert len(edge.observed_endpoints or []) == 1
    finally:
        journey_fold.tenant_scoped_qec_session = original
        await engine.dispose()


@needs_db
def test_the_router_ranks_folded_journeys_from_their_own_rows():
    asyncio.run(_run_router_ranking())


async def _run_router_ranking():
    """T-GEN-02 end to end over REAL rows: fold, then rank.

    Exercises the read path the API actually serves — ``_journey_evidence``
    (walk-ordered nodes, the walked edges, the completed traversal) and
    ``_rank_journeys`` (the tenant's active criticality pack, the band, the
    evidence, the deterministic order).  The pure tests cannot reach it because
    it is all SQL, and an untested read path is how a ranking that works in a
    unit test returns nothing in production.
    """
    from app.db.journey_models import JourneyRow
    from app.db.models import QecBase
    from app.routers import journeys as router
    from app.services import criticality

    engine = create_async_engine(DB_URL)
    async with engine.begin() as conn:
        await conn.run_sync(QecBase.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant = f"qec-m24k-{uuid.uuid4().hex[:10]}"
    app_id = "m24-app"
    originals = (journey_fold.tenant_scoped_qec_session,
                 criticality.tenant_scoped_qec_session)
    try:
        journey_fold.tenant_scoped_qec_session = lambda tid: _scoped(factory, tid)
        criticality.tenant_scoped_qec_session = lambda tid: _scoped(factory, tid)
        await journey_fold.fold_crawl(
            tenant_id=tenant, app_id=app_id, exploration_id="ex-rank",
            coverage=_coverage())

        async with _scoped(factory, tenant) as session:
            rows = (await session.execute(
                select(JourneyRow).where(
                    JourneyRow.tenant_id == tenant))).scalars().all()
            evidence = await router._journey_evidence(
                session, tenant, app_id, rows[0])
            ranked = await router._rank_journeys(
                session, tenant, app_id, list(rows),
                [{"journey_id": r.journey_id, "deepest_steps": r.deepest_steps,
                  "paths_completed": 1} for r in rows])

        # The evidence is the journey's own completed walk, in WALK order.
        assert [n.fingerprint for n in evidence["nodes"]] == [FP_START, FP_RESULT]
        assert evidence["edge_labels"] == ["get quote"]

        assert len(ranked) == 1
        entry = ranked[0]
        assert entry["rank"] == 1
        assert entry["criticality"]["band"] in ("P0", "P1", "P2", "P3")
        assert entry["criticality"]["evidence"]
        assert entry["criticality"]["registry_version"]
        # Read from the graph rows the fold just wrote, not recomputed here.
        assert entry["endpoints_observed"] == 2
        assert entry["boundary_nodes"] == 1
    finally:
        journey_fold.tenant_scoped_qec_session = originals[0]
        criticality.tenant_scoped_qec_session = originals[1]
        await engine.dispose()
