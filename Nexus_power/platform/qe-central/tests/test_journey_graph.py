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

from app.config import settings
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
def test_next_action_fork_classifies_branches_and_blocks_the_unsafe_ones():
    asyncio.run(_run_next_action_classification())


async def _run_next_action_classification():
    """A next-action fork (Apply Now / Start Over / Back to Dashboard) folds into
    three branches, each classified: the forward option is DISCOVERED (walkable
    under Phase-B approval), the destructive and navigational options are BLOCKED
    with a reason — so branch_planner (which only plans discovered branches) never
    queues a walk that clicks 'Start Over' and wipes the quote."""
    from app.db.journey_models import JourneyBranchRow
    from app.db.models import QecBase

    engine = create_async_engine(DB_URL)
    async with engine.begin() as conn:
        await conn.run_sync(QecBase.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant = f"qec-na-{uuid.uuid4().hex[:10]}"
    app_id = "na-app"
    originals = (journey_fold.tenant_scoped_qec_session,
                 branch_planner.tenant_scoped_qec_session)
    try:
        journey_fold.tenant_scoped_qec_session = lambda tid: _scoped(factory, tid)
        branch_planner.tenant_scoped_qec_session = lambda tid: _scoped(factory, tid)

        flow = {
            "flow_id": "n" * 24, "entry_fingerprint": "review",
            "entry_url": "https://a.example/quote/review", "entry_title": "Review",
            "terminal": "submit_boundary", "completed": True, "fully_answered": True,
            "outcome_values": [{"label": "Monthly Premium", "value": "$4.68",
                                "value_type": "currency"}],
            "steps": [{
                "fingerprint": "review", "url": "u1", "title": "Your Quote Summary",
                "fields_filled": 0, "fields_unfilled": 0,
                "decision_points": [{
                    "control_signature": "nextaction:abc",
                    "control_label": "Next action",
                    "options": ["Apply Now", "Start Over", "Back to Dashboard"],
                    "provenance": "next_action",
                    "option_classes": {
                        "Apply Now": "forward",
                        "Start Over": "destructive",
                        "Back to Dashboard": "navigational",
                    },
                }],
            }],
        }
        r = await journey_fold.fold_crawl(
            tenant_id=tenant, app_id=app_id, exploration_id="ex-na",
            coverage=_coverage(flow))
        assert r["branches"] == 3

        async with _scoped(factory, tenant) as s:
            by = {b.option_label_norm: b for b in (await s.execute(
                select(JourneyBranchRow))).scalars().all()}
            assert by["apply now"].status == BRANCH_DISCOVERED
            assert by["start over"].status == BRANCH_BLOCKED
            assert "destructive" in by["start over"].blocked_reason
            assert by["back to dashboard"].status == BRANCH_BLOCKED
            assert "navigational" in by["back to dashboard"].blocked_reason

        # The planner plans ONLY the forward branch — never Start Over / Back.
        plans = await branch_planner.plan_walks(tenant_id=tenant, app_id=app_id)
        planned_opts = {v for p in plans for v in p["choice_overrides"].values()}
        assert "start over" not in planned_opts
        assert "back to dashboard" not in planned_opts
    finally:
        journey_fold.tenant_scoped_qec_session = originals[0]
        branch_planner.tenant_scoped_qec_session = originals[1]
        await engine.dispose()


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

        # PROBE FIRST. One option is already walked, so the planner adds only
        # enough representatives to reach branch_probe_k and then waits for
        # evidence — queueing all 8 up front is what let a 23-option state
        # picker run for hours before anyone knew it changed nothing.
        plans = await branch_planner.plan_walks(
            tenant_id=tenant, app_id=app_id, limit=20)
        assert 1 <= len(plans) <= settings.branch_probe_k, len(plans)
        for p in plans:
            assert len(p["branch_ids"]) == 1
            assert len(p["choice_overrides"]) == 1

        # ...and the cap LIFTS once the representatives are shown to fork, so a
        # genuine business decision is still enumerated in full. Nothing is
        # retired here (the walks produced different outcomes), so every
        # remaining option must come back plannable.
        async with _scoped(factory, tenant) as s:
            probes = (await s.execute(select(JourneyBranchRow).where(
                JourneyBranchRow.tenant_id == tenant,
                JourneyBranchRow.status == BRANCH_DISCOVERED,
            ).limit(settings.branch_probe_k))).scalars().all()
            for i, b in enumerate(probes):
                b.status = BRANCH_WALKED
                b.walked_in_traversal = f"trav-distinct-{i}"

        plans2 = await branch_planner.plan_walks(
            tenant_id=tenant, app_id=app_id, limit=20)
        walked_opts = {"option-0"} | {b.option_label_norm for b in probes}
        assert {list(p["choice_overrides"].values())[0] for p in plans2} == (
            {f"option-{i}" for i in range(9)} - walked_opts)

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

        # Probe-limited per decision; the DEFERRAL below is what this test is
        # about — excess beyond the per-journey enumeration cap must be recorded
        # honestly rather than silently dropped.
        plans = await branch_planner.plan_walks(
            tenant_id=tenant, app_id=app_id, limit=20)
        assert 1 <= len(plans) <= settings.branch_probe_k, len(plans)

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
            tr = (await s.execute(select(JourneyTraversalRow).where(
                JourneyTraversalRow.tenant_id == tenant))).scalar_one()
            assert tr.completed is False
            assert tr.terminal == "loop"

        # ...and it is still plannable: one plan per unchosen option.
        plans = await branch_planner.plan_walks(
            tenant_id=tenant, app_id=app_id, limit=20)
        # Probe-limited on the first cycle (see branch_probe_k); the point of
        # this test is that it is plannable AT ALL, not how many at once.
        assert plans, "deadlocked: no plans"
        assert len(plans) <= settings.branch_probe_k
        assert {list(p["choice_overrides"].values())[0] for p in plans} <= {
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


def _radio_group_flow(entry_fp="fpRG"):
    """A 3-option radio group as the explorer reports it: THREE decision-point
    records (one per element), all naming the same question via ``group_id``,
    each enumerating the same three answers. Keyed naively that is 3x3 = 9
    phantom branches; keyed on the question it is 3."""
    opts = ["term life", "whole life", "universal life"]
    gid = "g" * 32
    return {
        "flow_id": "r" * 24, "entry_fingerprint": entry_fp,
        "entry_url": "https://a.example/quote/start/", "entry_title": "Quote",
        "terminal": "submit_boundary", "completed": True, "fully_answered": True,
        "outcome_values": [],
        "steps": [
            {"fingerprint": entry_fp, "url": "u1", "title": "Product",
             "fields_filled": 1, "fields_unfilled": 2,
             "advance": {"tier": 1, "control_name": "Continue", "oracle": False},
             "decision_points": [
                 {"control_signature": f"sig-{i}", "control_label": label,
                  "group_id": gid, "options": opts,
                  "provenance": "planned",
                  **({"choice": "whole life"} if label == "whole life" else {})}
                 for i, label in enumerate(opts)]},
            {"fingerprint": "fpRG2", "url": "u2", "title": "Coverage",
             "fields_filled": 0, "fields_unfilled": 0},
        ],
    }


@needs_db
def test_radio_group_folds_to_one_decision_not_a_cross_product():
    """Regression: VKPower's 4-card product picker folded to 16 branches (4
    elements x 4 options) instead of 4, because each member was treated as its
    own independent decision."""
    asyncio.run(_run_radio_group_fold())


async def _run_radio_group_fold():
    from app.db.journey_models import JourneyBranchRow
    from app.db.models import QecBase

    engine = create_async_engine(DB_URL)
    async with engine.begin() as conn:
        await conn.run_sync(QecBase.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant = f"qec-rg-{uuid.uuid4().hex[:10]}"
    app_id = "app-rg"
    originals = (journey_fold.tenant_scoped_qec_session,
                 branch_planner.tenant_scoped_qec_session)
    try:
        journey_fold.tenant_scoped_qec_session = (
            lambda tid: _scoped(factory, tid))
        branch_planner.tenant_scoped_qec_session = (
            lambda tid: _scoped(factory, tid))

        r = await journey_fold.fold_crawl(
            tenant_id=tenant, app_id=app_id, exploration_id="ex-rg",
            coverage=_coverage(_radio_group_flow()))
        assert r["branches"] == 3, f"expected 3 branches, got {r['branches']}"

        async with _scoped(factory, tenant) as s:
            rows = (await s.execute(select(JourneyBranchRow).where(
                JourneyBranchRow.tenant_id == tenant))).scalars().all()
            assert len({b.control_signature for b in rows}) == 1, (
                "three elements are ONE question")
            assert {b.option_label_norm for b in rows} == {
                "term life", "whole life", "universal life"}
            walked = [b for b in rows if b.status == BRANCH_WALKED]
            assert [b.option_label_norm for b in walked] == ["whole life"], (
                "exactly the chosen answer is walked")

        # ...and the planner offers the two answers nobody took.
        plans = await branch_planner.plan_walks(
            tenant_id=tenant, app_id=app_id, limit=20)
        offered = {list(p["choice_overrides"].values())[0] for p in plans}
        assert offered, "the unwalked answers must still be offered"
        assert offered <= {"term life", "universal life"}, offered
        assert len(offered) <= settings.branch_probe_k
        # The override is keyed on the QUESTION, so a walk can force it without
        # having to guess which of the three elements owns the answer.
        assert all(list(p["choice_overrides"].keys())[0] == "g" * 32
                   for p in plans)
    finally:
        journey_fold.tenant_scoped_qec_session = originals[0]
        branch_planner.tenant_scoped_qec_session = originals[1]
        await engine.dispose()


@needs_db
def test_busy_dispatch_requeues_the_option_instead_of_retiring_it():
    """409 back-pressure is not a finding.

    ``blocked`` means "the walk ran and did not reach this option". A dispatch
    the single-flight explorer REFUSED produced no walk at all, so retiring the
    option would silently cap coverage at one per cycle. Observed live: a 4-plan
    cycle burned 3 options per round on back-pressure alone."""
    asyncio.run(_run_unmark_planned())


async def _run_unmark_planned():
    from app.db.journey_models import JourneyBranchRow
    from app.db.models import QecBase

    engine = create_async_engine(DB_URL)
    async with engine.begin() as conn:
        await conn.run_sync(QecBase.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant = f"qec-busy-{uuid.uuid4().hex[:10]}"
    app_id = "app-busy"
    originals = (journey_fold.tenant_scoped_qec_session,
                 branch_planner.tenant_scoped_qec_session)
    try:
        journey_fold.tenant_scoped_qec_session = (
            lambda tid: _scoped(factory, tid))
        branch_planner.tenant_scoped_qec_session = (
            lambda tid: _scoped(factory, tid))

        await journey_fold.fold_crawl(
            tenant_id=tenant, app_id=app_id, exploration_id="ex-busy",
            coverage=_coverage(_radio_group_flow()))
        plans = await branch_planner.plan_walks(
            tenant_id=tenant, app_id=app_id, limit=20)
        assert plans, "need a plan to requeue"
        ids = plans[0]["branch_ids"]

        assert await branch_planner.mark_planned(
            tenant_id=tenant, branch_ids=ids) == 1
        assert await branch_planner.unmark_planned(
            tenant_id=tenant, branch_ids=ids) == 1

        async with _scoped(factory, tenant) as s:
            row = (await s.execute(select(JourneyBranchRow).where(
                JourneyBranchRow.tenant_id == tenant,
                JourneyBranchRow.branch_id == ids[0]))).scalar_one()
            assert row.status == BRANCH_DISCOVERED, "must return to the backlog"

        # ...and it is offered again on the next cycle, not lost.
        again = await branch_planner.plan_walks(
            tenant_id=tenant, app_id=app_id, limit=20)
        assert ids[0] in {b for p in again for b in p["branch_ids"]}
    finally:
        journey_fold.tenant_scoped_qec_session = originals[0]
        branch_planner.tenant_scoped_qec_session = originals[1]
        await engine.dispose()


def test_walked_and_blocked_are_never_downgraded_by_requeue():
    """The requeue must only ever undo ``planned``. A walked option is a proof
    and a blocked one is a finding; neither may be silently reopened."""
    import inspect
    src = inspect.getsource(branch_planner.unmark_planned)
    assert "BRANCH_PLANNED" in src, "requeue must filter on planned only"
    assert "BRANCH_WALKED" not in src and "BRANCH_BLOCKED" not in src


def test_walk_depth_is_persisted_on_the_dispatched_plan():
    """The completion handler reads walk_depth back to decide whether to
    recurse. Dropping it from the stored plan made every branch walk look like a
    fresh depth-0 crawl, so the autowalk never terminated — it re-planned every
    ~2.5 minutes and burned the branch backlog."""
    import inspect

    from app.routers import explorations
    src = inspect.getsource(explorations._dispatch_explorer)
    assert 'pending_stats["walk_plan"]' in src
    assert '"walk_depth"' in src, (
        "walk_depth MUST be persisted on the stored walk_plan or the completion "
        "handler reads 0 forever and the autowalk never terminates")


# ── E2E means the WHOLE application, not a five-minute sample ────────────


class _AppRow:
    def __init__(self, schedule=None, budgets=None):
        self.schedule = schedule or {}
        self.budgets = budgets or {}


def test_e2e_never_inherits_the_first_pass_ceiling():
    """Regression: an app explicitly configured crawl_mode=e2e still got the
    40-state / depth-4 / 5-minute interactive ceiling because it had no per-app
    budget — and then reported a 'completed' crawl of a funnel it had seen one
    page of. E2E is a different promise: catalogue the whole application."""
    from app.routers.explorations import (
        _E2E_BUDGET, _FIRST_PASS_BUDGET, _resolve_crawl_mode)

    e2e = _AppRow(schedule={"crawl_mode": "e2e"})
    assert _resolve_crawl_mode(e2e, [], None) == "e2e"

    assert _E2E_BUDGET["max_states"] > _FIRST_PASS_BUDGET["max_states"] * 50
    assert _E2E_BUDGET["max_depth"] > _FIRST_PASS_BUDGET["max_depth"] * 5
    # The WALL is deliberately bounded rather than maximised — see
    # test_e2e_wall_budget_is_per_crawl_and_bounds_reaper_recovery. Coverage
    # comes from states/depth/requests and from crawls CHAINING, not from
    # letting one crawl run for hours.
    assert _E2E_BUDGET["max_wall_ms"] > _FIRST_PASS_BUDGET["max_wall_ms"]


def test_a_planned_branch_walk_is_always_e2e():
    """A branch walk exists to reach a path the default data would not take;
    running it under an explore-sized budget would strand it short of the very
    thing it was dispatched to prove."""
    from app.routers.explorations import _resolve_crawl_mode
    row = _AppRow(schedule={"crawl_mode": "explore"})
    assert _resolve_crawl_mode(row, [], {"branch_ids": ["b1"]}) == "e2e"


def test_non_e2e_modes_keep_the_fast_first_pass():
    """Explore/Target are the interactive 'show me tests now' flows and must NOT
    become 4-hour crawls — the ceiling is right for them."""
    from app.routers.explorations import _resolve_crawl_mode
    assert _resolve_crawl_mode(_AppRow(), [], None) == "explore"
    assert _resolve_crawl_mode(_AppRow(), ["/quote"], None) == "target"
    assert _resolve_crawl_mode(
        _AppRow(schedule={"crawl_mode": "target"}), [], None) == "target"


def test_autowalk_depth_is_a_backstop_not_a_coverage_policy():
    """The sweep must end because the branch backlog is empty, not because a
    counter ran out. A low cap silently reports 'complete' with options unwalked."""
    from app.config import settings
    assert settings.autowalk_max_depth >= 100, (
        "autowalk depth caps COVERAGE when set low — the terminator should be "
        "plan_walks() returning nothing")


def test_e2e_wall_budget_is_per_crawl_and_bounds_reaper_recovery():
    """max_wall_ms is PER CRAWL, not per sweep.

    The stale reaper grants an in-flight crawl its whole stamped wall before
    declaring it dead, so an over-generous wall IS the window during which a
    crawl whose explorer died holds the app's one-active-crawl slot with the
    Crawl button disabled. A 4h wall bricked the app for 4h; observed crawls
    take about a minute."""
    from app.routers.explorations import _E2E_BUDGET
    wall_min = _E2E_BUDGET["max_wall_ms"] / 60_000
    assert 10 <= wall_min <= 60, (
        f"{wall_min:.0f}min per crawl: under 10 truncates a slow site, over 60 "
        "leaves the app blocked too long when an explorer dies")


def test_e2e_still_lifts_the_coverage_dimensions():
    """Bounding the wall must not quietly re-cap COVERAGE — states, depth and
    requests are what decide how much of the app is catalogued."""
    from app.routers.explorations import _E2E_BUDGET, _FIRST_PASS_BUDGET
    for k in ("max_states", "max_depth", "max_requests"):
        assert _E2E_BUDGET[k] > _FIRST_PASS_BUDGET[k] * 10, k


# ── equivalence classes: a data variation is not a business path ─────────


def _variation_flow(entry_fp="fpV", option="alabama", premium="$42.10"):
    """One walk of a many-option decision (a US state picker). Every option
    produces the SAME page path and the SAME premium — it varies data, it does
    not fork the business."""
    return {
        "flow_id": ("v" + option)[:24].ljust(24, "x"),
        "entry_fingerprint": entry_fp,
        "entry_url": "https://a.example/quote/personal/", "entry_title": "Personal",
        "terminal": "submit_boundary", "completed": True, "fully_answered": True,
        "outcome_values": [{"label": "Monthly Premium", "value": premium,
                            "value_type": "currency"}],
        "steps": [
            {"fingerprint": entry_fp, "url": "u1", "title": "Personal",
             "fields_filled": 1, "fields_unfilled": 0,
             "advance": {"tier": 1, "control_name": "Continue", "oracle": False},
             "decision_points": [
                 {"control_signature": "sig-state", "control_label": "State",
                  "options": ["alabama", "alaska", "arizona", "arkansas"],
                  "choice": option, "provenance": "planned"}]},
            {"fingerprint": "fpV2", "url": "u2", "title": "Review",
             "fields_filled": 0, "fields_unfilled": 0},
        ],
    }


@needs_db
def test_options_that_change_nothing_are_retired_as_equivalent():
    """Regression: one 5-page form produced 113 branches — 23 US states, 13
    height-in-inches — each walked as its own crawl while holding a fleet-wide
    lock, then reported as 113 'proven business paths' where there are six."""
    asyncio.run(_run_equivalence())


async def _run_equivalence():
    from app.db.journey_models import JourneyBranchRow
    from app.db.models import QecBase
    from app.services.journey_fold import BRANCH_EQUIVALENT

    engine = create_async_engine(DB_URL)
    async with engine.begin() as conn:
        await conn.run_sync(QecBase.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant = f"qec-eq-{uuid.uuid4().hex[:10]}"
    app_id = "app-eq"
    originals = (journey_fold.tenant_scoped_qec_session,
                 branch_planner.tenant_scoped_qec_session)
    try:
        journey_fold.tenant_scoped_qec_session = lambda t: _scoped(factory, t)
        branch_planner.tenant_scoped_qec_session = lambda t: _scoped(factory, t)

        # two representatives walked, both identical in path AND premium
        for i, opt in enumerate(("alabama", "alaska")):
            await journey_fold.fold_crawl(
                tenant_id=tenant, app_id=app_id, exploration_id=f"ex-{i}",
                coverage=_coverage(_variation_flow(option=opt)))

        res = await branch_planner.classify_equivalent_options(
            tenant_id=tenant, app_id=app_id)
        assert res["retired"] >= 1, res

        async with _scoped(factory, tenant) as s:
            rows = (await s.execute(select(JourneyBranchRow).where(
                JourneyBranchRow.tenant_id == tenant))).scalars().all()
            walked = [b for b in rows if b.status == BRANCH_WALKED]
            equiv = [b for b in rows if b.status == BRANCH_EQUIVALENT]
            assert len(walked) == 2, [b.option_label_norm for b in walked]
            assert {b.option_label_norm for b in equiv} == {"arizona", "arkansas"}
            # honest, never silent
            assert all("equivalent" in b.blocked_reason for b in equiv)

        # ...and the planner stops queueing them
        plans = await branch_planner.plan_walks(
            tenant_id=tenant, app_id=app_id, limit=20)
        assert plans == [], plans
    finally:
        journey_fold.tenant_scoped_qec_session = originals[0]
        branch_planner.tenant_scoped_qec_session = originals[1]
        await engine.dispose()


@needs_db
def test_a_decision_that_really_forks_keeps_every_option_walkable():
    """The cap must LIFT when the representatives disagree. "Term Life vs Whole
    Life" produces a different premium, so every remaining product stays a real
    business path — pruning it would silently delete coverage."""
    asyncio.run(_run_real_fork_not_retired())


async def _run_real_fork_not_retired():
    from app.db.journey_models import JourneyBranchRow
    from app.db.models import QecBase
    from app.services.journey_fold import BRANCH_EQUIVALENT

    engine = create_async_engine(DB_URL)
    async with engine.begin() as conn:
        await conn.run_sync(QecBase.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant = f"qec-fork-{uuid.uuid4().hex[:10]}"
    app_id = "app-fork"
    originals = (journey_fold.tenant_scoped_qec_session,
                 branch_planner.tenant_scoped_qec_session)
    try:
        journey_fold.tenant_scoped_qec_session = lambda t: _scoped(factory, t)
        branch_planner.tenant_scoped_qec_session = lambda t: _scoped(factory, t)

        # same decision, DIFFERENT premium per option → a genuine fork
        for i, (opt, prem) in enumerate((("alabama", "$42.10"),
                                         ("alaska", "$99.99"))):
            await journey_fold.fold_crawl(
                tenant_id=tenant, app_id=app_id, exploration_id=f"ex-{i}",
                coverage=_coverage(_variation_flow(option=opt, premium=prem)))

        res = await branch_planner.classify_equivalent_options(
            tenant_id=tenant, app_id=app_id)
        assert res["retired"] == 0, res
        async with _scoped(factory, tenant) as s:
            rows = (await s.execute(select(JourneyBranchRow).where(
                JourneyBranchRow.tenant_id == tenant))).scalars().all()
            assert not [b for b in rows if b.status == BRANCH_EQUIVALENT]
    finally:
        journey_fold.tenant_scoped_qec_session = originals[0]
        branch_planner.tenant_scoped_qec_session = originals[1]
        await engine.dispose()


# ── liveness recovery: a dead crawl must not hold the FLEET's lock ────────


def test_liveness_is_fail_safe_and_never_reaps_a_running_crawl():
    """Reaping a HEALTHY crawl is far worse than recovering slowly, so the probe
    is deliberately asymmetric: any worker reporting the job wins, and anything
    inconclusive degrades to the old timeout."""
    import inspect

    from app.clients import explorer_client
    src = inspect.getsource(explorer_client.crawl_liveness)
    assert 'return "alive"' in src, "a reporting worker must win outright"
    assert 'return "unknown" if inconclusive else "dead"' in src, (
        "an unreachable worker must NOT be read as dead")


def test_reaper_requires_a_minimum_age_before_trusting_liveness():
    """A crawl the worker has not registered yet answers 404 and would look
    dead. Without an age floor the reaper would kill crawls it just dispatched."""
    import inspect

    from app.controlplane import reaper
    src = inspect.getsource(reaper._worker_says_dead)
    assert "max(60.0, grace_s)" in src, "there must be an age floor"
    assert 'verdict != "dead"' in src, "only a definitive dead may reap"
    assert "return False" in src.split("except")[-1], (
        "any liveness failure must fall back to the old timeout behaviour")


def test_crawl_id_is_persisted_so_liveness_can_be_asked_at_all():
    """The reaper probes by the worker's job id. Without it persisted there is
    nothing to ask, and recovery falls back to waiting out the wall budget —
    which for an E2E crawl is tens of minutes of fleet-wide outage."""
    import inspect

    from app.routers import explorations
    src = inspect.getsource(explorations._dispatch_explorer)
    assert '"crawl_id": crawl_id' in src


# ── a submit approval must not silently disable the funnel ───────────────

def test_approving_a_generic_advance_label_for_submit_is_refused():
    """Observed live: an app set submit_approvals=["Continue"], and its
    five-step quote funnel was recorded as five ONE-STEP journeys.

    The wizard skips any approved name because the Phase-B submit path owns it —
    correct — but "Continue" is the same label on step 2 as on step 5, so every
    step became unwalkable. No error was raised anywhere; the catalogue simply
    described a product that does not exist. The approval is a legitimate
    operator action; accepting it silently is the defect."""
    import pytest as _pytest
    from fastapi import HTTPException

    from app.routers.apps import _reject_advance_shadowing_approvals

    for label in ("Continue", "next", "PROCEED", "Submit "):
        if label.strip().lower() == "submit":
            continue  # 'submit' is a commit word, handled by the commit veto
        with _pytest.raises(HTTPException) as exc:
            _reject_advance_shadowing_approvals(
                {"allow_submit": True, "submit_approvals": [label]})
        assert exc.value.status_code == 422
        assert "unwalkable" in str(exc.value.detail)


def test_a_distinct_final_submit_label_is_accepted():
    """The fix must not block the legitimate case — a real terminal control."""
    from app.routers.apps import _reject_advance_shadowing_approvals
    for label in ("See My Quote", "Submit Application", "Place Order"):
        _reject_advance_shadowing_approvals(
            {"allow_submit": True, "submit_approvals": [label]})   # no raise


def test_absent_or_malformed_approvals_are_ignored():
    from app.routers.apps import _reject_advance_shadowing_approvals
    _reject_advance_shadowing_approvals({})
    _reject_advance_shadowing_approvals({"submit_approvals": None})
    _reject_advance_shadowing_approvals(None)


# ── naming needs signal that titles alone cannot give ────────────────────

def test_the_prompt_carries_the_questions_and_controls_of_the_journey():
    """Regression: every page of the VKPower SPA carries the SAME <title>, so
    the login journey and the quote journey produced byte-identical prompts and
    the model named the LOGIN flow "Get Life Insurance Quote". That was the only
    guess the input allowed — the input was starved, not the model wrong."""
    login = journey_naming.build_prompt(
        entry_title="VKPower Life Insurance",
        step_titles=["VKPower Life Insurance", "VKPower Life Insurance"],
        outcomes=[], terminal="submit_boundary",
        decisions=[], triggers=["sign in without pin", "continue"])
    quote = journey_naming.build_prompt(
        entry_title="VKPower Life Insurance",
        step_titles=["VKPower Life Insurance", "VKPower Life Insurance"],
        outcomes=[{"label": "Estimated Monthly Premium", "value_type": "currency"}],
        terminal="submit_boundary",
        decisions=["coverage amount", "term length"], triggers=["continue"])

    assert login != quote, "the two journeys must no longer look identical"
    assert "sign in without pin" in login
    assert "coverage amount" in quote and "Estimated Monthly Premium" in quote


def test_the_prompt_stays_url_free():
    """F1/F2 doctrine: no URL may enter the prompt, including via the new
    grounding — labels are product UI text, not addresses."""
    p = journey_naming.build_prompt(
        entry_title="Quote", step_titles=["Health"], outcomes=[],
        terminal="submit_boundary",
        decisions=["coverage amount"], triggers=["continue"])
    assert "http" not in p and "://" not in p


def test_grounding_is_optional_so_older_callers_are_unaffected():
    p = journey_naming.build_prompt(
        entry_title="Quote", step_titles=["Health"], outcomes=[],
        terminal="submit_boundary")
    assert "Questions this journey answered" not in p
    assert "Controls pressed" not in p


def test_an_ungroundable_journey_keeps_its_fallback_name():
    """Regression: a sign-in was catalogued as "Get Life Insurance Quote".

    With no decisions, no outcomes, and a <title> every page of the SPA shares,
    there is nothing to name the journey FROM — and the model answers anyway,
    because it always answers. On a life-insurance app the only guess available
    was the wrong one. An invented name in an evidence product is worse than a
    dull one, so an ungroundable journey keeps its honest fallback until a later
    crawl gives it a decision or an outcome."""
    import inspect
    src = inspect.getsource(journey_naming.name_unnamed_journeys)
    assert "if not decisions and not outcomes and len(distinct_titles) < 2:" in src
    assert "continue" in src.split("distinct_titles) < 2:")[1][:400]


def test_a_journey_with_an_outcome_is_still_named():
    """The floor must not block the case that matters: a journey that produced a
    premium has the strongest possible grounding."""
    import inspect
    src = inspect.getsource(journey_naming.name_unnamed_journeys)
    # outcomes present ⇒ the guard's `not outcomes` is False ⇒ naming proceeds
    assert "not outcomes" in src
