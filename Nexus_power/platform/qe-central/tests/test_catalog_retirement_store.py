"""M2.3 — the retirement DB seam: stamp it, exclude it, revive it.

The milestone's proof runs two REAL crawls of a real application through the
whole chain (``tests/contract/test_m23_retirement_regression.py``). This module
covers the one durable behaviour that proof cannot reach, because it needs a
THIRD crawl: an application that starts asking a question again.

That path matters more than its size suggests. If revival is broken, a question
the application restored stays retired for ever, it never returns to the active
catalogue, and every diff from then on is permanently wrong about a live
question — a failure that is silent, compounding, and exactly the kind the
catalogue exists to prevent.

DB-gated in the house pattern: skips without a disposable Postgres, fails loudly
under ``QEC_REQUIRE_DB``.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.services import catalog_store

DB_URL = os.environ.get("QEC_TEST_DATABASE_URL", "")
needs_db = pytest.mark.skipif(
    not DB_URL,
    reason="QEC_TEST_DATABASE_URL not set — the retirement round-trip needs a "
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


async def _set_inventory(factory, tenant, app_id, fp, controls):
    """Write one node's control inventory, exactly as the fold does."""
    from app.db.journey_models import JourneyNodeRow
    async with _scoped(factory, tenant) as s:
        row = (await s.execute(select(JourneyNodeRow).where(
            JourneyNodeRow.tenant_id == tenant,
            JourneyNodeRow.app_id == app_id,
            JourneyNodeRow.fingerprint == fp))).scalar_one_or_none()
        if row is None:
            s.add(JourneyNodeRow(
                node_id=f"n-{tenant}-{fp}", tenant_id=tenant, app_id=app_id,
                fingerprint=fp, url="http://a.test/apply", title="Apply",
                controls_inventory=controls))
        else:
            row.controls_inventory = controls


@needs_db
def test_retirement_is_stamped_excluded_and_reversible():
    asyncio.run(_run())


