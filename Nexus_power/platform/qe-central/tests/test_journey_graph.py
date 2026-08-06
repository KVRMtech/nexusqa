"""Journey Graph (Release C) — fold idempotency, branch status law, naming
validation, planner conflict rules, and the earnable branch_coverage.

Pure-logic tests run everywhere; the DB round-trip is skipif-gated on
``QEC_TEST_DATABASE_URL`` (house pattern).
"""
from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.services import branch_planner, journey_fold, journey_naming
from app.services.journey_fold import (
    BRANCH_BLOCKED,
    BRANCH_DEFERRED,
    BRANCH_DISCOVERED,
    BRANCH_PLANNED,
    BRANCH_WALKED,
    flows_of,
    is_pre_hardening,
    path_hash_of,
)


# ── Pure helpers ─────────────────────────────────────────────────────────

def _flow(entry_fp="fpA", terminal="submit_boundary", completed=True,
          steps=None, outcomes=None):
    return {
        "flow_id": "f" * 24, "entry_fingerprint": entry_fp,
        "entry_url": "https://a.example/quote", "entry_title": "Get a Quote",
        "terminal": terminal, "completed": completed, "fully_answered": True,
        "outcome_values": outcomes or [], "steps": steps or [
            {"fingerprint": "fpA", "url": "u1", "title": "Start",
             "fields_filled": 2, "fields_unfilled": 0,
             "advance": {"tier": 1, "control_name": "Continue", "oracle": False},
             "decision_points": [
                 {"control_signature": "sig-smoke", "control_label": "Tobacco use",
                  "options": ["non-smoker", "smoker"], "choice": "non-smoker",
                  "provenance": "synthesized"}]},
            {"fingerprint": "fpB", "url": "u2", "title": "Review",
             "fields_filled": 0, "fields_unfilled": 0},
        ],
    }


def _coverage(*flows, tiers=True):
    summary = {"flows_found": len(flows)}
    if tiers:
        summary["advances_by_tier"] = {}
    return {"flows": list(flows), "flow_summary": summary}


def test_flows_of_tolerates_malformed():
    assert flows_of(None) == []
    assert flows_of({"flows": ["x", 4]}) == []
    assert len(flows_of(_coverage(_flow()))) == 1


def test_pre_hardening_detected_by_missing_tier_rollup():
    assert is_pre_hardening(_coverage(_flow(), tiers=False)) is True
    assert is_pre_hardening(_coverage(_flow())) is False
    assert is_pre_hardening(None) is True


def test_path_hash_is_order_sensitive_and_stable():
    a = [{"fingerprint": "f1"}, {"fingerprint": "f2"}]
    b = [{"fingerprint": "f2"}, {"fingerprint": "f1"}]
    assert path_hash_of(a) == path_hash_of(a)
    assert path_hash_of(a) != path_hash_of(b)


# ── Naming validation (C2) ───────────────────────────────────────────────

def test_url_text_is_rejected_in_names():
    assert journey_naming.looks_like_url_text("Visit https://a.example")
    assert journey_naming.looks_like_url_text("Open /quote/start")
    assert journey_naming.looks_like_url_text("Go to acme.com now")
    assert not journey_naming.looks_like_url_text("Get a life insurance quote")


def test_parse_proposal_reads_two_lines():
    out = journey_naming.parse_proposal(
        "NAME: Get a life insurance quote\n"
        "DESCRIPTION: A visitor answers health questions and sees a premium.")
    assert out == ("Get a life insurance quote",
                   "A visitor answers health questions and sees a premium.")


def test_parse_proposal_rejects_url_tainted_or_unreadable():
    assert journey_naming.parse_proposal("NAME: Visit /quote/start") is None
    assert journey_naming.parse_proposal("I think it is a quote flow") is None
    tainted_desc = journey_naming.parse_proposal(
        "NAME: Get a quote\nDESCRIPTION: at https://a.example/quote")
    assert tainted_desc == ("Get a quote", "")


def test_build_prompt_is_url_free():
    p = journey_naming.build_prompt(
        entry_title="Get a Quote", step_titles=["Health", "Review"],
        outcomes=[{"label": "Monthly Premium", "value_type": "currency"}],
        terminal="submit_boundary")
    assert "http" not in p and "/" not in p
    assert "Monthly Premium" in p and "submit_boundary" in p


