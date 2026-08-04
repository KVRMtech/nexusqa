"""Advance memory — proven-only write-back, tenant-private recall, and the
consent-gated, value-free cross-tenant label pool.

Laws under test:
  * only PROVEN tier-3 advances are harvested (presence of step ``advance``
    evidence with ``oracle`` + ``signature`` IS the proof);
  * recall answers from the tenant's own memory before any LLM call;
  * contribution to the shared pool requires the tenant's explicit opt-in
    (OFF by default) and stores label patterns only — nothing
    tenant-identifying beyond a pseudonymous hash;
  * every path is best-effort: DB trouble degrades to "no recall", never to
    a blocked pick or a failed completion.
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

from app.services import advance_agent, advance_memory
from app.services.advance_memory import (
    _proven_oracle_advances,
    contributor_hash,
    normalize_label,
)


# ── Pure logic (no DB) ───────────────────────────────────────────────────

def test_normalize_label_collapses_case_and_whitespace():
    assert normalize_label("  See   My\tQuote ") == "see my quote"
    assert normalize_label("") == ""


def test_contributor_hash_is_pseudonymous_and_stable():
    h = contributor_hash("tenant-a")
    assert h == contributor_hash("tenant-a")
    assert h != contributor_hash("tenant-b")
    assert "tenant-a" not in h and len(h) == 16


def _coverage(steps):
    return {"flows": [{"steps": steps}]}


def test_harvest_extracts_only_oracle_advances_with_signature():
    steps = [
        {"advance": {"tier": 1, "control_name": "Continue", "oracle": False}},
        {"advance": {"tier": 3, "control_name": "See My Quote", "oracle": True,
                     "signature": "sig-a"}},
        {"advance": {"tier": 3, "control_name": "No Signature", "oracle": True}},
        {"title": "terminal step, no advance"},
    ]
    assert _proven_oracle_advances(_coverage(steps)) == [
        ("sig-a", "see my quote")]


def test_harvest_tolerates_malformed_coverage():
    assert _proven_oracle_advances(None) == []
    assert _proven_oracle_advances({"flows": "nope"}) == []
    assert _proven_oracle_advances({"flows": [{"steps": [None, 4, {}]}]}) == []


def test_recall_never_raises_without_db():
    """With no reachable database, recall degrades to None (best-effort)."""
    assert asyncio.run(advance_memory.recall("", "sig")) is None
    assert asyncio.run(advance_memory.recall_prior("", {"x"})) is None


# ── pick_advance recall integration (DB monkeypatched) ───────────────────

def _controls():
    return [{"name": "Pay Now", "kind": "button"},        # filtered (commit)
            {"name": "See My Quote", "kind": "button"},
            {"name": "Back", "kind": "button"}]


def _pick():
    return asyncio.run(advance_agent.pick_advance(
        tenant_id="t1", controls=_controls(),
        page_title="Health", page_url="https://a.example/q"))


def test_memory_hit_skips_the_llm(monkeypatch):
    async def hit(tenant_id, signature):
        return "see my quote"

    async def no_prior(*a, **k):
        return None

    called = {"llm": 0}

    async def llm(**kw):
        called["llm"] += 1
        return types.SimpleNamespace(ok=True, text="1", detail="")

    monkeypatch.setattr(advance_memory, "recall", hit)
    monkeypatch.setattr(advance_memory, "recall_prior", no_prior)
    monkeypatch.setattr(advance_agent.platform_api, "complete_llm", llm)
    d = _pick()
    assert d.status == advance_agent.STATUS_PICKED
    assert d.index == 1  # original index of "See My Quote"
    assert called["llm"] == 0


def test_prior_hit_skips_the_llm_as_tier_2_5(monkeypatch):
    async def miss(*a, **k):
        return None

    async def prior(tenant_id, labels):
        assert "See My Quote" in labels
        return "see my quote"

    called = {"llm": 0}

    async def llm(**kw):
        called["llm"] += 1
        return types.SimpleNamespace(ok=True, text="1", detail="")

    monkeypatch.setattr(advance_memory, "recall", miss)
    monkeypatch.setattr(advance_memory, "recall_prior", prior)
    monkeypatch.setattr(advance_agent.platform_api, "complete_llm", llm)
    d = _pick()
    assert d.status == advance_agent.STATUS_PICKED and d.index == 1
    assert called["llm"] == 0


def test_stale_memory_label_falls_through_to_llm(monkeypatch):
    """A remembered label no longer on the page must not block the pick."""
    async def hit(tenant_id, signature):
        return "a label that is gone"

    async def no_prior(*a, **k):
        return None

    async def llm(**kw):
        return types.SimpleNamespace(ok=True, text="1", detail="")

    monkeypatch.setattr(advance_memory, "recall", hit)
    monkeypatch.setattr(advance_memory, "recall_prior", no_prior)
    monkeypatch.setattr(advance_agent.platform_api, "complete_llm", llm)
    d = _pick()
    assert d.status == advance_agent.STATUS_PICKED and d.index == 1


# ── DB round-trip (skipif-gated, house pattern) ──────────────────────────

DB_URL = os.environ.get("QEC_TEST_DATABASE_URL", "")
needs_db = pytest.mark.skipif(
    not DB_URL,
    reason="QEC_TEST_DATABASE_URL not set — memory/prior round-trip needs a "
           "disposable Postgres (QecBase tables are created in-test)",
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
def test_harvest_then_recall_round_trip():
    asyncio.run(_run_round_trip())


async def _run_round_trip():
    from app.db.advance_models import AdvanceLabelPriorRow, AdvanceMemoryRow
    from app.db.fleet_models import TenantProvisioningRow
    from app.db.models import QecBase

    engine = create_async_engine(DB_URL)
    async with engine.begin() as conn:
        await conn.run_sync(QecBase.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    consenting = f"qec-adv-yes-{uuid.uuid4().hex[:8]}"
    private = f"qec-adv-no-{uuid.uuid4().hex[:8]}"
    original = advance_memory.tenant_scoped_qec_session
    try:
        advance_memory.tenant_scoped_qec_session = (
            lambda tid: _scoped(factory, tid))
        async with _scoped(factory, consenting) as s:
            s.add(TenantProvisioningRow(
                tenant_id=consenting, share_advance_priors=True))
            s.add(TenantProvisioningRow(
                tenant_id=private, share_advance_priors=False))

        coverage = _coverage([
            {"advance": {"tier": 3, "control_name": "See My Quote",
                         "oracle": True, "signature": "sig-rt"}},
        ])
        out = await advance_memory.harvest_completion(
            tenant_id=consenting, app_id="app1", coverage=coverage)
        assert out == {"proven": 1, "remembered": 1, "contributed": 1}

        # Tenant-private recall answers; the other tenant sees nothing.
        assert await advance_memory.recall(consenting, "sig-rt") == "see my quote"
        assert await advance_memory.recall(private, "sig-rt") is None

        # Re-proof reinforces (proof_count grows, no duplicate rows).
        await advance_memory.harvest_completion(
            tenant_id=consenting, app_id="app1", coverage=coverage)
        async with _scoped(factory, consenting) as s:
            row = (await s.execute(select(AdvanceMemoryRow).where(
                AdvanceMemoryRow.tenant_id == consenting))).scalar_one()
            assert row.proof_count == 2

        # The non-consenting tenant is remembered privately but contributes
        # NOTHING to the pool.
        await advance_memory.harvest_completion(
            tenant_id=private, app_id="app2", coverage=coverage)
        async with _scoped(factory, private) as s:
            prior = (await s.execute(select(AdvanceLabelPriorRow).where(
                AdvanceLabelPriorRow.label_norm == "see my quote"))).scalar_one()
            assert prior.distinct_tenants == 1
            assert contributor_hash(private) not in prior.contributor_hashes
            assert consenting not in str(prior.contributor_hashes)
    finally:
        advance_memory.tenant_scoped_qec_session = original
        await engine.dispose()
