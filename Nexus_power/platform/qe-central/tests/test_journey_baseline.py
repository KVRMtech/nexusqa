"""O0 — Journey baseline lifecycle tests.

Pure-logic tests run everywhere; the DB round-trip is skipif-gated on
``QEC_TEST_DATABASE_URL`` (house pattern).

Coverage:
  * outcome_hash — deterministic, order-insensitive on labels, case-insensitive
  * outcomes_match — semantic equality
  * diff_outcomes — added / removed / changed / unchanged
  * build_snapshot — shape + immutability anchor
  * approve_baseline — DB round-trip: captured→approved
  * detect_drift — DB round-trip: approved→validated / approved→drifted
  * adjudicate_drift — DB round-trip: intended_change / defect
  * approval chain — hash-chained, tamper-evident
"""
from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.services.journey_baseline import (
    ADJUDICATION_DEFECT,
    ADJUDICATION_INTENDED_CHANGE,
    ADJUDICATION_VERDICTS,
    BASELINE_APPROVED,
    BASELINE_CAPTURED,
    BASELINE_DRIFTED,
    BASELINE_STATES,
    BASELINE_VALIDATED,
    build_snapshot,
    diff_outcomes,
    outcome_hash,
    outcomes_match,
)


# ── Pure-logic tests ────────────────────────────────────────────────────────

def test_baseline_states_exhaustive():
    assert BASELINE_STATES == {"captured", "approved", "validated", "drifted"}
    for s in BASELINE_STATES:
        assert len(s) <= 16


def test_adjudication_verdicts_exhaustive():
    assert ADJUDICATION_VERDICTS == {"intended_change", "defect"}


def test_outcome_hash_is_deterministic():
    vals = [{"label": "Monthly Premium", "value": "$45.00",
             "value_type": "currency"}]
    assert outcome_hash(vals) == outcome_hash(vals)


def test_outcome_hash_is_label_order_insensitive():
    a = [{"label": "Premium", "value": "$45", "value_type": "currency"},
         {"label": "Coverage", "value": "$250,000", "value_type": "currency"}]
    b = [{"label": "Coverage", "value": "$250,000", "value_type": "currency"},
         {"label": "Premium", "value": "$45", "value_type": "currency"}]
    assert outcome_hash(a) == outcome_hash(b)


def test_outcome_hash_is_case_insensitive_on_labels():
    a = [{"label": "Monthly Premium", "value": "$45", "value_type": "CURRENCY"}]
    b = [{"label": "monthly premium", "value": "$45", "value_type": "currency"}]
    assert outcome_hash(a) == outcome_hash(b)


def test_outcome_hash_differs_on_value_change():
    a = [{"label": "Premium", "value": "$45", "value_type": "currency"}]
    b = [{"label": "Premium", "value": "$50", "value_type": "currency"}]
    assert outcome_hash(a) != outcome_hash(b)


def test_outcome_hash_empty():
    assert outcome_hash([]) == outcome_hash([])
    assert outcome_hash(None) == outcome_hash([])


def test_outcomes_match_identical():
    vals = [{"label": "Premium", "value": "$45", "value_type": "currency"}]
    assert outcomes_match(vals, vals) is True


def test_outcomes_match_different():
    a = [{"label": "Premium", "value": "$45", "value_type": "currency"}]
    b = [{"label": "Premium", "value": "$50", "value_type": "currency"}]
    assert outcomes_match(a, b) is False


def test_outcomes_match_empty():
    assert outcomes_match([], []) is True


def test_diff_outcomes_unchanged():
    vals = [{"label": "Premium", "value": "$45", "value_type": "currency"}]
    diffs = diff_outcomes(vals, vals)
    assert len(diffs) == 1
    assert diffs[0]["change"] == "unchanged"
    assert diffs[0]["approved_value"] == "$45"
    assert diffs[0]["observed_value"] == "$45"


