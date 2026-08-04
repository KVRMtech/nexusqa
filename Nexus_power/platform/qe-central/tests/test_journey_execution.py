"""Runnable Journeys (Release D) — linker matching law, dispatch honesty,
verdict fold-back, and the factory client extensions.

Laws under test:
  * matching is deterministic URL-path coverage; adoption requires SPANNING
    (entry AND terminal covered), not sampling;
  * every dispatch outcome is a ledger row — blocked dispatches carry the
    factory's reason, never a silent swallow;
  * the durable evidence key (ingested run) is resolved via ci_run_id;
  * a journey with no completed walk is honestly not-runnable.
"""
from __future__ import annotations

import asyncio
import os
import types
import uuid
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.clients import factory
from app.services import journey_case_linker as linker
from app.services import journey_runner as runner
from app.services.journey_case_linker import (
    KIND_JOURNEY_E2E,
    KIND_LINKED,
    case_paths,
    coverage_score,
    display_name_for,
    extraneous_steps,
    norm_path,
    spans_journey,
)


# ── Pure matching law ────────────────────────────────────────────────────

def test_norm_path_strips_host_query_and_trailing_slash():
    assert norm_path("https://a.example/quote/start/?x=1") == "/quote/start"
    assert norm_path("https://other-env.example/quote/start") == "/quote/start"
    assert norm_path("https://a.example/") == "/"
    assert norm_path("") == "/"


def test_case_paths_reads_observed_urls_in_order_deduped():
    case = {"steps": [
        {"observed": {"url": "https://a.example/quote/start"}},
        {"observed": {"url": "https://a.example/quote/start"}},
        {"observed": {"url": "https://a.example/quote/health"}},
        {"next_url": "https://a.example/quote/review"},
        {"observed": {}},
    ]}
    assert case_paths(case) == ["/quote/start", "/quote/health", "/quote/review"]
    assert case_paths(None) == []
    assert case_paths({"steps": "nope"}) == []


def test_coverage_score_is_journey_fraction():
    journey = ["/quote/start", "/quote/health", "/quote/review"]
    assert coverage_score(journey, journey) == 100
    assert coverage_score(journey, ["/quote/start", "/quote/health"]) == 67
    assert coverage_score(journey, ["/elsewhere"]) == 0
    assert coverage_score([], ["/x"]) == 0


def test_spanning_requires_entry_and_terminal():
    journey = ["/quote/start", "/quote/health", "/quote/review"]
    assert spans_journey(journey, ["/quote/start", "/quote/review"])
    assert not spans_journey(journey, ["/quote/start", "/quote/health"])
    assert not spans_journey(journey, ["/quote/health", "/quote/review"])
    assert not spans_journey([], ["/x"])


def test_display_name_is_business_named_f5():
    assert display_name_for("Get Life Insurance Quote", "ignored") == \
        "Verify Get Life Insurance Quote end to end"
    assert display_name_for("", "Quote Start") == \
        "Verify Quote Start end to end"


# ── Factory client extensions ────────────────────────────────────────────

def test_list_test_cases_paginates(monkeypatch):
    pages = {
        1: {"items": [{"test_case_id": "a"}, {"test_case_id": "b"}], "total": 3},
        2: {"items": [{"test_case_id": "c"}], "total": 3},
    }
    calls = []

    async def fake_call(*, method, path, endpoint, tenant_id, timeout_s,
                        json_body=None):
        calls.append(path)
        page = int(path.split("page=")[1].split("&")[0])
        return pages[page]

    monkeypatch.setattr(factory, "_call", fake_call)
    items = asyncio.run(factory.list_test_cases(
        tenant_id="t1", artifact_id="art1"))
    assert [i["test_case_id"] for i in items] == ["a", "b", "c"]
    assert len(calls) == 2


def test_run_cases_body_shape(monkeypatch):
    seen = {}

    async def fake_call(*, method, path, endpoint, tenant_id, timeout_s,
                        json_body=None):
        seen.update(method=method, path=path, body=json_body)
        return {"run_id": "r1", "status": "running"}

    monkeypatch.setattr(factory, "_call", fake_call)
    out = asyncio.run(factory.run_cases(
        tenant_id="t1", artifact_id="art1", test_ids=["case-1", " "]))
    assert out["run_id"] == "r1"
    assert seen["method"] == "POST"
    assert seen["path"].endswith("/art1/playwright/run")
    assert seen["body"] == {"test_ids": ["case-1"]}


