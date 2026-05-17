"""Circuit-breaker logic tests using an in-memory fake repository.

These tests pin the state machine behaviour without requiring a live
database. The transitions tested correspond directly to the persistence
shape in alembic migration ``019_knowledge_foundation``.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest

from nexus_sdk.feature_flags.circuit_breaker import (
    CircuitBreaker,
    CircuitDecision,
)
from nexus_sdk.feature_flags.models import (
    CircuitConfig,
    CircuitState,
    Outcome,
)


# ── Fake repository ──────────────────────────────────────────────


@dataclass
class _FakeRow:
    tenant_id: str
    feature_key: str
    state: str
    trip_count: int = 0
    failure_count_window: int = 0
    total_count_window: int = 0
    window_started_at: Optional[datetime] = None
    last_tripped_at: Optional[datetime] = None
    last_trip_reason: Optional[str] = None
    cooldown_until: Optional[datetime] = None
    updated_at: Optional[datetime] = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


class _FakeSession:
    """No-op async session; the breaker uses it only as a transaction handle."""

    async def execute(self, *_args, **_kwargs):  # noqa: D401
        return None

    async def commit(self):
        return None

    async def rollback(self):
        return None


class _FakeRepo:
    """In-memory drop-in for FlagRepository sufficient for breaker tests."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], _FakeRow] = {}

    @asynccontextmanager
    async def _session(self):
        yield _FakeSession()

    def session_scope(self):
        return self._session()

    async def fetch_circuit_for_update(
        self, _session, tenant_id: str, feature_key: str
    ):
        row = self.rows.get((tenant_id, feature_key))
        if row is None:
            return None
        return {
            "tenant_id": row.tenant_id,
            "feature_key": row.feature_key,
            "state": row.state,
            "trip_count": row.trip_count,
            "failure_count_window": row.failure_count_window,
            "total_count_window": row.total_count_window,
            "window_started_at": row.window_started_at,
            "last_tripped_at": row.last_tripped_at,
            "last_trip_reason": row.last_trip_reason,
            "cooldown_until": row.cooldown_until,
            "updated_at": row.updated_at,
        }


class _PatchingBreaker(CircuitBreaker):
    """CircuitBreaker variant that writes back to the fake repo's dict
    rather than executing SQL."""

    def __init__(self, repo: _FakeRepo, config: CircuitConfig) -> None:
        super().__init__(repo, config)  # type: ignore[arg-type]
        self._fake = repo

    async def _persist(
        self,
        *,
        session,
        tenant_id: str,
        feature_key: str,
        state: CircuitState,
        failure_count: int,
        total_count: int,
        window_started_at: datetime,
        cooldown_until: Optional[datetime],
        tripped: bool,
        last_trip_reason: Optional[str],
        trip_count_delta: int = 0,
    ) -> None:
        key = (tenant_id, feature_key)
        row = self._fake.rows.get(key)
        if row is None:
            row = _FakeRow(tenant_id=tenant_id, feature_key=feature_key, state=state.value)
            self._fake.rows[key] = row
        row.state = state.value
        row.trip_count += trip_count_delta
        row.failure_count_window = failure_count
        row.total_count_window = total_count
        row.window_started_at = window_started_at
        row.cooldown_until = cooldown_until
        row.updated_at = datetime.now(timezone.utc)
        if tripped:
            row.last_tripped_at = datetime.now(timezone.utc)
            row.last_trip_reason = last_trip_reason


@pytest.fixture
def breaker() -> _PatchingBreaker:
    repo = _FakeRepo()
    cfg = CircuitConfig(
        window_seconds=300,
        min_samples=10,
        failure_threshold=0.30,
        cooldown_seconds=60,
    )
    return _PatchingBreaker(repo, cfg)


# ── Tests ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_initial_outcome_keeps_circuit_closed(
    breaker: _PatchingBreaker,
) -> None:
    decision = await breaker.record_outcome("t1", "k", Outcome.SUCCESS)
    assert decision.state == CircuitState.CLOSED
    assert decision.tripped is False
    assert decision.window_samples == 1


