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
        # NOTE: the parameter is `crawl_ref`, not `artifact_id`. It was renamed
        # when the value it receives was identified as an EXPLORATION id rather
        # than a canonical_artifacts.artifact_id (catalog_store.py:120-131). This
        # call kept the old keyword and so raised TypeError — invisible, because
        # the whole test is gated on QEC_TEST_DATABASE_URL and no CI job had ever
        # provided one. The M0.x database job is what made it run.
        r1 = await catalog_store.persist_catalog_version(
            tenant_id=tenant, app_id=app_id, crawl_ref="art-1")
        assert r1["question_count"] == 2 and r1["questions_upserted"] == 2

        # The live master catalog reads them back.
        master = await catalog_store.build_app_master_catalog(tenant, app_id)
        assert {q["question_id"] for q in master["questions"]} == {"q_email", "q_state"}

        # catalog_questions + one version row exist.
        # Both reads filter tenant_id EXPLICITLY. QEC_TEST_DATABASE_URL is the
        # SUPERUSER DSN, and a superuser bypasses row-level security — so the
        # `_scoped` session's tenant GUC does not filter anything here. Unfiltered,
        # these counts include every other run's and every sibling test's rows, and
        # the assertion fails the second time this test sees the same database.
        from sqlalchemy import select
        async with _scoped(factory, tenant) as s:
            qs = (await s.execute(select(CatalogQuestionRow).where(
                CatalogQuestionRow.tenant_id == tenant))).scalars().all()
            vs = (await s.execute(select(CatalogVersionRow).where(
                CatalogVersionRow.tenant_id == tenant))).scalars().all()
            assert len(qs) == 2 and len(vs) == 1

        # A re-crawl adds a question → a second version → diff names the addition.
        await _add_node(factory, tenant, app_id, "fp2", [
            {"name": "Phone", "question_id": "q_phone", "type": "tel"}])
        await catalog_store.persist_catalog_version(
            tenant_id=tenant, app_id=app_id, crawl_ref="art-2")

        d = await catalog_store.diff_latest_versions(tenant, app_id)
        assert d["diff"] is not None
        assert d["diff"]["added"] == ["q_phone"]
        # diff_latest_versions reports BOTH keys: `crawl_ref` (what the column
        # actually holds) and `artifact_id` (kept for one release so existing
        # readers do not break). Assert both, so the compatibility alias cannot
        # be dropped without this test noticing.
        assert d["to"]["crawl_ref"] == "art-2" and d["to"]["artifact_id"] == "art-2"
        assert d["from"]["crawl_ref"] == "art-1" and d["from"]["artifact_id"] == "art-1"
    finally:
        catalog_store.tenant_scoped_qec_session = original
        await engine.dispose()
