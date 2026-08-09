"""P2/P6 — catalog_store DB round-trip: persist a version, build the live master
catalog, and diff two versions. DB-gated (house pattern) — skips without a
disposable Postgres; the pure cores are covered in test_master_catalog /
test_catalog_diff."""
from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.services import catalog_store

DB_URL = os.environ.get("QEC_TEST_DATABASE_URL", "")
needs_db = pytest.mark.skipif(
    not DB_URL,
    reason="QEC_TEST_DATABASE_URL not set — the catalog_store round-trip needs a "
           "disposable Postgres (QecBase tables are created in-test)",
)


@asynccontextmanager
async def _scoped(factory, tenant):
    session = factory()
    try:
        await session.execute(
            text("SELECT set_config('nexus.current_tenant_id', :t, true)"),
            {"t": tenant})
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def _add_node(factory, tenant, app_id, fp, controls, url="u", title="P"):
    from app.db.journey_models import JourneyNodeRow
    async with _scoped(factory, tenant) as s:
        # node_id must be globally unique (PK) — scope by tenant so reruns on a
        # persistent DB don't collide (production uses _sid(tenant, app, fp)).
        s.add(JourneyNodeRow(
            node_id=f"n-{tenant}-{fp}", tenant_id=tenant, app_id=app_id,
            fingerprint=fp, url=url, title=title, controls_inventory=controls))


@needs_db
def test_catalog_store_persist_build_and_diff_round_trip():
    asyncio.run(_run())


async def _run():
    from app.db.journey_models import (
        CatalogQuestionRow, CatalogVersionRow)
    from app.db.models import QecBase

    engine = create_async_engine(DB_URL)
    async with engine.begin() as conn:
        await conn.run_sync(QecBase.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant = f"qec-cs-{uuid.uuid4().hex[:10]}"
    app_id = "cs-app"
    original = catalog_store.tenant_scoped_qec_session
    try:
        catalog_store.tenant_scoped_qec_session = lambda tid: _scoped(factory, tid)

        # One page with two questions.
        await _add_node(factory, tenant, app_id, "fp1", [
            {"name": "Email", "question_id": "q_email", "type": "email",
             "required": True, "options": []},
            {"name": "State", "question_id": "q_state", "type": "select",
             "options": ["CA", "NY"]}])

        # Persist version 1.
        r1 = await catalog_store.persist_catalog_version(
            tenant_id=tenant, app_id=app_id, artifact_id="art-1")
        assert r1["question_count"] == 2 and r1["questions_upserted"] == 2

        # The live master catalog reads them back.
        master = await catalog_store.build_app_master_catalog(tenant, app_id)
        assert {q["question_id"] for q in master["questions"]} == {"q_email", "q_state"}

        # catalog_questions + one version row exist.
        from sqlalchemy import select
        async with _scoped(factory, tenant) as s:
            qs = (await s.execute(select(CatalogQuestionRow))).scalars().all()
            vs = (await s.execute(select(CatalogVersionRow))).scalars().all()
            assert len(qs) == 2 and len(vs) == 1

        # A re-crawl adds a question → a second version → diff names the addition.
        await _add_node(factory, tenant, app_id, "fp2", [
            {"name": "Phone", "question_id": "q_phone", "type": "tel"}])
        await catalog_store.persist_catalog_version(
            tenant_id=tenant, app_id=app_id, artifact_id="art-2")

        d = await catalog_store.diff_latest_versions(tenant, app_id)
        assert d["diff"] is not None
        assert d["diff"]["added"] == ["q_phone"]
        assert d["to"]["artifact_id"] == "art-2"
        assert d["from"]["artifact_id"] == "art-1"
    finally:
        catalog_store.tenant_scoped_qec_session = original
        await engine.dispose()
