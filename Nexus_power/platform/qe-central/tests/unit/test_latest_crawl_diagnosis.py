"""Regression: _latest_crawl ALWAYS emits a typed diagnosis — including the
never-crawled early-return path.

Caught by a live E2E: GET /apps/{id} on a fresh app returned {"status":"none",
"active":false} with NO diagnosis, because the no-crawl branch returns early. The
unit tests proved diagnose() *can* produce NONE, but nothing proved the endpoint
*emits* it. This pins both branches.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from app.routers.apps import _latest_crawl
from app.services import crawl_diagnosis as cd


class _FakeResult:
    def __init__(self, row):
        self._row = row

    def scalar_one_or_none(self):
        return self._row


class _FakeSession:
    """Minimal stand-in for the tenant-scoped session _latest_crawl uses."""

    def __init__(self, row):
        self._row = row

    async def execute(self, *_args, **_kwargs):
        return _FakeResult(self._row)


class _Row:
    def __init__(self, **kw):
        self.exploration_id = kw.get("exploration_id", "exp-1")
        self.status = kw.get("status", "completed")
        self.error = kw.get("error", "")
        self.stats = kw.get("stats", {})
        self.artifact_id = kw.get("artifact_id", "")
        self.started_at = kw.get("started_at")
        self.finished_at = kw.get("finished_at")


def _run(row):
    return asyncio.run(_latest_crawl(_FakeSession(row), "app-1"))


def test_never_crawled_app_still_gets_a_typed_diagnosis():
    out = _run(None)
    assert out["status"] == "none" and out["active"] is False
    assert out["diagnosis"]["code"] == cd.CODE_NONE
    assert out["diagnosis"]["remediation"]  # tells the client what to do next


def test_completed_crawl_emits_diagnosis():
    out = _run(_Row(status="completed", stats={"visits": 4, "generate": {"generated": 3}}))
    assert out["diagnosis"]["code"] == cd.CODE_COMPLETED_OK


def test_seed_blocked_crawl_emits_named_fields():
    out = _run(_Row(status="completed", stats={
        "visits": 4, "generate": {"generated": 0},
        "coverage": {"fields_needing_seed": ["From Account", "Payee"]},
    }))
    assert out["diagnosis"]["code"] == cd.CODE_SEEDS_NEEDED
    assert out["diagnosis"]["fields"] == ["From Account", "Payee"]


def test_stalled_crawl_uses_the_stall_valve_status_in_the_diagnosis():
    # Active status but far past its wall budget → the valve flips it to 'stalled',
    # and the diagnosis must reflect the EFFECTIVE status, not the raw one.
    old = datetime.now(timezone.utc) - timedelta(seconds=5000)
    out = _run(_Row(status="running", started_at=old, stats={"budget_wall_ms": 300_000}))
    assert out["status"] == "stalled"
    assert out["diagnosis"]["code"] == cd.CODE_STALLED


def test_every_branch_emits_a_diagnosis_key():
    for row in (None, _Row(status="running", started_at=datetime.now(timezone.utc)),
                _Row(status="failed", error="boom"), _Row(status="refused", error="nope")):
        assert "diagnosis" in _run(row), "every _latest_crawl branch must carry a diagnosis"
