"""SQLAlchemy Core repository for feature flag + circuit breaker tables.

Self-contained — does not depend on ``nexus_sdk.db.models`` Base. The
authoritative schema is defined in alembic migration
``019_knowledge_foundation``. The Table objects here are query-only
projections and must stay structurally aligned with that migration.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from .models import CircuitState, FlagState, Mode

logger = logging.getLogger(__name__)


_metadata = sa.MetaData()


tenant_feature_flags = sa.Table(
    "tenant_feature_flags",
    _metadata,
    sa.Column("id", sa.BigInteger, primary_key=True),
    sa.Column("tenant_id", sa.String(64), nullable=False),
    sa.Column("feature_key", sa.String(128), nullable=False),
    sa.Column("enabled", sa.Boolean, nullable=False),
    sa.Column("mode", sa.String(32), nullable=False),
    sa.Column("config", JSONB, nullable=False),
    sa.Column("enabled_by", sa.String(128)),
    sa.Column("enabled_at", sa.DateTime(timezone=True)),
    sa.Column("disabled_at", sa.DateTime(timezone=True)),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("version", sa.Integer, nullable=False),
)


feature_circuit_state = sa.Table(
    "feature_circuit_state",
    _metadata,
    sa.Column("tenant_id", sa.String(64), primary_key=True),
    sa.Column("feature_key", sa.String(128), primary_key=True),
    sa.Column("state", sa.String(16), nullable=False),
    sa.Column("trip_count", sa.Integer, nullable=False),
    sa.Column("failure_count_window", sa.Integer, nullable=False),
    sa.Column("total_count_window", sa.Integer, nullable=False),
    sa.Column("window_started_at", sa.DateTime(timezone=True)),
    sa.Column("last_tripped_at", sa.DateTime(timezone=True)),
    sa.Column("last_trip_reason", sa.Text),
    sa.Column("cooldown_until", sa.DateTime(timezone=True)),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _set_tenant_context(session: AsyncSession, tenant_id: str) -> None:
    """Apply RLS session variable for the current transaction."""
    await session.execute(
        sa.text("SELECT set_config('nexus.current_tenant_id', :tid, true)"),
        {"tid": tenant_id},
    )


class FlagRepository:
    """Async DB access for tenant_feature_flags + feature_circuit_state."""

    def __init__(self, session_factory):
        self._sf = session_factory

    # ── Reads ────────────────────────────────────────────────────

    async def get(
        self, tenant_id: str, feature_key: str
    ) -> Optional[FlagState]:
        async with self._sf() as session:
            await _set_tenant_context(session, tenant_id)
            row = (
                await session.execute(
                    sa.select(tenant_feature_flags).where(
                        tenant_feature_flags.c.tenant_id == tenant_id,
                        tenant_feature_flags.c.feature_key == feature_key,
                    )
                )
            ).mappings().first()
            if row is None:
                return None
            cb = await self._get_circuit(session, tenant_id, feature_key)
            return _row_to_state(row, cb)

    async def list_for_tenant(self, tenant_id: str) -> list[FlagState]:
        async with self._sf() as session:
            await _set_tenant_context(session, tenant_id)
            rows = (
                await session.execute(
                    sa.select(tenant_feature_flags)
                    .where(tenant_feature_flags.c.tenant_id == tenant_id)
                    .order_by(tenant_feature_flags.c.feature_key)
                )
            ).mappings().all()
            states: list[FlagState] = []
            for row in rows:
                cb = await self._get_circuit(
                    session, tenant_id, row["feature_key"]
                )
                states.append(_row_to_state(row, cb))
            return states

    async def _get_circuit(
        self,
        session: AsyncSession,
        tenant_id: str,
        feature_key: str,
    ) -> Optional[dict]:
        result = await session.execute(
            sa.select(feature_circuit_state).where(
                feature_circuit_state.c.tenant_id == tenant_id,
                feature_circuit_state.c.feature_key == feature_key,
            )
        )
        m = result.mappings().first()
        return dict(m) if m else None

    # ── Writes ───────────────────────────────────────────────────

    async def upsert(
        self,
        *,
        tenant_id: str,
        feature_key: str,
        enabled: bool,
        mode: Mode,
        config: dict,
        actor: str,
        expected_version: Optional[int],
    ) -> tuple[bool, Optional[FlagState]]:
        """Insert or update a flag row.

        Returns (success, new_state). When success is False the caller
        encountered an optimistic-lock conflict; new_state is None.
        """
        async with self._sf() as session:
            await _set_tenant_context(session, tenant_id)
            now = _now()
            # Try update first.
            existing = (
                await session.execute(
                    sa.select(tenant_feature_flags).where(
                        tenant_feature_flags.c.tenant_id == tenant_id,
                        tenant_feature_flags.c.feature_key == feature_key,
                    )
                )
            ).mappings().first()

            if existing is not None:
                if (
                    expected_version is not None
                    and existing["version"] != expected_version
                ):
                    return False, None

                values: dict = {
                    "enabled": enabled,
                    "mode": mode.value,
                    "config": _normalize_config(config),
                    "enabled_by": actor,
                }
                if enabled and not existing["enabled"]:
                    values["enabled_at"] = now
                if existing["enabled"] and not enabled:
                    values["disabled_at"] = now

                # Trigger nexus_tff_touch handles updated_at + version.
                upd = (
                    sa.update(tenant_feature_flags)
                    .where(
                        tenant_feature_flags.c.tenant_id == tenant_id,
                        tenant_feature_flags.c.feature_key == feature_key,
                        tenant_feature_flags.c.version == existing["version"],
                    )
                    .values(**values)
                    .returning(tenant_feature_flags)
                )
                result = await session.execute(upd)
                row = result.mappings().first()
                if row is None:
                    # Race lost.
                    await session.rollback()
                    return False, None
                await session.commit()
                cb = await self._get_circuit(session, tenant_id, feature_key)
                return True, _row_to_state(row, cb)

            # Insert.
            ins_values = {
                "tenant_id": tenant_id,
                "feature_key": feature_key,
                "enabled": enabled,
                "mode": mode.value,
                "config": _normalize_config(config),
                "enabled_by": actor,
                "enabled_at": now if enabled else None,
                "created_at": now,
                "updated_at": now,
                "version": 1,
            }
            result = await session.execute(
                sa.insert(tenant_feature_flags)
                .values(**ins_values)
                .returning(tenant_feature_flags)
            )
            row = result.mappings().first()
            await session.commit()
            return True, _row_to_state(row, None)

    # ── Circuit breaker ──────────────────────────────────────────

    async def upsert_circuit(
        self,
        *,
        tenant_id: str,
        feature_key: str,
        state: CircuitState,
        trip_count_delta: int = 0,
        failure_count: int,
        total_count: int,
        window_started_at: Optional[datetime],
        cooldown_until: Optional[datetime],
        last_trip_reason: Optional[str] = None,
        tripped: bool = False,
    ) -> dict:
        async with self._sf() as session:
            await _set_tenant_context(session, tenant_id)
            now = _now()
            existing = await self._get_circuit(session, tenant_id, feature_key)
            new_trip_count = (existing["trip_count"] if existing else 0) + trip_count_delta
            values = {
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

            stmt = (
                sa.dialects.postgresql.insert(feature_circuit_state)
                .values(**values)
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[
                    feature_circuit_state.c.tenant_id,
                    feature_circuit_state.c.feature_key,
                ],
                set_={
                    k: stmt.excluded[k]
                    for k in (
                        "state",
                        "trip_count",
                        "failure_count_window",
                        "total_count_window",
                        "window_started_at",
                        "cooldown_until",
                        "updated_at",
                    )
                    | ({"last_tripped_at", "last_trip_reason"} if tripped else set())
                },
            )
            await session.execute(stmt)
            await session.commit()
            row = await self._get_circuit(session, tenant_id, feature_key)
            return row or {}

    async def fetch_circuit_for_update(
        self, session: AsyncSession, tenant_id: str, feature_key: str
    ) -> Optional[dict]:
        """Row-locking read used inside record_outcome transactions."""
        result = await session.execute(
            sa.select(feature_circuit_state)
            .where(
                feature_circuit_state.c.tenant_id == tenant_id,
                feature_circuit_state.c.feature_key == feature_key,
            )
            .with_for_update()
        )
        m = result.mappings().first()
        return dict(m) if m else None

    def session_scope(self):
        """Return an async context manager opening a new tenant session."""
        return self._sf()


# ── Helpers ──────────────────────────────────────────────────────


def _normalize_config(config: Optional[dict]) -> dict:
    """Round-trip via JSON to ensure JSONB compatibility and detect cycles."""
    if config is None:
        return {}
    try:
        return json.loads(json.dumps(config, default=str))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"config is not JSON-serializable: {exc}") from exc


def _row_to_state(row, circuit_row: Optional[dict]) -> FlagState:
    return FlagState(
        tenant_id=row["tenant_id"],
        feature_key=row["feature_key"],
        enabled=row["enabled"],
        mode=Mode(row["mode"]),
        config=row["config"] or {},
        version=row["version"],
        circuit_state=(
            CircuitState(circuit_row["state"])
            if circuit_row
            else CircuitState.CLOSED
        ),
        cooldown_until=(circuit_row or {}).get("cooldown_until"),
        enabled_by=row.get("enabled_by"),
        enabled_at=row.get("enabled_at"),
        updated_at=row.get("updated_at"),
    )