def test_list_runs_unwraps_runs_key(monkeypatch):
    async def fake_call(**kw):
        return {"artifact_id": "a", "runs": [{"run_id": "x", "ci_run_id": "d"}]}

    monkeypatch.setattr(factory, "_call", fake_call)
    runs = asyncio.run(factory.list_runs(tenant_id="t1", artifact_id="a"))
    assert runs == [{"run_id": "x", "ci_run_id": "d"}]


# ── Dispatch honesty (DB faked; poller stubbed) ──────────────────────────

class _FakeSession:
    def __init__(self):
        self.added = []

    def add(self, row):
        self.added.append(row)


def _fake_scope(store):
    @asynccontextmanager
    async def scope(tenant_id):
        yield store
    return scope


def _dispatch(monkeypatch, *, run_result=None, error=None):
    store = _FakeSession()
    monkeypatch.setattr(runner, "tenant_scoped_qec_session", _fake_scope(store))
    spawned = []
    monkeypatch.setattr(runner, "_spawn_poller", lambda **kw: spawned.append(kw))

    async def fake_run_cases(**kw):
        if error is not None:
            raise error
        return run_result

    # The default dispatch path is the WATCHABLE run-live one; the headless
    # path is patched identically so a `live=False` caller behaves the same.
    monkeypatch.setattr(runner.factory, "run_cases_live", fake_run_cases)
    monkeypatch.setattr(runner.factory, "run_cases", fake_run_cases)
    out = asyncio.run(runner.dispatch_journey_run(
        tenant_id="t1", app_id="app1", journey_id="j1",
        artifact_id="art1", test_case_id="case-1", env_ref="uat"))
    return out, store, spawned


def test_dispatch_running_records_row_and_spawns_poller(monkeypatch):
    out, store, spawned = _dispatch(
        monkeypatch, run_result={"run_id": "disp-1", "status": "running"})
    assert out["status"] == "running"
    assert out["dispatch_run_id"] == "disp-1"
    row = store.added[0]
    assert row.status == "running" and row.dispatch_run_id == "disp-1"
    assert row.env_ref == "uat"
    assert len(spawned) == 1


def test_dispatch_blocked_body_is_an_honest_row(monkeypatch):
    out, store, spawned = _dispatch(
        monkeypatch, run_result={"status": "blocked",
                                 "blocked_reason": "member_data",
                                 "note": "3 fields missing"})
    assert out["status"] == "blocked"
    assert "member_data" in out["blocked_reason"]
    assert "3 fields missing" in store.added[0].blocked_reason
    assert spawned == []


def test_dispatch_factory_rejection_is_an_honest_row(monkeypatch):
    out, store, spawned = _dispatch(
        monkeypatch, error=factory.FactoryClientError(409, "quarantined"))
    assert out["status"] == "blocked"
    assert "409" in out["blocked_reason"]
    assert "quarantined" in store.added[0].blocked_reason
    assert spawned == []


# ── DB round-trip (skipif-gated, house pattern) ──────────────────────────

DB_URL = os.environ.get("QEC_TEST_DATABASE_URL", "")
needs_db = pytest.mark.skipif(
    not DB_URL,
    reason="QEC_TEST_DATABASE_URL not set — linker/runner round-trip needs a "
           "disposable Postgres (QecBase tables are created in-test)",
)


@asynccontextmanager
async def _scoped(factory_, tenant):
    session = factory_()
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


@needs_db
def test_link_adopt_run_foldback_round_trip():
    asyncio.run(_run_round_trip())


