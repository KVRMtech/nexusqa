"""R4 Mechanic Memory — unit + DB round-trip tests.

Pure-logic tests cover the harvest extraction and the coverage→memory pipeline.
DB tests (skipif-gated) cover the full recall→harvest→prior lifecycle.
"""
from __future__ import annotations

import os
import asyncio

import pytest

from app.services.mechanic_memory import (
    _proven_mechanics_from_coverage,
    harvest_mechanics,
    recall_all,
    recall_priors,
)


# ── Pure-logic: _proven_mechanics_from_coverage ──────────────────────────────

def test_extracts_from_valid_ledger():
    coverage = {
        "field_ledger": [
            {"signature": "abc123", "mechanic": "click_element", "filled": True},
            {"signature": "def456", "mechanic": "focus_space", "filled": True},
        ],
    }
    pairs = _proven_mechanics_from_coverage(coverage)
    assert pairs == [("abc123", "click_element"), ("def456", "focus_space")]


def test_skips_unfilled_entries():
    coverage = {
        "field_ledger": [
            {"signature": "abc123", "mechanic": "click_element", "filled": False},
            {"signature": "def456", "mechanic": "focus_space", "filled": True},
        ],
    }
    pairs = _proven_mechanics_from_coverage(coverage)
    assert pairs == [("def456", "focus_space")]


def test_skips_entries_without_mechanic():
    coverage = {
        "field_ledger": [
            {"signature": "abc123", "filled": True},
            {"signature": "def456", "mechanic": "", "filled": True},
        ],
    }
    pairs = _proven_mechanics_from_coverage(coverage)
    assert pairs == []


def test_skips_entries_without_signature():
    coverage = {
        "field_ledger": [
            {"mechanic": "click_element", "filled": True},
            {"signature": "", "mechanic": "click_element", "filled": True},
        ],
    }
    pairs = _proven_mechanics_from_coverage(coverage)
    assert pairs == []


def test_deduplicates_by_signature():
    coverage = {
        "field_ledger": [
            {"signature": "abc123", "mechanic": "click_element", "filled": True},
            {"signature": "abc123", "mechanic": "focus_space", "filled": True},
        ],
    }
    pairs = _proven_mechanics_from_coverage(coverage)
    assert len(pairs) == 1
    assert pairs[0] == ("abc123", "click_element")


def test_handles_none_coverage():
    assert _proven_mechanics_from_coverage(None) == []


def test_handles_missing_field_ledger():
    assert _proven_mechanics_from_coverage({}) == []
    assert _proven_mechanics_from_coverage({"field_ledger": None}) == []


def test_handles_malformed_entries():
    coverage = {
        "field_ledger": [
            None,
            "not a dict",
            42,
            {"signature": "abc123", "mechanic": "click_element", "filled": True},
        ],
    }
    pairs = _proven_mechanics_from_coverage(coverage)
    assert pairs == [("abc123", "click_element")]


def test_empty_ledger():
    assert _proven_mechanics_from_coverage({"field_ledger": []}) == []


def test_mixed_filled_and_unfilled():
    coverage = {
        "field_ledger": [
            {"signature": "a", "mechanic": "click_element", "filled": True},
            {"signature": "b", "mechanic": "focus_space", "filled": False},
            {"signature": "c", "mechanic": "native_fill", "filled": True},
            {"signature": "d", "filled": True},
        ],
    }
    pairs = _proven_mechanics_from_coverage(coverage)
    assert pairs == [("a", "click_element"), ("c", "native_fill")]


# ── DB round-trip tests ─────────────────────────────────────────────────────

QEC_TEST_DB = os.environ.get("QEC_TEST_DATABASE_URL", "")
skip_no_db = pytest.mark.skipif(not QEC_TEST_DB,
                                reason="QEC_TEST_DATABASE_URL not set")


@skip_no_db
def test_recall_harvest_round_trip():
    """Seed mechanic → recall → harvest replaces → recall again."""
    from app.db import tenant_scoped_qec_session, utc_now
    from app.db.advance_models import MechanicMemoryRow

    tenant = "test-mechanic-rt"
    app_id = "app-mech-1"

    async def _run():
        # Clean slate
        async with tenant_scoped_qec_session(tenant) as s:
            from sqlalchemy import delete
            await s.execute(
                delete(MechanicMemoryRow).where(
                    MechanicMemoryRow.tenant_id == tenant))

        # Seed one mechanic directly
        async with tenant_scoped_qec_session(tenant) as s:
            s.add(MechanicMemoryRow(
                tenant_id=tenant, control_sig="sig_aaa",
                mechanic="click_element", app_id=app_id,
                proof_count=1, last_proven_at=utc_now()))

        # Recall should return it
        result = await recall_all(tenant, app_id)
        assert result == {"sig_aaa": "click_element"}

        # Harvest with a DIFFERENT mechanic for the same sig → replaces
        coverage = {
            "field_ledger": [
                {"signature": "sig_aaa", "mechanic": "focus_space", "filled": True},
                {"signature": "sig_bbb", "mechanic": "native_fill", "filled": True},
            ],
        }
        stats = await harvest_mechanics(
            tenant_id=tenant, app_id=app_id, coverage=coverage)
        assert stats["proven"] == 2
        assert stats["remembered"] == 2

        # Recall again — sig_aaa replaced, sig_bbb added
        result = await recall_all(tenant, app_id)
        assert result["sig_aaa"] == "focus_space"
        assert result["sig_bbb"] == "native_fill"

        # Harvest same mechanic again → proof_count reinforced
        stats = await harvest_mechanics(
            tenant_id=tenant, app_id=app_id, coverage=coverage)
        assert stats["remembered"] == 2

        async with tenant_scoped_qec_session(tenant) as s:
            from sqlalchemy import select
            row = (await s.execute(
                select(MechanicMemoryRow).where(
                    MechanicMemoryRow.tenant_id == tenant,
                    MechanicMemoryRow.control_sig == "sig_bbb",
                ))).scalar_one()
            assert row.proof_count == 2

        # Cleanup
        async with tenant_scoped_qec_session(tenant) as s:
            await s.execute(
                delete(MechanicMemoryRow).where(
                    MechanicMemoryRow.tenant_id == tenant))

    asyncio.run(_run())


@skip_no_db
def test_recall_empty_tenant():
    """recall_all on a tenant with no mechanics returns empty dict."""
    async def _run():
        result = await recall_all("nonexistent-tenant-xyz", "some-app")
        assert result == {}

    asyncio.run(_run())


@skip_no_db
def test_harvest_empty_coverage():
    """harvest_mechanics with no mechanic entries returns zeros."""
    async def _run():
        stats = await harvest_mechanics(
            tenant_id="test-empty", app_id="app-1",
            coverage={"field_ledger": []})
        assert stats == {"proven": 0, "remembered": 0, "contributed": 0}

    asyncio.run(_run())