async def _run():
    from app.db.journey_models import CatalogQuestionRow
    from app.db.models import QecBase
    from app.services.catalog import (
        LIFECYCLE_RETIRED, apply_control_lifecycle, question_id_for)

    engine = create_async_engine(DB_URL)
    async with engine.begin() as conn:
        await conn.run_sync(QecBase.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant = f"qec-ret-{uuid.uuid4().hex[:10]}"
    app_id = "ret-app"
    fp = f"fp-{uuid.uuid4().hex[:8]}"

    def ctrl(name, **extra):
        c = {"name": name, "type": "text", "options": [], "required": False}
        c["question_id"] = question_id_for(c)
        c.update(extra)
        return c

    email, beneficiary = ctrl("Email"), ctrl("Beneficiary")
    q_email, q_ben = email["question_id"], beneficiary["question_id"]

    original = catalog_store.tenant_scoped_qec_session
    try:
        catalog_store.tenant_scoped_qec_session = lambda tid: _scoped(factory, tid)

        # ── CRAWL 1 — both questions asked ──────────────────────────────────
        await _set_inventory(factory, tenant, app_id, fp, [email, beneficiary])
        r1 = await catalog_store.persist_catalog_version(
            tenant_id=tenant, app_id=app_id, crawl_ref="crawl-1")
        assert r1["question_count"] == 2 and r1["questions_retired"] == 0

        # ── CRAWL 2 — the application stops asking one of them ──────────────
        await _set_inventory(factory, tenant, app_id, fp, apply_control_lifecycle(
            [email, beneficiary], {q_email}, crawl_ref="crawl-2",
            now_iso="2026-08-19T12:00:00+00:00", conclusive=True))
        r2 = await catalog_store.persist_catalog_version(
            tenant_id=tenant, app_id=app_id, crawl_ref="crawl-2")
        assert r2["questions_retired"] == 1, r2
        assert r2["question_count"] == 1, "the active catalogue still holds it"
        assert r2["catalog_total"] == 2, "the audit catalogue lost a question"

        async with _scoped(factory, tenant) as s:
            row = (await s.execute(select(CatalogQuestionRow).where(
                CatalogQuestionRow.tenant_id == tenant,
                CatalogQuestionRow.question_id == q_ben))).scalar_one()
            assert row.stale is True and row.retired_at is not None
            assert row.retired_in_crawl == "crawl-2"
            assert row.retire_reason == "conclusive_absence"
            assert row.first_seen_artifact == "crawl-1"
            # THE COLUMN THAT USED TO BUMP ON EVERY FOLD. It must name the last
            # crawl that actually SAW the question, not the one that retired it.
            assert row.last_seen_crawl == "crawl-1"
            assert row.last_seen_artifact == "crawl-1"
            retired_at_first = row.retired_at

        # Excluded from active planning, present for audit.
        active = await catalog_store.build_app_master_catalog(tenant, app_id)
        audit = await catalog_store.build_app_master_catalog(
            tenant, app_id, include_retired=True)
        assert {q["question_id"] for q in active["questions"]} == {q_email}
        assert {q["question_id"] for q in audit["questions"]} == {q_email, q_ben}
        listed = await catalog_store.load_retired_questions(tenant, app_id)
        assert [r["question_id"] for r in listed] == [q_ben]
        assert listed[0]["name"] == "Beneficiary", "the audit row lost its content"

        # ── CRAWL 3 — still gone. The retirement DATE must not drift ────────
        # …and neither may the evidence trail inflate: crawl-3 re-persisted the
        # catalogue without any new page observation, so nothing looked and
        # nothing missed. A ``missed_crawls`` that ticked here would be counting
        # folds, not evidence.
        await catalog_store.persist_catalog_version(
            tenant_id=tenant, app_id=app_id, crawl_ref="crawl-3")
        async with _scoped(factory, tenant) as s:
            row = (await s.execute(select(CatalogQuestionRow).where(
                CatalogQuestionRow.tenant_id == tenant,
                CatalogQuestionRow.question_id == q_ben))).scalar_one()
            assert row.retired_at == retired_at_first, (
                "a later agreeing crawl moved the retirement date; an auditor is "
                "owed the crawl that ESTABLISHED it")
            assert row.retired_in_crawl == "crawl-2"
            assert row.missed_crawls == 1, (
                f"missed_crawls is {row.missed_crawls}; exactly ONE crawl looked "
                f"at the page and did not find the question")

        # ── CRAWL 4 — THE APPLICATION ASKS IT AGAIN ─────────────────────────
        await _set_inventory(factory, tenant, app_id, fp, [email, ctrl("Beneficiary")])
        await catalog_store.persist_catalog_version(
            tenant_id=tenant, app_id=app_id, crawl_ref="crawl-4")
        async with _scoped(factory, tenant) as s:
            row = (await s.execute(select(CatalogQuestionRow).where(
                CatalogQuestionRow.tenant_id == tenant,
                CatalogQuestionRow.question_id == q_ben))).scalar_one()
            assert row.retired_at is None, "a revived question is still retired"
            assert row.stale is False and row.missed_crawls == 0
            assert row.retired_in_crawl == "" and row.retire_reason == ""
            assert row.last_seen_crawl == "crawl-4"
        revived = await catalog_store.build_app_master_catalog(tenant, app_id)
        assert q_ben in {q["question_id"] for q in revived["questions"]}, (
            "the application asks the question again and the active catalogue "
            "still withholds it")
        assert not await catalog_store.load_retired_questions(tenant, app_id)

        # ── AND THE DIFF FOLLOWS IT BOTH WAYS ───────────────────────────────
        d = await catalog_store.diff_latest_versions(tenant, app_id)
        assert d["diff"]["added"] == [q_ben], (
            f"the revival is not reported as an addition: {d['diff']['added']}")
        assert d["diff"]["removed"] == []
        assert q_email in d["diff"]["unchanged_ids"]

        # And the audit description of a removal still resolves after the
        # revival — history is not rewritten by it.
        detail = await catalog_store._describe_removed(
            tenant, app_id, [q_ben], {"questions": [{"question_id": q_ben,
                                                     "name": "Beneficiary"}]})
        assert detail and detail[0]["question_id"] == q_ben
    finally:
        catalog_store.tenant_scoped_qec_session = original
        await engine.dispose()


@needs_db
def test_the_lifecycle_columns_survive_a_row_written_before_m23():
    """Every catalog_questions row that predates this milestone carries the
    column defaults. They must read as ACTIVE and be upsertable, or the first
    fold after deploy would either crash or mass-retire a live catalogue."""
    asyncio.run(_run_legacy())


async def _run_legacy():
    from app.db.journey_models import CatalogQuestionRow
    from app.db.models import QecBase
    from app.services.catalog import question_id_for

    engine = create_async_engine(DB_URL)
    async with engine.begin() as conn:
        await conn.run_sync(QecBase.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant = f"qec-legacy-{uuid.uuid4().hex[:10]}"
    app_id = "legacy-app"
    fp = f"fp-{uuid.uuid4().hex[:8]}"

    ctrl = {"name": "Email", "type": "text", "options": [], "required": False}
    ctrl["question_id"] = question_id_for(ctrl)

    original = catalog_store.tenant_scoped_qec_session
    try:
        catalog_store.tenant_scoped_qec_session = lambda tid: _scoped(factory, tid)
        # A pre-M2.3 row: inserted with the column DEFAULTS and nothing else.
        async with _scoped(factory, tenant) as s:
            s.add(CatalogQuestionRow(
                cq_id=f"cq-{uuid.uuid4().hex[:16]}", tenant_id=tenant,
                app_id=app_id, question_id=ctrl["question_id"], name="Email",
                answer_type="text", options=[], pages=[],
                first_seen_artifact="old-crawl", last_seen_artifact="old-crawl"))

        # A control inventory with NO lifecycle keys — exactly what qec_010 rows
        # hold — folded again.
        await _set_inventory(factory, tenant, app_id, fp,
                             [{"name": "Email", "type": "text"}])
        report = await catalog_store.persist_catalog_version(
            tenant_id=tenant, app_id=app_id, crawl_ref="new-crawl")
        assert report["questions_retired"] == 0, (
            "a fold against pre-M2.3 rows retired something; deploying this "
            "would mass-retire live catalogues")

        async with _scoped(factory, tenant) as s:
            row = (await s.execute(select(CatalogQuestionRow).where(
                CatalogQuestionRow.tenant_id == tenant,
                CatalogQuestionRow.question_id == ctrl["question_id"],
            ))).scalar_one()
            assert row.stale is False and row.retired_at is None
            assert row.last_seen_crawl == "new-crawl"
            assert row.first_seen_artifact == "old-crawl", (
                "the upsert overwrote a historical first-seen record")
    finally:
        catalog_store.tenant_scoped_qec_session = original
        await engine.dispose()
