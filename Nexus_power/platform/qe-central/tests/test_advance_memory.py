"""Advance memory — proven-only write-back, tenant-private recall, and the
consent-gated, value-free cross-tenant label pool.

Laws under test:
  * only PROVEN advances are harvested — of EVERY tier (presence of step
    ``advance`` evidence carrying a ``signature`` IS the proof; who decided is
    recorded, not required);
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
    _proven_advances,
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


def test_harvest_extracts_every_tier_that_carries_a_signature():
    """T-CAP-02. A deterministic advance is proof of exactly the same fact a
    tier-3 one is, and the harvest used to drop it."""
    steps = [
        {"advance": {"tier": 1, "control_name": "Continue", "oracle": False,
                     "signature": "sig-det"}},
        {"advance": {"tier": 3, "control_name": "See My Quote", "oracle": True,
                     "signature": "sig-a"}},
        {"title": "terminal step, no advance"},
    ]
    assert _proven_advances(_coverage(steps)) == [
        ("sig-det", "continue", False),
        ("sig-a", "see my quote", True),
    ]


def test_an_advance_without_a_signature_is_never_stored_under_a_fabricated_key():
    """The signature is the decision point a label is recalled AT. Without one
    there is nowhere to put the memory, whichever tier decided."""
    steps = [
        {"advance": {"tier": 1, "control_name": "Continue", "oracle": False}},
        {"advance": {"tier": 3, "control_name": "No Signature", "oracle": True}},
        {"advance": {"tier": 1, "control_name": "", "oracle": False,
                     "signature": "sig-nameless"}},
    ]
    assert _proven_advances(_coverage(steps)) == []


def test_harvest_tolerates_malformed_coverage():
    assert _proven_advances(None) == []
    assert _proven_advances({"flows": "nope"}) == []
    assert _proven_advances({"flows": [{"steps": [None, 4, {}]}]}) == []


def test_recall_never_raises_without_db():
    """With no reachable database, recall degrades to None (best-effort)."""
    assert asyncio.run(advance_memory.recall("", "sig")) is None
    assert asyncio.run(advance_memory.recall_prior("", {"x"})) is None


# ── The cross-service key, frozen as data ────────────────────────────────

#: MIRRORED PIN. qe-explorer computes this same signature locally for a
#: DETERMINISTIC (tier-1/2) advance so the memory it writes lands where THIS
#: service will look for it. The two share no library, and a cross-process
#: contract cannot be proven inside one process — so it is frozen as data on
#: both sides. Mirror: qe-explorer/tests/test_advance_signature.py::
#: test_signature_parity_vector — change BOTH or neither.
PARITY_CONTROLS = [
    {"kind": "button", "name": "Continue"},
    {"kind": "link", "name": "Back to Quote"},
    {"kind": "button", "name": "  SAVE   Draft "},
]
PARITY_TITLE = "Step 2 of 4 - Coverage 12345"
PARITY_SIGNATURE = (
    "1063a6f6feeaa9bdae95e55ce8a573ee11af034fcc959b3f4a007c62c9cd00c9")


def test_signature_parity_vector():
    eligible = advance_agent.eligible_controls(PARITY_CONTROLS)
    assert len(eligible) == len(PARITY_CONTROLS), (
        "the vector must survive eligibility unchanged, or it pins the filter "
        "rather than the hash")
    assert advance_agent.compute_signature(eligible, PARITY_TITLE) == PARITY_SIGNATURE


def test_a_deterministic_advance_is_recalled_at_the_key_the_explorer_wrote(
        monkeypatch):
    """T-CAP-02 END TO END, across the seam.

    Crawl 1 advanced deterministically ("Continue", tier 1, no LLM) and the
    explorer stored it under the key it computed itself. Crawl 2 reaches the
    SAME decision point and this service answers it — from memory, without an
    LLM call. Neither half is allowed to invent the key: the store side uses
    the frozen vector the explorer's own suite pins, the recall side computes
    it here from the controls.
    """
    stored = {sig: label for sig, label, _ in _proven_advances(_coverage([
        # exactly what qe-explorer's walker emits for a tier-1 advance
        {"advance": {"tier": 1, "control_name": "Continue", "oracle": False,
                     "signature": PARITY_SIGNATURE}},
    ]))}
    assert stored == {PARITY_SIGNATURE: "continue"}, (
        "crawl 1 proved a deterministic advance and it was not stored")

    async def recall(tenant_id, signature):
        return stored.get(signature)

    async def no_prior(*a, **k):
        return None

    called = {"llm": 0}

    async def llm(**kw):
        called["llm"] += 1
        return types.SimpleNamespace(ok=True, text="1", detail="")

    monkeypatch.setattr(advance_memory, "recall", recall)
    monkeypatch.setattr(advance_memory, "recall_prior", no_prior)
    monkeypatch.setattr(advance_agent.platform_api, "complete_llm", llm)

    decision = asyncio.run(advance_agent.pick_advance(
        tenant_id="t1", controls=PARITY_CONTROLS,
        page_title=PARITY_TITLE, page_url="https://a.example/step2"))

    assert decision.status == advance_agent.STATUS_PICKED
    assert decision.index == 0 and PARITY_CONTROLS[0]["name"] == "Continue"
    assert called["llm"] == 0, (
        "the common deterministic path must never depend on an LLM")


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
    # The LABEL has to be unique per run too, not just the tenant ids.
    # advance_label_priors is the federated pool: deliberately CROSS-TENANT and
    # deliberately tenant-column-free, keyed by label_norm alone. With a fixed
    # "See My Quote" every run contributed to the SAME prior row, so
    # distinct_tenants counted every past run and the == 1 assertion below
    # failed the second time this test met the same database. A unique label
    # gives each run its own pool row — which is also the only isolation
    # available on a table that has no tenant to scope by.
    sfx = uuid.uuid4().hex[:8]
    control_name, label_norm = f"See My Quote {sfx}", f"see my quote {sfx}"
    signature = f"sig-rt-{sfx}"
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
            {"advance": {"tier": 3, "control_name": control_name,
                         "oracle": True, "signature": signature}},
        ])
        out = await advance_memory.harvest_completion(
            tenant_id=consenting, app_id="app1", coverage=coverage)
        assert out == {"proven": 1, "remembered": 1, "contributed": 1,
                       "oracle": 1, "deterministic": 0}

        # Tenant-private recall answers; the other tenant sees nothing.
        assert await advance_memory.recall(consenting, signature) == label_norm
        assert await advance_memory.recall(private, signature) is None

        # Re-proof reinforces (proof_count grows, no duplicate rows).
        await advance_memory.harvest_completion(
            tenant_id=consenting, app_id="app1", coverage=coverage)
        async with _scoped(factory, consenting) as s:
            row = (await s.execute(select(AdvanceMemoryRow).where(
                AdvanceMemoryRow.tenant_id == consenting))).scalar_one()
            assert row.proof_count == 2

        # T-CAP-02: a DETERMINISTIC advance survives the same round trip.
        # Its evidence carries oracle=False and tier=1 — the two facts the
        # harvest used to reject it on — and the crawl that produced it never
        # called an LLM.
        det_name, det_label = f"Continue {sfx}", f"continue {sfx}"
        det_signature = f"sig-det-{sfx}"
        det_out = await advance_memory.harvest_completion(
            tenant_id=consenting, app_id="app1", coverage=_coverage([
                {"advance": {"tier": 1, "control_name": det_name,
                             "oracle": False, "signature": det_signature}},
            ]))
        assert det_out == {"proven": 1, "remembered": 1, "contributed": 1,
                           "oracle": 0, "deterministic": 1}
        assert await advance_memory.recall(consenting, det_signature) == det_label
        assert await advance_memory.recall(private, det_signature) is None

        # The non-consenting tenant is remembered privately but contributes
        # NOTHING to the pool.
        await advance_memory.harvest_completion(
            tenant_id=private, app_id="app2", coverage=coverage)
        async with _scoped(factory, private) as s:
            prior = (await s.execute(select(AdvanceLabelPriorRow).where(
                AdvanceLabelPriorRow.label_norm == label_norm))).scalar_one()
            assert prior.distinct_tenants == 1
            assert contributor_hash(private) not in prior.contributor_hashes
            assert consenting not in str(prior.contributor_hashes)
    finally:
        advance_memory.tenant_scoped_qec_session = original
        await engine.dispose()