@pytest.mark.asyncio
async def test_trips_when_threshold_exceeded(
    breaker: _PatchingBreaker,
) -> None:
    # 7 failures + 3 successes = 70% failure rate over 10 samples.
    # The 10th outcome is the first one that meets min_samples (10),
    # and the failure rate already exceeds 30%, so the trip must occur
    # exactly at the 10th outcome.
    for i in range(7):
        d = await breaker.record_outcome("t1", "k", Outcome.FAILURE)
        # Below min_samples — must not have tripped yet.
        assert d.state == CircuitState.CLOSED, (
            f"unexpected trip at sample {i + 1}"
        )
    # 8th and 9th outcomes (successes) still below min_samples (10).
    for i in range(2):
        d = await breaker.record_outcome("t1", "k", Outcome.SUCCESS)
        assert d.state == CircuitState.CLOSED, (
            f"unexpected trip at sample {8 + i}"
        )
    # 10th outcome — min_samples reached, threshold crossed; this is
    # the call that must observe the trip.
    decision = await breaker.record_outcome("t1", "k", Outcome.SUCCESS)
    assert decision.state == CircuitState.OPEN
    assert decision.tripped is True
    assert decision.cooldown_until is not None

    # 11th outcome — already OPEN, cooldown not yet elapsed; no further
    # transitions; tripped flag must be False.
    decision_after = await breaker.record_outcome(
        "t1", "k", Outcome.FAILURE
    )
    assert decision_after.state == CircuitState.OPEN
    assert decision_after.tripped is False
    assert decision_after.transitioned is False


@pytest.mark.asyncio
async def test_stays_closed_below_min_samples(
    breaker: _PatchingBreaker,
) -> None:
    # 9 outcomes, all failures — below min_samples (10) so no trip.
    for _ in range(9):
        decision = await breaker.record_outcome("t1", "k", Outcome.FAILURE)
        assert decision.state == CircuitState.CLOSED
        assert decision.tripped is False


@pytest.mark.asyncio
async def test_half_open_success_closes_circuit(
    breaker: _PatchingBreaker,
) -> None:
    # Force into OPEN.
    await breaker.force_trip("t1", "k", "test", "alice")
    assert breaker._fake.rows[("t1", "k")].state == CircuitState.OPEN.value

    # Backdate cooldown so the next outcome trips OPEN -> HALF_OPEN.
    row = breaker._fake.rows[("t1", "k")]
    row.cooldown_until = datetime.now(timezone.utc) - timedelta(seconds=1)

    decision = await breaker.record_outcome("t1", "k", Outcome.SUCCESS)
    assert decision.state == CircuitState.CLOSED
    assert decision.transitioned is True


@pytest.mark.asyncio
async def test_half_open_failure_reopens_circuit(
    breaker: _PatchingBreaker,
) -> None:
    await breaker.force_trip("t1", "k", "test", "alice")
    row = breaker._fake.rows[("t1", "k")]
    row.cooldown_until = datetime.now(timezone.utc) - timedelta(seconds=1)

    decision = await breaker.record_outcome("t1", "k", Outcome.FAILURE)
    assert decision.state == CircuitState.OPEN
    assert decision.tripped is True
    assert decision.transitioned is True


@pytest.mark.asyncio
async def test_thumbs_down_counts_as_failure(
    breaker: _PatchingBreaker,
) -> None:
    for _ in range(7):
        await breaker.record_outcome("t1", "k", Outcome.THUMBS_DOWN)
    for _ in range(3):
        await breaker.record_outcome("t1", "k", Outcome.THUMBS_UP)
    decision = await breaker.record_outcome("t1", "k", Outcome.THUMBS_DOWN)
    assert decision.state == CircuitState.OPEN


@pytest.mark.asyncio
async def test_window_rolls_after_elapse(
    breaker: _PatchingBreaker,
) -> None:
    # Record enough failures to be near tripping.
    for _ in range(5):
        await breaker.record_outcome("t1", "k", Outcome.FAILURE)
    # Backdate the window so it rolls on the next outcome.
    row = breaker._fake.rows[("t1", "k")]
    row.window_started_at = datetime.now(timezone.utc) - timedelta(seconds=400)
    decision = await breaker.record_outcome("t1", "k", Outcome.FAILURE)
    # New window — only one outcome so far, well below min_samples.
    assert decision.state == CircuitState.CLOSED
    assert decision.window_samples == 1


@pytest.mark.asyncio
async def test_reset_returns_to_closed_with_clean_counters(
    breaker: _PatchingBreaker,
) -> None:
    await breaker.force_trip("t1", "k", "test", "alice")
    await breaker.reset("t1", "k", "alice")
    row = breaker._fake.rows[("t1", "k")]
    assert row.state == CircuitState.CLOSED.value
    assert row.failure_count_window == 0
    assert row.total_count_window == 0
    assert row.cooldown_until is None


@pytest.mark.asyncio
async def test_force_trip_records_reason(
    breaker: _PatchingBreaker,
) -> None:
    await breaker.force_trip("t1", "k", "operator_alert", "ops-pager")
    row = breaker._fake.rows[("t1", "k")]
    assert row.state == CircuitState.OPEN.value
    assert row.cooldown_until is not None
    assert row.last_trip_reason is not None
    assert "operator_alert" in row.last_trip_reason