def test_diff_outcomes_changed():
    a = [{"label": "Premium", "value": "$45", "value_type": "currency"}]
    b = [{"label": "Premium", "value": "$50", "value_type": "currency"}]
    diffs = diff_outcomes(a, b)
    assert len(diffs) == 1
    assert diffs[0]["change"] == "changed"
    assert diffs[0]["approved_value"] == "$45"
    assert diffs[0]["observed_value"] == "$50"


def test_diff_outcomes_added():
    a = []
    b = [{"label": "Premium", "value": "$45", "value_type": "currency"}]
    diffs = diff_outcomes(a, b)
    assert len(diffs) == 1
    assert diffs[0]["change"] == "added"
    assert diffs[0]["approved_value"] is None


def test_diff_outcomes_removed():
    a = [{"label": "Premium", "value": "$45", "value_type": "currency"}]
    b = []
    diffs = diff_outcomes(a, b)
    assert len(diffs) == 1
    assert diffs[0]["change"] == "removed"
    assert diffs[0]["observed_value"] is None


def test_diff_outcomes_mixed():
    a = [{"label": "Premium", "value": "$45", "value_type": "currency"},
         {"label": "Coverage", "value": "$250k", "value_type": "currency"}]
    b = [{"label": "Premium", "value": "$50", "value_type": "currency"},
         {"label": "Term", "value": "20yr", "value_type": "text"}]
    diffs = diff_outcomes(a, b)
    by_label = {d["label"]: d for d in diffs}
    assert by_label["Coverage"]["change"] == "removed"
    assert by_label["Premium"]["change"] == "changed"
    assert by_label["Term"]["change"] == "added"


def test_diff_outcomes_tolerates_malformed():
    diffs = diff_outcomes(None, [42, "bad"])
    assert diffs == []


class _FakeTraversal:
    def __init__(self, *, traversal_id="t1", exploration_id="e1",
                 path_fps=None, path_hash="ph1", outcome_values=None,
                 identity_ref="", env_ref="", terminal="submit_boundary",
                 completed=True, created_at=None):
        self.traversal_id = traversal_id
        self.exploration_id = exploration_id
        self.path_fps = path_fps or ["fpA", "fpB"]
        self.path_hash = path_hash
        self.outcome_values = outcome_values or [
            {"label": "Premium", "value": "$45", "value_type": "currency"}]
        self.identity_ref = identity_ref
        self.env_ref = env_ref
        self.terminal = terminal
        self.completed = completed
        self.created_at = created_at or datetime.now(timezone.utc)


def test_build_snapshot_shape():
    t = _FakeTraversal()
    snap = build_snapshot(t)
    assert snap["traversal_id"] == "t1"
    assert snap["outcome_values"] == t.outcome_values
    assert snap["outcome_hash"] == outcome_hash(t.outcome_values)
    assert snap["path_fps"] == ["fpA", "fpB"]
    assert snap["completed"] is True
    assert "captured_at" in snap


def test_build_snapshot_outcome_hash_is_anchor():
    t = _FakeTraversal()
    snap = build_snapshot(t)
    assert snap["outcome_hash"] == outcome_hash(snap["outcome_values"])


def test_build_snapshot_with_steps():
    t = _FakeTraversal()
    steps = [{"step": 1, "title": "Start"}]
    snap = build_snapshot(t, steps=steps)
    assert snap["steps"] == steps


# ── DB round-trip tests (skipif-gated) ──────────────────────────────────────