# ── Planner conflict rule (C4, pure part) ────────────────────────────────

def test_identity_ref_is_stable_and_value_free():
    a = branch_planner._identity_ref({"sig-a": "smoker", "sig-b": "gold"})
    b = branch_planner._identity_ref({"sig-b": "gold", "sig-a": "smoker"})
    assert a == b and a.startswith("synthetic+planned:")
    assert "smoker" not in a


# ── E1 pure-logic tests ─────────────────────────────────────────────────

def test_branch_deferred_status_exists():
    assert BRANCH_DEFERRED == "deferred"
    assert len(BRANCH_DEFERRED) <= 16  # fits the String(16) column


def test_single_option_identity_ref():
    ref = branch_planner._identity_ref({"sig-plan": "gold"})
    assert ref.startswith("synthetic+planned:")
    assert "gold" not in ref


# ── DB round-trip (skipif-gated) ─────────────────────────────────────────

DB_URL = os.environ.get("QEC_TEST_DATABASE_URL", "")
needs_db = pytest.mark.skipif(
    not DB_URL,
    reason="QEC_TEST_DATABASE_URL not set — the fold/planner round-trip needs "
           "a disposable Postgres (QecBase tables are created in-test)",
)


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


@needs_db
def test_fold_recall_plan_reconcile_round_trip():
    asyncio.run(_run_round_trip())


