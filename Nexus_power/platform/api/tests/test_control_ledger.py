"""R6 — the proven-control ledger, behaviourally proven for the first time.

The flagship 'permanent capability' mechanism (heal once → reuse everywhere →
quarantine when stale) had ZERO tests (requirements-audit finding), and its
FIX_KINDS gate silently dropped the nav/advance/nav_recover memos the
write-on-green path has always passed. Real async-SQLite round-trips, no mocks
of the module under test; every engine is disposed (an undisposed aiosqlite
engine keeps a non-daemon thread alive and hangs pytest at exit).
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.services.diff_and_heal import control_ledger as cl
from app.services.diff_and_heal.control_ledger import ProvenControlLedgerRow


def _with_ledger(body):
    """Run ``await body(session)`` against a fresh in-memory ledger DB, always
    disposing the engine (Windows: a live aiosqlite thread blocks interpreter
    exit)."""
    async def runner():
        engine = create_async_engine("sqlite+aiosqlite://")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(ProvenControlLedgerRow.__table__.create)
            maker = async_sessionmaker(engine, expire_on_commit=False)
            async with maker() as session:
                return await body(session)
        finally:
            await engine.dispose()
    return asyncio.run(runner())


OBS = {"verb": "click", "label": "Products", "kind": "link",
       "url": "https://app.test/quote"}


def test_fingerprint_is_stable_and_page_scoped():
    fp1 = cl.control_fingerprint(OBS, page_path="/quote")
    fp2 = cl.control_fingerprint(dict(OBS), page_path="/quote")
    fp3 = cl.control_fingerprint(OBS, page_path="/other")
    assert fp1 and fp1 == fp2, "same control + page must fingerprint identically"
    assert fp1 != fp3, "a different page is a different control identity"
    assert cl.control_fingerprint(None) == ""
    assert cl.control_fingerprint({}) == ""


@pytest.mark.parametrize("kind", ["control_kind", "reanchor", "interaction",
                                  "wait", "nav", "advance", "nav_recover"])
def test_every_write_path_fix_kind_is_recordable(kind):
    """The write-on-green loop passes ALL SEVEN kinds; the gate silently
    dropped nav/advance/nav_recover (fail-open False, no trace) so those heals
    were never memoized — the audit's one-line R6 finding."""
    async def body(s):
        ok = await cl.record_proven_fix(
            s, tenant_id="t1", app_key="art-1",
            control_fp="f" * 40, fix_kind=kind, payload={"x": 1},
            label="Products", page_path="/quote", proven_by_run="r1")
        await s.commit()
        return ok
    assert _with_ledger(body) is True, f"fix_kind {kind!r} must be recordable"


def test_unknown_fix_kind_and_blank_fp_still_refused():
    async def body(s):
        bad_kind = await cl.record_proven_fix(
            s, tenant_id="t1", app_key="a", control_fp="f" * 40,
            fix_kind="made_up", payload={})
        blank_fp = await cl.record_proven_fix(
            s, tenant_id="t1", app_key="a", control_fp="",
            fix_kind="reanchor", payload={})
        return bad_kind, blank_fp
    assert _with_ledger(body) == (False, False)


def test_upsert_bumps_confirmed_count():
    async def body(s):
        for _ in range(2):
            await cl.record_proven_fix(
                s, tenant_id="t1", app_key="art-1", control_fp="f" * 40,
                fix_kind="nav", payload={"url": "https://app.test/quote"},
                label="entry", page_path="/quote", proven_by_run="r1")
        await s.commit()
        return await cl.get_proven_fixes(s, tenant_id="t1", app_key="art-1")
    fixes = _with_ledger(body)          # {control_fp: [entries]}
    assert list(fixes) == ["f" * 40]
    entries = fixes["f" * 40]
    assert len(entries) == 1, "re-prove must UPSERT, not duplicate"
    assert entries[0]["confirmed_count"] == 2
    assert entries[0]["fix_kind"] == "nav"


def test_stale_quarantine_at_threshold_and_reactivation():
    """Two consecutive misfires quarantine the seed (stops being served); a
    later green re-prove reactivates it."""
    async def body(s):
        kw = dict(tenant_id="t1", app_key="art-1", control_fp="f" * 40,
                  fix_kind="interaction")
        await cl.record_proven_fix(s, **kw, payload={"recipe": "open_then_click"})
        await s.commit()
        one = await cl.mark_seed_stale(s, **kw, invalidated_by_run="r2")
        await s.commit()
        after_one = await cl.get_proven_fixes(s, tenant_id="t1", app_key="art-1")
        await cl.mark_seed_stale(s, **kw, invalidated_by_run="r3")
        await s.commit()
        after_two = await cl.get_proven_fixes(s, tenant_id="t1", app_key="art-1")
        await cl.record_proven_fix(s, **kw, payload={"recipe": "open_then_click"})
        await s.commit()
        revived = await cl.get_proven_fixes(s, tenant_id="t1", app_key="art-1")
        return one, after_one, after_two, revived
    one, after_one, after_two, revived = _with_ledger(body)
    assert one is True
    assert len(after_one) == 1, "a single misfire must NOT quarantine (flake tolerance)"
    assert after_two == {}, "second consecutive misfire quarantines the seed"
    assert len(revived) == 1, "a green re-prove reactivates the quarantined seed"


def test_tenant_and_app_scoping_is_absolute():
    async def body(s):
        await cl.record_proven_fix(
            s, tenant_id="t1", app_key="art-1", control_fp="f" * 40,
            fix_kind="reanchor", payload={})
        await s.commit()
        other_tenant = await cl.get_proven_fixes(s, tenant_id="t2", app_key="art-1")
        other_app = await cl.get_proven_fixes(s, tenant_id="t1", app_key="art-2")
        return other_tenant, other_app
    assert _with_ledger(body) == ({}, {})
