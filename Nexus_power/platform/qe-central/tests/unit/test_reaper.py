"""Phase 0 WS-B — tests for the stale-crawl reaper's PURE staleness predicate.

The reap WRITE (fleet-wide conditional UPDATE) is DB-gated and covered by an
integration test where a Postgres DSN is available; these unit tests pin the
deterministic selection logic that decides WHICH rows are stale — including the
wall-budget window, the un-stamped fallback, the queue window, the naive-timestamp
handling, and the never-reap-a-row-with-no-age guard.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.controlplane import reaper


NOW = datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc)


def _ago(seconds: float) -> datetime:
    return NOW - timedelta(seconds=seconds)


def test_stale_after_uses_wall_budget_plus_grace():
    # 300s wall budget + 180s grace = 480s window.
    assert reaper.stale_after_seconds(
        "running", {"budget_wall_ms": 300_000}, grace_s=180, queue_max_wait_s=3600,
    ) == 480.0


def test_stale_after_falls_back_to_default_wall_when_unstamped():
    assert reaper.stale_after_seconds(
        "writing", {}, grace_s=180, queue_max_wait_s=3600,
    ) == reaper._DEFAULT_WALL_S + 180


def test_queued_uses_queue_max_wait():
    assert reaper.stale_after_seconds(
        "queued", {}, grace_s=60, queue_max_wait_s=3600,
    ) == 3660.0


def test_running_within_budget_is_not_stale():
    assert not reaper.is_stale(
        status="running", started_at=_ago(120), created_at=_ago(130),
        stats={"budget_wall_ms": 300_000}, now=NOW, grace_s=180, queue_max_wait_s=3600,
    )


def test_running_past_budget_plus_grace_is_stale():
    assert reaper.is_stale(
        status="running", started_at=_ago(600), created_at=_ago(610),
        stats={"budget_wall_ms": 300_000}, now=NOW, grace_s=180, queue_max_wait_s=3600,
    )


def test_unstamped_row_stale_only_after_default_ceiling():
    # 1800 + 180 = 1980s window.
    assert not reaper.is_stale(
        status="pending", started_at=None, created_at=_ago(1900),
        stats={}, now=NOW, grace_s=180, queue_max_wait_s=3600,
    )
    assert reaper.is_stale(
        status="pending", started_at=None, created_at=_ago(2100),
        stats={}, now=NOW, grace_s=180, queue_max_wait_s=3600,
    )


def test_pending_uses_created_at_when_no_started_at():
    assert reaper.is_stale(
        status="pending", started_at=None, created_at=_ago(2500),
        stats={}, now=NOW, grace_s=180, queue_max_wait_s=3600,
    )


def test_never_reap_row_with_no_measurable_age():
    assert not reaper.is_stale(
        status="running", started_at=None, created_at=None,
        stats={}, now=NOW, grace_s=180, queue_max_wait_s=3600,
    )


def test_naive_started_at_is_treated_as_utc():
    naive = (NOW - timedelta(seconds=600)).replace(tzinfo=None)
    assert reaper.is_stale(
        status="running", started_at=naive, created_at=None,
        stats={"budget_wall_ms": 300_000}, now=NOW, grace_s=180, queue_max_wait_s=3600,
    )


def test_malformed_budget_wall_ms_falls_back_safely():
    # A non-numeric budget must not crash; falls back to the default window.
    assert reaper.stale_after_seconds(
        "running", {"budget_wall_ms": "oops"}, grace_s=180, queue_max_wait_s=3600,
    ) == reaper._DEFAULT_WALL_S + 180


def test_reason_for_distinguishes_queue_timeout():
    assert "queue timeout" in reaper._reason_for("queued")
    assert "no completion callback" in reaper._reason_for("running")


def test_active_statuses_cover_phase2_queue_states():
    assert {"pending", "writing", "running", "dispatched", "queued", "claimed"} == set(
        reaper.ACTIVE_STATUSES
    )
