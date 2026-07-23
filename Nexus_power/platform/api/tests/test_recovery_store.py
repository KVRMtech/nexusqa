"""R5 v2 — persisted, human-gated recovery proposals (real async-SQLite).

Doctrine under test: persist proposals; a human APPROVE/REJECT is attributed +
timestamped; the agent applies nothing; a terminal decision is not reopened by
a repeat scan; a green run of the repro RESOLVES an approved proposal.
"""
from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.services.agentic import recovery_store as rs
from app.services.agentic.recovery_store import RecoveryProposalRow


def _with_store(body):
    async def runner():
        engine = create_async_engine("sqlite+aiosqlite://")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(RecoveryProposalRow.__table__.create)
            maker = async_sessionmaker(engine, expire_on_commit=False)
            async with maker() as s:
                return await body(s)
        finally:
            await engine.dispose()
    return asyncio.run(runner())


def _proposal(scenario="gap-1", cause="WRONG_CONTROL_KIND", step=2):
    return {"kind": "capability_gap_proposal", "status": "proposed",
            "scenario_id": scenario, "step_number": step, "cause": cause,
            "suggested_strategy": "route through the UACR interaction resolver",
            "evidence": ["kind=slider"]}


def test_persist_is_idempotent_upsert():
    async def body(s):
        n1 = await rs.persist_scan(s, tenant_id="t1", artifact_id="a1",
                                   run_id="r1", proposals=[_proposal()])
        await s.commit()
        n2 = await rs.persist_scan(s, tenant_id="t1", artifact_id="a1",
                                   run_id="r2", proposals=[_proposal()])
        await s.commit()
        rows = await rs.list_proposals(s, tenant_id="t1", artifact_id="a1")
        return n1, n2, rows
    n1, n2, rows = _with_store(body)
    assert n1 == 1 and n2 == 1
    assert len(rows) == 1, "same (scenario,cause) UPSERTs — no duplicate proposals"
    assert rows[0]["status"] == "proposed"
    assert rows[0]["run_id"] == "r2", "re-scan refreshes the run pointer"


def test_approve_is_attributed_and_terminal():
    async def body(s):
        await rs.persist_scan(s, tenant_id="t1", artifact_id="a1", run_id="r1",
                              proposals=[_proposal()])
        await s.commit()
        pid = (await rs.list_proposals(s, tenant_id="t1", artifact_id="a1"))[0]["proposal_id"]
        upd = await rs.record_decision(s, tenant_id="t1", proposal_id=pid,
                                       decision="approve", decided_by="founder@x",
                                       note="valid gap")
        await s.commit()
        # a repeat scan must NOT reopen an approved proposal
        n = await rs.persist_scan(s, tenant_id="t1", artifact_id="a1", run_id="r3",
                                  proposals=[_proposal()])
        after = (await rs.list_proposals(s, tenant_id="t1", artifact_id="a1"))[0]
        return upd, n, after
    upd, n, after = _with_store(body)
    assert upd["status"] == "approved"
    assert upd["decided_by"] == "founder@x" and upd["decided_at"]
    assert n == 0, "an approved proposal is terminal — a re-scan does not touch it"
    assert after["status"] == "approved"


def test_bad_decision_and_missing_proposal_return_none():
    async def body(s):
        await rs.persist_scan(s, tenant_id="t1", artifact_id="a1", run_id="r1",
                              proposals=[_proposal()])
        await s.commit()
        pid = (await rs.list_proposals(s, tenant_id="t1", artifact_id="a1"))[0]["proposal_id"]
        bad = await rs.record_decision(s, tenant_id="t1", proposal_id=pid,
                                       decision="delete_it", decided_by="x")
        missing = await rs.record_decision(s, tenant_id="t1", proposal_id="nope",
                                           decision="approve", decided_by="x")
        anon = await rs.record_decision(s, tenant_id="t1", proposal_id=pid,
                                        decision="approve", decided_by="")
        return bad, missing, anon
    assert _with_store(body) == (None, None, None)


def test_green_run_resolves_only_approved_proposals():
    async def body(s):
        await rs.persist_scan(s, tenant_id="t1", artifact_id="a1", run_id="r1",
                              proposals=[_proposal("gap-1"), _proposal("gap-2", cause="CANVAS_NO_DOM")])
        await s.commit()
        rows = await rs.list_proposals(s, tenant_id="t1", artifact_id="a1")
        pid1 = next(r["proposal_id"] for r in rows if r["scenario_id"] == "gap-1")
        await rs.record_decision(s, tenant_id="t1", proposal_id=pid1,
                                 decision="approve", decided_by="op")
        await s.commit()
        # both scenarios now pass — but only the APPROVED one resolves
        resolved = await rs.resolve_if_passing(
            s, tenant_id="t1", artifact_id="a1",
            passing_scenario_ids={"gap-1", "gap-2"})
        await s.commit()
        final = {r["scenario_id"]: r["status"]
                 for r in await rs.list_proposals(s, tenant_id="t1", artifact_id="a1")}
        return resolved, final
    resolved, final = _with_store(body)
    assert resolved == 1
    assert final["gap-1"] == "resolved"       # approved + now green -> resolved
    assert final["gap-2"] == "proposed"       # never approved -> untouched


def test_tenant_isolation():
    async def body(s):
        await rs.persist_scan(s, tenant_id="t1", artifact_id="a1", run_id="r1",
                              proposals=[_proposal()])
        await s.commit()
        return await rs.list_proposals(s, tenant_id="t2", artifact_id="a1")
    assert _with_store(body) == []
