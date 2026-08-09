"""P3 — persona journey generation (project_from_catalog). Pure; exercises the
full catalog+branches → rules → projected journey chain."""
from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.services import catalog
from app.services.catalog import question_id_for
from app.services.journey_projector import rules_from_branches
from app.services.persona_journeys import project_from_catalog

DB_URL = os.environ.get("QEC_TEST_DATABASE_URL", "")
needs_db = pytest.mark.skipif(
    not DB_URL,
    reason="QEC_TEST_DATABASE_URL not set — the persona generation round-trip needs "
           "a disposable Postgres (QecBase tables are created in-test)")


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


def _fixture():
    nodes = [{"node_fp": "n1", "title": "Health", "controls": [
        {"name": "Cigarettes Per Day", "signature": "sig-cig", "type": "number"}]}]
    branches = [
        {"node_fp": "n1", "control_signature": "q:tobacco",
         "control_label_norm": "tobacco use", "option_label_norm": "yes",
         "reveals": ["input:cigarettes per day"]},
        {"node_fp": "n1", "control_signature": "q:tobacco",
         "control_label_norm": "tobacco use", "option_label_norm": "no"},
    ]
    master = catalog.build_master_catalog(nodes, branches=branches)
    rules = rules_from_branches(branches, master["questions"])
    return master, rules


def test_smoker_persona_activates_the_conditional_question_by_name():
    master, rules = _fixture()
    smoker = project_from_catalog(master, rules, {"tobacco use": "yes"})
    assert "Cigarettes Per Day" in [q["name"] for q in smoker["activated"]]
    assert smoker["counts"]["activated"] == 1
    assert smoker["answered"] == 1


def test_healthy_persona_skips_the_conditional_question():
    master, rules = _fixture()
    healthy = project_from_catalog(master, rules, {"tobacco use": "no"})
    assert "Cigarettes Per Day" in [q["name"] for q in healthy["skipped"]]
    assert healthy["counts"]["activated"] == 0


def test_unknown_answer_key_is_dropped_not_guessed():
    master, rules = _fixture()
    none = project_from_catalog(master, rules, {"nonexistent question": "x"})
    assert none["answered"] == 0


def test_answers_keyed_by_question_id_also_work():
    master, rules = _fixture()
    from app.services.catalog import question_id_for
    trig = question_id_for({"signature": "q:tobacco", "name": "tobacco use"})
    smoker = project_from_catalog(master, rules, {trig: "yes"})
    assert smoker["counts"]["activated"] == 1


@needs_db
def test_generate_all_produces_distinct_persona_journeys():
    asyncio.run(_run_persona_gen())


async def _run_persona_gen():
    from app.db.journey_models import (
        JourneyBranchRow, JourneyNodeRow, PersonaJourneyRow)
    from app.db.models import QecBase
    from app.services import catalog_store, persona_journeys

    engine = create_async_engine(DB_URL)
    async with engine.begin() as conn:
        await conn.run_sync(QecBase.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant = f"qec-pj-{uuid.uuid4().hex[:10]}"
    app_id = "pj-app"
    child = question_id_for({"signature": "sig-cig", "name": "Cigarettes Per Day"})
    orig = (catalog_store.tenant_scoped_qec_session,
            persona_journeys.tenant_scoped_qec_session)
    try:
        catalog_store.tenant_scoped_qec_session = lambda tid: _scoped(factory, tid)
        persona_journeys.tenant_scoped_qec_session = lambda tid: _scoped(factory, tid)

        # A cigarettes field + a tobacco questionnaire question that reveals it.
        async with _scoped(factory, tenant) as s:
            # ids are PKs — scope by tenant so reruns on a persistent DB don't collide.
            s.add(JourneyNodeRow(
                node_id=f"n1-{tenant}", tenant_id=tenant, app_id=app_id,
                fingerprint="fp1", url="u", title="Health", controls_inventory=[
                    {"name": "Cigarettes Per Day", "signature": "sig-cig",
                     "type": "number", "question_id": child}]))
            s.add(JourneyBranchRow(
                branch_id=f"b-yes-{tenant}", tenant_id=tenant, app_id=app_id,
                node_fp="fp1", control_signature="q:tobacco",
                control_label_norm="tobacco use", option_label_norm="yes",
                status="walked", reveals=["input:cigarettes per day"]))
            s.add(JourneyBranchRow(
                branch_id=f"b-no-{tenant}", tenant_id=tenant, app_id=app_id,
                node_fp="fp1", control_signature="q:tobacco",
                control_label_norm="tobacco use", option_label_norm="no",
                status="discovered"))

        await persona_journeys.register_persona(
            tenant_id=tenant, app_id=app_id, name="Tobacco",
            answers={"tobacco use": "yes"})
        await persona_journeys.register_persona(
            tenant_id=tenant, app_id=app_id, name="Healthy",
            answers={"tobacco use": "no"})

        res = await persona_journeys.generate_all_journeys(
            tenant_id=tenant, app_id=app_id)
        assert res["generated"] == 2

        listing = await persona_journeys.list_personas(tenant_id=tenant, app_id=app_id)
        by_name = {p["name"]: p["persona_id"] for p in listing["personas"]}
        async with _scoped(factory, tenant) as s:
            rows = {r.persona_id: r for r in (await s.execute(
                select(PersonaJourneyRow))).scalars().all()}
        # Distinct journeys from ONE catalog: smoker executes/activates the
        # cigarettes question; healthy skips it.
        assert child in rows[by_name["Tobacco"]].activated
        assert child in rows[by_name["Healthy"]].skipped
        assert rows[by_name["Tobacco"]].provenance == "inferred"
    finally:
        catalog_store.tenant_scoped_qec_session = orig[0]
        persona_journeys.tenant_scoped_qec_session = orig[1]
        await engine.dispose()
