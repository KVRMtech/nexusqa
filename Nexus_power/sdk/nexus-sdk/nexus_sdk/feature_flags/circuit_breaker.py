"""Persisted circuit breaker for the feature flag service.

The breaker observes outcomes within a tumbling time window. When the
failure rate exceeds ``failure_threshold`` and the window contains at
least ``min_samples`` outcomes, the circuit trips to OPEN and stays
there until ``cooldown_seconds`` elapses; the next outcome observed
afterward closes the circuit on success or re-opens it on failure
(HALF_OPEN canary).

Why tumbling vs. true sliding: we only need an eventually-consistent
view of failure rate. Tracking every outcome event by timestamp is
significantly more expensive and offers no operational advantage at
the breaker's decision granularity. Tumbling windows are easy to
reason about, idempotent under restart, and trivially testable.

Concurrency model: every update path is wrapped in a single
``SELECT FOR UPDATE`` transaction on ``feature_circuit_state``, so
concurrent callers serialize on the row lock.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from .models import CircuitConfig, CircuitState, Outcome, is_failure_outcome
from .repository import (
    FlagRepository,
    _now,
    _set_tenant_context,
    feature_circuit_state,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CircuitDecision:
    """Outcome of a record_outcome call."""

    state: CircuitState
    tripped: bool
    transitioned: bool
    failure_rate: float
    window_samples: int
    cooldown_until: Optional[datetime]


class CircuitBreaker:
    """Stateless service layer; all state lives in feature_circuit_state."""

    def __init__(self, repo: FlagRepository, config: CircuitConfig):
        self._repo = repo
        self._cfg = config

    # ── Outcome recording ────────────────────────────────────────

    async def record_outcome(
        self,
        tenant_id: str,
        feature_key: str,
        outcome: Outcome,
    ) -> CircuitDecision:
        """Apply an outcome to the breaker, returning the new state.

        Always returns synchronously even if the row is contended;
        SELECT FOR UPDATE serializes the update under a row lock.
        """
        now = _now()
        async with self._repo.session_scope() as session:
            await _set_tenant_context(session, tenant_id)
            row = await self._repo.fetch_circuit_for_update(
                session, tenant_id, feature_key
            )

            state_before = (
                CircuitState(row["state"]) if row else CircuitState.CLOSED
            )
            failure_count = row["failure_count_window"] if row else 0
            total_count = row["total_count_window"] if row else 0
            window_started = (
                row["window_started_at"]
                if row and row["window_started_at"]
                else now
            )
            cooldown_until = row["cooldown_until"] if row else None

            # Roll the window if it has elapsed.
            window_end = window_started + timedelta(
                seconds=self._cfg.window_seconds
            )
            if now >= window_end:
                window_started = now
                failure_count = 0
                total_count = 0

            # Apply the outcome.
            total_count += 1
            if is_failure_outcome(outcome):
                failure_count += 1

            # State transition logic.
            new_state = state_before
            tripped_now = False
            transitioned = False
            trip_reason: Optional[str] = None

            if state_before == CircuitState.OPEN:
                if cooldown_until is None or now >= cooldown_until:
                    new_state = CircuitState.HALF_OPEN
                    transitioned = True

            if new_state == CircuitState.HALF_OPEN:
                if is_failure_outcome(outcome):
                    new_state = CircuitState.OPEN
                    tripped_now = True
                    cooldown_until = now + timedelta(
                        seconds=self._cfg.cooldown_seconds
                    )
                    trip_reason = (
                        f"half_open canary failed: outcome={outcome.value}"
                    )
                    # Reset window so we start fresh after eventual recovery.
                    window_started = now
                    failure_count = 0
                    total_count = 0
                    transitioned = True
                else:
                    new_state = CircuitState.CLOSED
                    cooldown_until = None
                    window_started = now
                    failure_count = 0
                    total_count = 0
                    transitioned = True
            elif new_state == CircuitState.CLOSED:
                if (
                    total_count >= self._cfg.min_samples
                    and (failure_count / total_count) >= self._cfg.failure_threshold
                ):
                    new_state = CircuitState.OPEN
                    tripped_now = True
                    cooldown_until = now + timedelta(
                        seconds=self._cfg.cooldown_seconds
                    )
                    transitioned = True
                    trip_reason = (
                        f"threshold breached: "
                        f"{failure_count}/{total_count} "
                        f">= {self._cfg.failure_threshold:.2f}"
                    )

            await self._persist(
                session=session,
                tenant_id=tenant_id,
                feature_key=feature_key,
                state=new_state,
                failure_count=failure_count,
                total_count=total_count,
                window_started_at=window_started,
                cooldown_until=cooldown_until,
                tripped=tripped_now,
                last_trip_reason=trip_reason,
                trip_count_delta=1 if tripped_now else 0,
            )
            await session.commit()

            failure_rate = (
                failure_count / total_count if total_count else 0.0
            )
            return CircuitDecision(
                state=new_state,
                tripped=tripped_now,
                transitioned=transitioned,
                failure_rate=failure_rate,
                window_samples=total_count,
                cooldown_until=cooldown_until,
            )

    # ── Lazy recovery on read ────────────────────────────────────

    async def maybe_recover(
        self, tenant_id: str, feature_key: str
    ) -> Optional[CircuitState]:
        """If the circuit is OPEN past its cooldown, move it to HALF_OPEN.

        Called from the read path; returns the new state on transition
        or None when no change was made.
        """
        async with self._repo.session_scope() as session:
            await _set_tenant_context(session, tenant_id)
            row = await self._repo.fetch_circuit_for_update(
                session, tenant_id, feature_key
            )
            if not row:
                return None
            state = CircuitState(row["state"])
            if state != CircuitState.OPEN:
                return None
            cooldown_until = row["cooldown_until"]
            if cooldown_until is None:
                return None
            if _now() < cooldown_until:
                return None

            await self._persist(
                session=session,
                tenant_id=tenant_id,
                feature_key=feature_key,
                state=CircuitState.HALF_OPEN,
                failure_count=row["failure_count_window"],
                total_count=row["total_count_window"],
                window_started_at=row["window_started_at"],
                cooldown_until=cooldown_until,
                tripped=False,
                last_trip_reason=None,
            )
            await session.commit()
            return CircuitState.HALF_OPEN

    # ── Operator controls ────────────────────────────────────────

    async def force_trip(
        self,
        tenant_id: str,
        feature_key: str,
        reason: str,
        actor: str,
    ) -> CircuitState:
        """Explicit operator trip — bypasses thresholds."""
        async with self._repo.session_scope() as session:
            await _set_tenant_context(session, tenant_id)
            now = _now()
            cooldown_until = now + timedelta(
                seconds=self._cfg.cooldown_seconds
            )
            await self._persist(
                session=session,
                tenant_id=tenant_id,
                feature_key=feature_key,
                state=CircuitState.OPEN,
                failure_count=0,
                total_count=0,
                window_started_at=now,
                cooldown_until=cooldown_until,
                tripped=True,
                last_trip_reason=f"manual:{actor}:{reason}",
                trip_count_delta=1,
            )
            await session.commit()
        return CircuitState.OPEN

    async def reset(
        self,
        tenant_id: str,
        feature_key: str,
        actor: str,
    ) -> CircuitState:
        """Explicit operator reset — clears window, closes circuit."""
        async with self._repo.session_scope() as session:
            await _set_tenant_context(session, tenant_id)
            now = _now()
            await self._persist(
                session=session,
                tenant_id=tenant_id,
                feature_key=feature_key,
                state=CircuitState.CLOSED,
                failure_count=0,
                total_count=0,
                window_started_at=now,
                cooldown_until=None,
                tripped=False,
                last_trip_reason=f"manual_reset:{actor}",
            )
            await session.commit()
        return CircuitState.CLOSED

    # ── Internals ────────────────────────────────────────────────

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
        now = _now()
        existing = await self._repo.fetch_circuit_for_update(
            session, tenant_id, feature_key
        )
        new_trip_count = (
            (existing["trip_count"] if existing else 0) + trip_count_delta
        )

        values: dict = {
            "tenant_id": tenant_id,
            "feature_key": feature_key,
            "state": state.value,
            "trip_count": new_trip_count,
            "failure_count_window": failure_count,
            "total_count_window": total_count,
            "window_started_at": window_started_at,
            "cooldown_until": cooldown_until,
            "updated_at": now,
        }
        if tripped:
            values["last_tripped_at"] = now
            values["last_trip_reason"] = last_trip_reason

        stmt = pg_insert(feature_circuit_state).values(**values)
        update_cols = {
            "state": stmt.excluded.state,
            "trip_count": stmt.excluded.trip_count,
            "failure_count_window": stmt.excluded.failure_count_window,
            "total_count_window": stmt.excluded.total_count_window,
            "window_started_at": stmt.excluded.window_started_at,
            "cooldown_until": stmt.excluded.cooldown_until,
            "updated_at": stmt.excluded.updated_at,
        }
        if tripped:
            update_cols["last_tripped_at"] = stmt.excluded.last_tripped_at
            update_cols["last_trip_reason"] = stmt.excluded.last_trip_reason

        stmt = stmt.on_conflict_do_update(
            index_elements=[
                feature_circuit_state.c.tenant_id,
                feature_circuit_state.c.feature_key,
            ],
            set_=update_cols,
        )
        await session.execute(stmt)