async def _run_round_trip():
    from app.db.journey_models import (
        JourneyNodeRow, JourneyRow, JourneyTraversalRow)
    from app.db.journey_run_models import JourneyCaseRow, JourneyRunRow
    from app.db.models import QecBase

    engine = create_async_engine(DB_URL)
    async with engine.begin() as conn:
        await conn.run_sync(QecBase.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant = f"qec-jrun-{uuid.uuid4().hex[:10]}"
    app_id = "app1"
    originals = (linker.tenant_scoped_qec_session,
                 runner.tenant_scoped_qec_session)
    try:
        linker.tenant_scoped_qec_session = lambda t: _scoped(session_factory, t)
        runner.tenant_scoped_qec_session = lambda t: _scoped(session_factory, t)

        # Seed a completed journey: start → health → review.
        async with _scoped(session_factory, tenant) as s:
            s.add(JourneyRow(
                journey_id="j1", tenant_id=tenant, app_id=app_id,
                entry_fingerprint="fpA", flow_id="f" * 24,
                entry_url="https://a.example/quote/start",
                entry_title="Quote", business_name="Get Life Insurance Quote",
                name_source="agent"))
            for fp, url in (("fpA", "https://a.example/quote/start"),
                            ("fpB", "https://a.example/quote/health"),
                            ("fpC", "https://a.example/quote/review")):
                s.add(JourneyNodeRow(
                    node_id=f"n-{fp}", tenant_id=tenant, app_id=app_id,
                    fingerprint=fp, url=url, title=fp))
            s.add(JourneyTraversalRow(
                traversal_id="t1", tenant_id=tenant, app_id=app_id,
                journey_id="j1", exploration_id="ex1",
                terminal="submit_boundary", completed=True,
                path_fps=["fpA", "fpB", "fpC"], path_hash="h1"))

        # Factory returns one spanning case and one sampling case.
        async def fake_cases(**kw):
            return [
                {"test_case_id": "case-span", "name": "quote flow",
                 "test_case": {"steps": [
                     {"observed": {"url": "https://a.example/quote/start"}},
                     {"observed": {"url": "https://a.example/quote/health"}},
                     {"observed": {"url": "https://a.example/quote/review"}},
                 ]}},
                {"test_case_id": "case-sample", "name": "start only",
                 "test_case": {"steps": [
                     {"observed": {"url": "https://a.example/quote/start"}},
                     {"observed": {"url": "https://a.example/quote/health"}},
                 ]}},
            ]

        original_list = linker.factory.list_test_cases
        linker.factory.list_test_cases = fake_cases
        try:
            report = await linker.link_app_journeys(
                tenant_id=tenant, app_id=app_id, artifact_id="art1")
            # Idempotent re-link.
            report2 = await linker.link_app_journeys(
                tenant_id=tenant, app_id=app_id, artifact_id="art1")
        finally:
            linker.factory.list_test_cases = original_list
        assert report["adopted"] == 1 and report["linked"] == 2
        assert report2["linked"] == 0  # nothing new on re-link

        async with _scoped(session_factory, tenant) as s:
            span = (await s.execute(select(JourneyCaseRow).where(
                JourneyCaseRow.test_case_id == "case-span"))).scalar_one()
            sample = (await s.execute(select(JourneyCaseRow).where(
                JourneyCaseRow.test_case_id == "case-sample"))).scalar_one()
            assert span.kind == KIND_JOURNEY_E2E
            assert span.coverage_score == 100
            assert span.display_name == \
                "Verify Get Life Insurance Quote end to end"
            assert sample.kind == KIND_LINKED and sample.coverage_score == 67
            adopted = await linker.runnable_case(
                s, tenant_id=tenant, app_id=app_id, journey_id="j1",
                artifact_id="art1")
            assert adopted is not None and adopted.test_case_id == "case-span"

        # Runner fold-back: terminal passed + ingested id via ci_run_id.
        async with _scoped(session_factory, tenant) as s:
            s.add(JourneyRunRow(
                journey_run_id="jr1", tenant_id=tenant, app_id=app_id,
                journey_id="j1", artifact_id="art1", test_case_id="case-span",
                dispatch_run_id="disp-9", status="running"))

        async def fake_list_runs(**kw):
            return [{"run_id": "ingest-9", "ci_run_id": "disp-9",
                     "status": "completed", "total_steps": 8,
                     "passed_steps": 8, "failed_steps": 0,
                     "environment": "uat"}]

        original_runs = runner.factory.list_runs
        runner.factory.list_runs = fake_list_runs
        try:
            await runner._fold_back(
                tenant_id=tenant, journey_run_id="jr1", artifact_id="art1",
                dispatch_run_id="disp-9", status="passed",
                job={"exit_code": 0, "steps_completed": 8, "total_tests": 1})
        finally:
            runner.factory.list_runs = original_runs

        async with _scoped(session_factory, tenant) as s:
            row = (await s.execute(select(JourneyRunRow).where(
                JourneyRunRow.journey_run_id == "jr1"))).scalar_one()
            assert row.status == "passed"
            assert row.ingested_run_id == "ingest-9"
            assert row.verdict_summary["passed_steps"] == 8
            assert row.finished_at is not None
    finally:
        linker.tenant_scoped_qec_session = originals[0]
        runner.tenant_scoped_qec_session = originals[1]
        await engine.dispose()


# ── Adoption tightness (Release D-P live refinement) ─────────────────────

def test_extraneous_counts_pages_outside_the_journey():
    journey = ["/quote/start", "/quote/review"]
    assert extraneous_steps(journey, ["/quote/start", "/quote/review"]) == 0
    assert extraneous_steps(journey, ["/quote/start", "/quote/review", "/login"]) == 1
    assert extraneous_steps(journey, ["/a", "/b"]) == 2


def test_tightest_spanning_case_is_adopted(monkeypatch):
    """Observed live: a quote-journey case that spanned the funnel but walked
    on to a 'Member Sign In' click failed THERE — red-flagging a claim the
    journey never made. Same coverage ⇒ the case with the fewest foreign
    pages wins."""
    journey = ["/quote/start", "/quote/review"]
    wanderer = ["/quote/start", "/quote/review", "/login"]
    tight = ["/quote/start", "/quote/review"]
    scored = [
        (coverage_score(journey, wanderer), spans_journey(journey, wanderer),
         extraneous_steps(journey, wanderer), {"test_case_id": "wanderer"}),
        (coverage_score(journey, tight), spans_journey(journey, tight),
         extraneous_steps(journey, tight), {"test_case_id": "tight"}),
    ]
    scored.sort(key=lambda m: (-int(m[1]), -m[0], m[2]))
    assert scored[0][3]["test_case_id"] == "tight"


# ── Live run window (defect 1) ───────────────────────────────────────────

def test_dispatch_uses_the_live_path_and_keeps_the_viewer_url(monkeypatch):
    """A journey run must be WATCHABLE: dispatch goes through run-live and
    the noVNC viewer address is kept on the ledger row."""
    store = _FakeSession()
    monkeypatch.setattr(runner, "tenant_scoped_qec_session", _fake_scope(store))
    monkeypatch.setattr(runner, "_spawn_poller", lambda **kw: None)
    seen = {}

    async def fake_live(**kw):
        seen.update(kw)
        return {"run_id": "disp-live", "status": "running",
                "live_url": "https://vm:6080/vnc.html?token=x"}

    async def fake_headless(**kw):
        raise AssertionError("headless path must not be used by default")

    monkeypatch.setattr(runner.factory, "run_cases_live", fake_live)
    monkeypatch.setattr(runner.factory, "run_cases", fake_headless)
    out = asyncio.run(runner.dispatch_journey_run(
        tenant_id="t1", app_id="app1", journey_id="j1", artifact_id="art1",
        test_case_id="case-1"))
    assert out["live_url"].startswith("https://vm:6080")
    assert store.added[0].live_url == out["live_url"]
    assert seen["test_ids"] == ["case-1"]


def test_live_progress_reports_runner_counters_while_in_flight(monkeypatch):
    class _Row:
        journey_run_id = "jr1"; status = "running"; live_url = "https://vm/live"
        blocked_reason = ""; artifact_id = "art1"; dispatch_run_id = "disp-1"
        ingested_run_id = ""

    monkeypatch.setattr(runner, "tenant_scoped_qec_session",
                        _fake_scope(_FakeSession()))

    async def fake_latest(session, **kw):
        return _Row()

    async def fake_status(**kw):
        return {"status": "running", "steps_completed": 2, "total_tests": 1,
                "output_tail": "", "output": "running step 2"}

    monkeypatch.setattr(runner, "latest_run", fake_latest)
    monkeypatch.setattr(runner.factory, "run_status", fake_status)
    out = asyncio.run(runner.live_progress(
        tenant_id="t1", app_id="app1", journey_id="j1"))
    assert out["in_flight"] is True
    assert out["live_url"] == "https://vm/live"
    assert out["steps_completed"] == 2
    assert "running step 2" in out["output_tail"]


def test_live_progress_without_a_run_is_never_run(monkeypatch):
    monkeypatch.setattr(runner, "tenant_scoped_qec_session",
                        _fake_scope(_FakeSession()))

    async def none_latest(session, **kw):
        return None

    monkeypatch.setattr(runner, "latest_run", none_latest)
    out = asyncio.run(runner.live_progress(
        tenant_id="t1", app_id="app1", journey_id="j1"))
    assert out == {"status": "never_run", "live_url": "", "in_flight": False}