DB_URL = os.environ.get("QEC_TEST_DATABASE_URL", "")
needs_db = pytest.mark.skipif(
    not DB_URL,
    reason="QEC_TEST_DATABASE_URL not set — the baseline round-trip needs "
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
def test_approve_then_validate_then_drift_round_trip():
    asyncio.run(_run_baseline_round_trip())


async def _run_baseline_round_trip():
    from app.db.gov_models import ApprovalEventRow
    from app.db.journey_models import JourneyRow, JourneyTraversalRow
    from app.db.models import QecBase
    from app.services import approval, journey_baseline

    engine = create_async_engine(DB_URL)
    async with engine.begin() as conn:
        await conn.run_sync(QecBase.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant = f"qec-bl-{uuid.uuid4().hex[:10]}"
    app_id = "app-bl"

    originals = (
        journey_baseline.tenant_scoped_qec_session,
        approval.tenant_scoped_qec_session,
    )

    @asynccontextmanager
    async def _test_session(tid):
        async with _scoped(factory, tid) as s:
            yield s

    journey_baseline.tenant_scoped_qec_session = _test_session
    approval.tenant_scoped_qec_session = _test_session

    try:
        journey_id = f"j-{uuid.uuid4().hex[:12]}"
        traversal_id_1 = f"t-{uuid.uuid4().hex[:12]}"
        traversal_id_2 = f"t-{uuid.uuid4().hex[:12]}"
        traversal_id_3 = f"t-{uuid.uuid4().hex[:12]}"
        outcomes_original = [
            {"label": "Monthly Premium", "value": "$45.00",
             "value_type": "currency"}]
        outcomes_same = [
            {"label": "Monthly Premium", "value": "$45.00",
             "value_type": "currency"}]
        outcomes_different = [
            {"label": "Monthly Premium", "value": "$52.00",
             "value_type": "currency"}]

        # ── Seed a journey + 3 traversals ────────────────────────────────
        async with _scoped(factory, tenant) as session:
            session.add(JourneyRow(
                journey_id=journey_id, tenant_id=tenant, app_id=app_id,
                entry_fingerprint="fp-entry", flow_id="flow-1",
                entry_url="https://example.com/quote",
                entry_title="Get a Quote",
                business_name="Get a life insurance quote",
                name_source="fallback",
                baseline_status="captured",
            ))
            session.add(JourneyTraversalRow(
                traversal_id=traversal_id_1, tenant_id=tenant, app_id=app_id,
                journey_id=journey_id, exploration_id="exp-1",
                terminal="submit_boundary", completed=True,
                fully_answered=True, path_fps=["fpA", "fpB"],
                path_hash="ph1", identity_ref="", env_ref="",
                outcome_values=outcomes_original,
            ))
            session.add(JourneyTraversalRow(
                traversal_id=traversal_id_2, tenant_id=tenant, app_id=app_id,
                journey_id=journey_id, exploration_id="exp-2",
                terminal="submit_boundary", completed=True,
                fully_answered=True, path_fps=["fpA", "fpB"],
                path_hash="ph2", identity_ref="", env_ref="",
                outcome_values=outcomes_same,
            ))
            session.add(JourneyTraversalRow(
                traversal_id=traversal_id_3, tenant_id=tenant, app_id=app_id,
                journey_id=journey_id, exploration_id="exp-3",
                terminal="submit_boundary", completed=True,
                fully_answered=True, path_fps=["fpA", "fpB"],
                path_hash="ph3", identity_ref="", env_ref="",
                outcome_values=outcomes_different,
            ))

        # ── 1. captured → approved ───────────────────────────────────────
        result = await journey_baseline.approve_baseline(
            tenant_id=tenant, app_id=app_id, journey_id=journey_id,
            traversal_id=traversal_id_1, signature="Venkata Reddy",
            actor="venkata@example.com")
        assert result["baseline_status"] == BASELINE_APPROVED
        assert result["outcome_hash"] == outcome_hash(outcomes_original)
        assert "approval_event" in result
        assert result["approval_event"]["action"] == "approve"

        async with _scoped(factory, tenant) as session:
            j = (await session.execute(
                select(JourneyRow).where(
                    JourneyRow.journey_id == journey_id)
            )).scalar_one()
            assert j.baseline_status == BASELINE_APPROVED
            assert j.baseline_traversal_id == traversal_id_1
            assert j.baseline_outcome_hash != ""
            assert j.baseline_snapshot is not None
            assert j.baseline_approved_by == "venkata@example.com"

        # ── 2. approved → validated (same outcomes) ──────────────────────
        drift_result = await journey_baseline.detect_drift(
            tenant_id=tenant, app_id=app_id, journey_id=journey_id,
            new_traversal_id=traversal_id_2,
            new_outcome_values=outcomes_same)
        assert drift_result["action"] == "validated"
        assert drift_result["baseline_status"] == BASELINE_VALIDATED

        async with _scoped(factory, tenant) as session:
            j = (await session.execute(
                select(JourneyRow).where(
                    JourneyRow.journey_id == journey_id)
            )).scalar_one()
            assert j.baseline_status == BASELINE_VALIDATED

        # ── 3. validated → drifted (different outcomes) ──────────────────
        drift_result = await journey_baseline.detect_drift(
            tenant_id=tenant, app_id=app_id, journey_id=journey_id,
            new_traversal_id=traversal_id_3,
            new_outcome_values=outcomes_different)
        assert drift_result["action"] == "drifted"
        assert drift_result["baseline_status"] == BASELINE_DRIFTED
        assert drift_result["approved_hash"] != drift_result["observed_hash"]

        async with _scoped(factory, tenant) as session:
            j = (await session.execute(
                select(JourneyRow).where(
                    JourneyRow.journey_id == journey_id)
            )).scalar_one()
            assert j.baseline_status == BASELINE_DRIFTED
            assert j.drift_traversal_id == traversal_id_3
            assert j.drift_detected_at is not None

        # ── 4. adjudicate: intended_change → re-approved ─────────────────
        adj = await journey_baseline.adjudicate_drift(
            tenant_id=tenant, app_id=app_id, journey_id=journey_id,
            verdict=ADJUDICATION_INTENDED_CHANGE,
            signature="Venkata Reddy", actor="venkata@example.com",
            reason="rate table updated Q3")
        assert adj["baseline_status"] == BASELINE_APPROVED
        assert adj["verdict"] == ADJUDICATION_INTENDED_CHANGE

        async with _scoped(factory, tenant) as session:
            j = (await session.execute(
                select(JourneyRow).where(
                    JourneyRow.journey_id == journey_id)
            )).scalar_one()
            assert j.baseline_status == BASELINE_APPROVED
            assert j.baseline_traversal_id == traversal_id_3
            snap_vals = (j.baseline_snapshot or {}).get("outcome_values", [])
            assert any(v.get("value") == "$52.00" for v in snap_vals)
            assert j.drift_traversal_id == ""

        # ── 5. Force another drift, then adjudicate as defect ────────────
        drift_result = await journey_baseline.detect_drift(
            tenant_id=tenant, app_id=app_id, journey_id=journey_id,
            new_traversal_id=traversal_id_1,
            new_outcome_values=outcomes_original)
        assert drift_result["action"] == "drifted"

        adj = await journey_baseline.adjudicate_drift(
            tenant_id=tenant, app_id=app_id, journey_id=journey_id,
            verdict=ADJUDICATION_DEFECT,
            signature="Venkata Reddy", actor="venkata@example.com",
            reason="premium regression — should be $52")
        assert adj["baseline_status"] == BASELINE_CAPTURED
        assert adj["verdict"] == ADJUDICATION_DEFECT

        async with _scoped(factory, tenant) as session:
            j = (await session.execute(
                select(JourneyRow).where(
                    JourneyRow.journey_id == journey_id)
            )).scalar_one()
            assert j.baseline_status == BASELINE_CAPTURED
            assert j.baseline_snapshot is None
            assert j.baseline_outcome_hash == ""

        # ── 6. Approval chain is hash-linked ─────────────────────────────
        from app.services.approval import verify_approval_chain
        history = await journey_baseline.approval_history(
            tenant_id=tenant, journey_id=journey_id)
        assert len(history) >= 3
        verification = verify_approval_chain(history)
        assert verification.ok is True

        # ── 7. No-baseline journey stays captured on drift check ─────────
        drift_result = await journey_baseline.detect_drift(
            tenant_id=tenant, app_id=app_id, journey_id=journey_id,
            new_traversal_id=traversal_id_2,
            new_outcome_values=outcomes_same)
        assert drift_result["action"] == "no_baseline_yet"
        assert drift_result["baseline_status"] == BASELINE_CAPTURED

    finally:
        journey_baseline.tenant_scoped_qec_session = originals[0]
        approval.tenant_scoped_qec_session = originals[1]
    await engine.dispose()


@needs_db
def test_approve_rejects_incomplete_traversal():
    asyncio.run(_run_incomplete_rejection())


async def _run_incomplete_rejection():
    from app.db.journey_models import JourneyRow, JourneyTraversalRow
    from app.db.models import QecBase
    from app.services import approval, journey_baseline

    engine = create_async_engine(DB_URL)
    async with engine.begin() as conn:
        await conn.run_sync(QecBase.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant = f"qec-bl-{uuid.uuid4().hex[:10]}"

    originals = (
        journey_baseline.tenant_scoped_qec_session,
        approval.tenant_scoped_qec_session,
    )

    @asynccontextmanager
    async def _test_session(tid):
        async with _scoped(factory, tid) as s:
            yield s

    journey_baseline.tenant_scoped_qec_session = _test_session
    approval.tenant_scoped_qec_session = _test_session

    try:
        j_id = f"j-{uuid.uuid4().hex[:12]}"
        t_id = f"t-{uuid.uuid4().hex[:12]}"
        async with _scoped(factory, tenant) as session:
            session.add(JourneyRow(
                journey_id=j_id, tenant_id=tenant, app_id="app-bl",
                entry_fingerprint="fp-inc", flow_id="flow-inc",
                business_name="Incomplete", name_source="fallback",
                baseline_status="captured"))
            session.add(JourneyTraversalRow(
                traversal_id=t_id, tenant_id=tenant, app_id="app-bl",
                journey_id=j_id, exploration_id="exp-inc",
                terminal="no_advance", completed=False,
                fully_answered=False, path_fps=["fpA"],
                path_hash="phi", outcome_values=[]))

        with pytest.raises(ValueError, match="completed"):
            await journey_baseline.approve_baseline(
                tenant_id=tenant, app_id="app-bl", journey_id=j_id,
                traversal_id=t_id, signature="Test Signer")
    finally:
        journey_baseline.tenant_scoped_qec_session = originals[0]
        approval.tenant_scoped_qec_session = originals[1]
    await engine.dispose()


@needs_db
def test_adjudicate_rejects_non_drifted():
    asyncio.run(_run_adjudicate_non_drifted())


async def _run_adjudicate_non_drifted():
    from app.db.journey_models import JourneyRow
    from app.db.models import QecBase
    from app.services import approval, journey_baseline

    engine = create_async_engine(DB_URL)
    async with engine.begin() as conn:
        await conn.run_sync(QecBase.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant = f"qec-bl-{uuid.uuid4().hex[:10]}"

    originals = (
        journey_baseline.tenant_scoped_qec_session,
        approval.tenant_scoped_qec_session,
    )

    @asynccontextmanager
    async def _test_session(tid):
        async with _scoped(factory, tid) as s:
            yield s

    journey_baseline.tenant_scoped_qec_session = _test_session
    approval.tenant_scoped_qec_session = _test_session

    try:
        j_id = f"j-{uuid.uuid4().hex[:12]}"
        async with _scoped(factory, tenant) as session:
            session.add(JourneyRow(
                journey_id=j_id, tenant_id=tenant, app_id="app-bl",
                entry_fingerprint="fp-nd", flow_id="flow-nd",
                business_name="Not Drifted", name_source="fallback",
                baseline_status="captured"))

        with pytest.raises(ValueError, match="not 'drifted'"):
            await journey_baseline.adjudicate_drift(
                tenant_id=tenant, app_id="app-bl", journey_id=j_id,
                verdict=ADJUDICATION_DEFECT,
                signature="Test Signer")
    finally:
        journey_baseline.tenant_scoped_qec_session = originals[0]
        approval.tenant_scoped_qec_session = originals[1]
    await engine.dispose()


@needs_db
def test_baseline_view_with_drift():
    asyncio.run(_run_baseline_view())


async def _run_baseline_view():
    from app.db.journey_models import JourneyRow, JourneyTraversalRow
    from app.db.models import QecBase
    from app.services import approval, journey_baseline

    engine = create_async_engine(DB_URL)
    async with engine.begin() as conn:
        await conn.run_sync(QecBase.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant = f"qec-bl-{uuid.uuid4().hex[:10]}"

    originals = (
        journey_baseline.tenant_scoped_qec_session,
        approval.tenant_scoped_qec_session,
    )

    @asynccontextmanager
    async def _test_session(tid):
        async with _scoped(factory, tid) as s:
            yield s

    journey_baseline.tenant_scoped_qec_session = _test_session
    approval.tenant_scoped_qec_session = _test_session

    try:
        j_id = f"j-{uuid.uuid4().hex[:12]}"
        t1 = f"t-{uuid.uuid4().hex[:12]}"
        t2 = f"t-{uuid.uuid4().hex[:12]}"
        outcomes_a = [{"label": "Premium", "value": "$45",
                       "value_type": "currency"}]
        outcomes_b = [{"label": "Premium", "value": "$50",
                       "value_type": "currency"}]

        async with _scoped(factory, tenant) as session:
            session.add(JourneyRow(
                journey_id=j_id, tenant_id=tenant, app_id="app-bl",
                entry_fingerprint="fp-v", flow_id="flow-v",
                business_name="View Test", name_source="fallback",
                baseline_status="captured"))
            session.add(JourneyTraversalRow(
                traversal_id=t1, tenant_id=tenant, app_id="app-bl",
                journey_id=j_id, exploration_id="exp-v1",
                terminal="submit_boundary", completed=True,
                fully_answered=True, path_fps=["fpA"],
                path_hash="phv1", outcome_values=outcomes_a))
            session.add(JourneyTraversalRow(
                traversal_id=t2, tenant_id=tenant, app_id="app-bl",
                journey_id=j_id, exploration_id="exp-v2",
                terminal="submit_boundary", completed=True,
                fully_answered=True, path_fps=["fpA"],
                path_hash="phv2", outcome_values=outcomes_b))

        await journey_baseline.approve_baseline(
            tenant_id=tenant, app_id="app-bl", journey_id=j_id,
            traversal_id=t1, signature="Test Signer")

        await journey_baseline.detect_drift(
            tenant_id=tenant, app_id="app-bl", journey_id=j_id,
            new_traversal_id=t2, new_outcome_values=outcomes_b)

        view = await journey_baseline.baseline_view(
            tenant_id=tenant, app_id="app-bl", journey_id=j_id)
        assert view["baseline_status"] == BASELINE_DRIFTED
        assert "drift" in view
        drift = view["drift"]
        assert drift["traversal_id"] == t2
        assert drift["detected_at"] is not None
        assert len(drift["diff"]) == 1
        assert drift["diff"][0]["change"] == "changed"
        assert drift["diff"][0]["approved_value"] == "$45"
        assert drift["diff"][0]["observed_value"] == "$50"

    finally:
        journey_baseline.tenant_scoped_qec_session = originals[0]
        approval.tenant_scoped_qec_session = originals[1]
    await engine.dispose()