async def _run_round_trip():
    from app.db.journey_models import (
        JourneyBranchRow, JourneyEdgeRow, JourneyNodeRow, JourneyRow,
        JourneyTraversalRow)
    from app.db.models import QecBase

    engine = create_async_engine(DB_URL)
    async with engine.begin() as conn:
        await conn.run_sync(QecBase.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant = f"qec-jg-{uuid.uuid4().hex[:10]}"
    app_id = "app1"
    originals = (journey_fold.tenant_scoped_qec_session,
                 branch_planner.tenant_scoped_qec_session)
    try:
        journey_fold.tenant_scoped_qec_session = (
            lambda tid: _scoped(factory, tid))
        branch_planner.tenant_scoped_qec_session = (
            lambda tid: _scoped(factory, tid))

        coverage = _coverage(_flow(
            outcomes=[{"label": "Monthly Premium", "value": "$42.10",
                       "value_type": "currency"}]))
        r1 = await journey_fold.fold_crawl(
            tenant_id=tenant, app_id=app_id, exploration_id="ex1",
            coverage=coverage)
        assert r1["journeys"] == 1 and r1["traversals"] == 1
        assert r1["nodes"] == 2 and r1["edges"] == 1
        assert r1["branches"] == 2  # smoker + non-smoker

        # IDEMPOTENT: same crawl re-folded is a no-op.
        r2 = await journey_fold.fold_crawl(
            tenant_id=tenant, app_id=app_id, exploration_id="ex1",
            coverage=coverage)
        assert r2["traversals"] == 0 and r2["nodes"] == 0 and r2["edges"] == 0

        async with _scoped(factory, tenant) as s:
            walked = (await s.execute(select(JourneyBranchRow).where(
                JourneyBranchRow.option_label_norm == "non-smoker"))).scalar_one()
            other = (await s.execute(select(JourneyBranchRow).where(
                JourneyBranchRow.option_label_norm == "smoker"))).scalar_one()
            assert walked.status == BRANCH_WALKED
            assert other.status == BRANCH_DISCOVERED
            node = (await s.execute(select(JourneyNodeRow).where(
                JourneyNodeRow.fingerprint == "fpA"))).scalar_one()
            assert node.is_decision is True
            terminal_node = (await s.execute(select(JourneyNodeRow).where(
                JourneyNodeRow.fingerprint == "fpB"))).scalar_one()
            assert terminal_node.is_boundary is True
            assert terminal_node.has_outcome is True
            edge = (await s.execute(select(JourneyEdgeRow))).scalar_one()
            assert edge.trigger_label_norm == "continue"
            assert edge.walk_count == 1  # idempotent re-fold did not bump

        # PLANNER: the discovered smoker branch becomes one plan.
        plans = await branch_planner.plan_walks(tenant_id=tenant, app_id=app_id)
        assert len(plans) == 1
        assert plans[0]["choice_overrides"] == {"sig-smoke": "smoker"}
        await branch_planner.mark_planned(
            tenant_id=tenant, branch_ids=plans[0]["branch_ids"])
        async with _scoped(factory, tenant) as s:
            assert (await s.execute(select(JourneyBranchRow).where(
                JourneyBranchRow.option_label_norm == "smoker"
            ))).scalar_one().status == BRANCH_PLANNED

        # The planned walk comes back having WALKED the smoker branch:
        walked_flow = _flow()
        walked_flow["steps"][0]["decision_points"][0]["choice"] = "smoker"
        r3 = await journey_fold.fold_crawl(
            tenant_id=tenant, app_id=app_id, exploration_id="ex2",
            coverage=_coverage(walked_flow),
            identity_ref="synthetic+planned:abc")
        assert r3["traversals"] == 1
        rec = await branch_planner.reconcile_completion(
            tenant_id=tenant, app_id=app_id,
            walk_plan={"branch_ids": plans[0]["branch_ids"]},
            terminal_reason="completed")
        assert rec == {"walked": 1, "blocked": 0}

        # A plan that never reaches its option ends BLOCKED with a reason.
        async with _scoped(factory, tenant) as s:
            s.add(JourneyBranchRow(
                branch_id="b-unreach", tenant_id=tenant, app_id=app_id,
                node_fp="fpA", control_signature="sig-tier",
                control_label_norm="coverage tier",
                option_label_norm="platinum", status=BRANCH_PLANNED))
        rec2 = await branch_planner.reconcile_completion(
            tenant_id=tenant, app_id=app_id,
            walk_plan={"branch_ids": ["b-unreach"]},
            terminal_reason="budget_exhausted")
        assert rec2 == {"walked": 0, "blocked": 1}
        async with _scoped(factory, tenant) as s:
            blocked = (await s.execute(select(JourneyBranchRow).where(
                JourneyBranchRow.branch_id == "b-unreach"))).scalar_one()
            assert blocked.status == BRANCH_BLOCKED
            assert "budget_exhausted" in blocked.blocked_reason

        # Tenant isolation: another tenant sees nothing.
        other_tenant = f"qec-jg-other-{uuid.uuid4().hex[:8]}"
        assert await branch_planner.plan_walks(
            tenant_id=other_tenant, app_id=app_id) == []
    finally:
        journey_fold.tenant_scoped_qec_session = originals[0]
        branch_planner.tenant_scoped_qec_session = originals[1]
        await engine.dispose()


# ── E1: systematic enumeration — one plan per option ────────────────────

def _flow_9_options(entry_fp="fpX"):
    """A single page with 9 HLQ options and a terminal."""
    options = [f"option-{i}" for i in range(9)]
    return {
        "flow_id": "x" * 24, "entry_fingerprint": entry_fp,
        "entry_url": "https://a.example/hlq", "entry_title": "Product Select",
        "terminal": "submit_boundary", "completed": True, "fully_answered": True,
        "outcome_values": [{"label": "Premium", "value": "$100",
                            "value_type": "currency"}],
        "steps": [
            {"fingerprint": "fpX", "url": "u1", "title": "HLQ",
             "fields_filled": 1, "fields_unfilled": 0,
             "advance": {"tier": 1, "control_name": "Continue", "oracle": False},
             "decision_points": [
                 {"control_signature": "sig-hlq", "control_label": "Product",
                  "options": options, "choice": "option-0",
                  "provenance": "synthesized"}]},
            {"fingerprint": "fpY", "url": "u2", "title": "Review",
             "fields_filled": 0, "fields_unfilled": 0},
        ],
    }


@needs_db
def test_e1_nine_options_yield_separate_plans():
    """E1: a 9-option page should yield 8 plans (one per unchosen option)."""
    asyncio.run(_run_e1_nine_options())


async def _run_e1_nine_options():
    from app.db.journey_models import JourneyBranchRow
    from app.db.models import QecBase

    engine = create_async_engine(DB_URL)
    async with engine.begin() as conn:
        await conn.run_sync(QecBase.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant = f"qec-e1-{uuid.uuid4().hex[:10]}"
    app_id = "app-e1"
    originals = (journey_fold.tenant_scoped_qec_session,
                 branch_planner.tenant_scoped_qec_session)
    try:
        journey_fold.tenant_scoped_qec_session = (
            lambda tid: _scoped(factory, tid))
        branch_planner.tenant_scoped_qec_session = (
            lambda tid: _scoped(factory, tid))

        coverage = _coverage(_flow_9_options())
        r = await journey_fold.fold_crawl(
            tenant_id=tenant, app_id=app_id, exploration_id="ex-e1",
            coverage=coverage)
        assert r["branches"] == 9  # 1 walked + 8 discovered

        plans = await branch_planner.plan_walks(
            tenant_id=tenant, app_id=app_id, limit=20)
        assert len(plans) == 8
        overrides_set = {list(p["choice_overrides"].values())[0] for p in plans}
        assert overrides_set == {f"option-{i}" for i in range(1, 9)}
        for p in plans:
            assert len(p["branch_ids"]) == 1
            assert len(p["choice_overrides"]) == 1

    finally:
        journey_fold.tenant_scoped_qec_session = originals[0]
        branch_planner.tenant_scoped_qec_session = originals[1]
        await engine.dispose()


@needs_db
def test_e1_explosion_cap_defers_excess():
    """E1 explosion control: branches beyond the per-journey cap are deferred."""
    asyncio.run(_run_e1_explosion_cap())


async def _run_e1_explosion_cap():
    from app.config import settings
    from app.db.journey_models import JourneyBranchRow
    from app.db.models import QecBase

    engine = create_async_engine(DB_URL)
    async with engine.begin() as conn:
        await conn.run_sync(QecBase.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant = f"qec-e1cap-{uuid.uuid4().hex[:10]}"
    app_id = "app-e1cap"
    originals = (journey_fold.tenant_scoped_qec_session,
                 branch_planner.tenant_scoped_qec_session)
    old_cap = settings.journey_path_enum_cap
    try:
        journey_fold.tenant_scoped_qec_session = (
            lambda tid: _scoped(factory, tid))
        branch_planner.tenant_scoped_qec_session = (
            lambda tid: _scoped(factory, tid))
        settings.journey_path_enum_cap = 3

        coverage = _coverage(_flow_9_options())
        await journey_fold.fold_crawl(
            tenant_id=tenant, app_id=app_id, exploration_id="ex-cap",
            coverage=coverage)

        plans = await branch_planner.plan_walks(
            tenant_id=tenant, app_id=app_id, limit=20)
        assert len(plans) == 3

        async with _scoped(factory, tenant) as s:
            deferred = (await s.execute(select(JourneyBranchRow).where(
                JourneyBranchRow.tenant_id == tenant,
                JourneyBranchRow.status == BRANCH_DEFERRED,
            ))).scalars().all()
            assert len(deferred) == 5  # 8 discovered - 3 capped = 5 deferred
            for b in deferred:
                assert "deferred" in b.blocked_reason
                assert "cap" in b.blocked_reason

    finally:
        settings.journey_path_enum_cap = old_cap
        journey_fold.tenant_scoped_qec_session = originals[0]
        branch_planner.tenant_scoped_qec_session = originals[1]
        await engine.dispose()


def _decision_blocked_flow(entry_fp="fpBlocked"):
    """The VKPower shape: a page whose ONLY control is an unanswered business
    decision.  The crawl may not pick an insurance type for the client, so the
    advance clicks Continue, the page validates and stays put, and the walk
    ends ``loop`` / ``completed=False``.  This traversal can never become
    completed until one of its own options is forced."""
    return {
        "flow_id": "d" * 24, "entry_fingerprint": entry_fp,
        "entry_url": "https://a.example/life-insurance/quote/start/",
        "entry_title": "Get a Quote",
        "terminal": "loop", "completed": False, "fully_answered": False,
        "outcome_values": [],
        "steps": [
            {"fingerprint": entry_fp, "url": "u1", "title": "Choose coverage",
             "fields_filled": 0, "fields_unfilled": 3,
             "decision_points": [
                 {"control_signature": "sig-product",
                  "control_label": "Insurance type",
                  "options": ["term life", "whole life", "universal life"],
                  "choice": "", "provenance": "needs_input"}]},
        ],
    }


@needs_db
def test_decision_blocked_traversal_still_yields_plans():
    """A journey stopped at an unmade decision must still be plannable.

    Planning only off COMPLETED traversals deadlocks the exact case branch
    walking exists to break — the journey cannot complete until an option is
    forced, and no option is forced until it completes.  Regression guard for
    the VKPower quote funnel that stalled at one state."""
    asyncio.run(_run_decision_blocked())


async def _run_decision_blocked():
    from app.db.journey_models import JourneyTraversalRow
    from app.db.models import QecBase

    engine = create_async_engine(DB_URL)
    async with engine.begin() as conn:
        await conn.run_sync(QecBase.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant = f"qec-blocked-{uuid.uuid4().hex[:10]}"
    app_id = "app-blocked"
    originals = (journey_fold.tenant_scoped_qec_session,
                 branch_planner.tenant_scoped_qec_session)
    try:
        journey_fold.tenant_scoped_qec_session = (
            lambda tid: _scoped(factory, tid))
        branch_planner.tenant_scoped_qec_session = (
            lambda tid: _scoped(factory, tid))

        await journey_fold.fold_crawl(
            tenant_id=tenant, app_id=app_id, exploration_id="ex-blocked",
            coverage=_coverage(_decision_blocked_flow()))

        # The traversal really is the deadlock shape: not completed, and
        # terminal says the funnel REFUSED to advance.
        async with _scoped(factory, tenant) as s:
            tr = (await s.execute(select(JourneyTraversalRow))).scalar_one()
            assert tr.completed is False
            assert tr.terminal == "loop"

        # ...and it is still plannable: one plan per unchosen option.
        plans = await branch_planner.plan_walks(
            tenant_id=tenant, app_id=app_id, limit=20)
        assert len(plans) == 3, f"deadlocked: {len(plans)} plans"
        assert {list(p["choice_overrides"].values())[0] for p in plans} == {
            "term life", "whole life", "universal life"}
    finally:
        journey_fold.tenant_scoped_qec_session = originals[0]
        branch_planner.tenant_scoped_qec_session = originals[1]
        await engine.dispose()


@needs_db
def test_broken_run_terminals_are_not_plannable():
    """The carve-out is NARROW: a walk that was cut short (budget, cancel) or
    whose advance was honestly UNKNOWN (oracle_unavailable) is a fragment, not
    a finding — planning off it would launder an unknown into a claim."""
    asyncio.run(_run_broken_terminals_excluded())


async def _run_broken_terminals_excluded():
    from app.db.models import QecBase

    engine = create_async_engine(DB_URL)
    async with engine.begin() as conn:
        await conn.run_sync(QecBase.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    originals = (journey_fold.tenant_scoped_qec_session,
                 branch_planner.tenant_scoped_qec_session)
    try:
        journey_fold.tenant_scoped_qec_session = (
            lambda tid: _scoped(factory, tid))
        branch_planner.tenant_scoped_qec_session = (
            lambda tid: _scoped(factory, tid))

        for terminal in ("budget_exhausted", "cancelled", "oracle_unavailable"):
            tenant = f"qec-excl-{uuid.uuid4().hex[:10]}"
            flow = _decision_blocked_flow()
            flow["terminal"] = terminal
            await journey_fold.fold_crawl(
                tenant_id=tenant, app_id="app-excl",
                exploration_id=f"ex-{terminal}", coverage=_coverage(flow))
            plans = await branch_planner.plan_walks(
                tenant_id=tenant, app_id="app-excl", limit=20)
            assert plans == [], f"{terminal} must not be plannable"
    finally:
        journey_fold.tenant_scoped_qec_session = originals[0]
        branch_planner.tenant_scoped_qec_session = originals[1]
        await engine.dispose()
