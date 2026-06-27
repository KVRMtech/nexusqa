"""Per-agent ON/OFF preferences for the agentic-QE suite — governance + cost control.

A customer can turn OFF any agent they don't want (e.g. the LLM ``context`` / ``intent``
agents, to keep spend at $0). Resolution: per-tenant row -> Governor defaults.

Mirrors ``surface_prefs`` exactly: the ORM binds the SDK ``Base`` (shared registry); the
table is created out-of-band by an idempotent migration (scripts/apply_agentic_prefs.sql)
with the standard tenant RLS; nothing here auto-creates it; ``resolve`` FAIL-OPENS to the
Governor defaults pre-migration so the suite runs unchanged. A toggle only gates WHETHER
an agent runs — it can never make a step green.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, String, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from nexus_sdk.db import Base

from . import governor


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class AgenticPrefRow(Base):
    """One per-tenant agent on/off row. Defaults mirror the Governor ($0 agents ON,
    LLM agents OFF) so a tenant that never sets a preference is unaffected."""

    __tablename__ = "agentic_prefs"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    sentinel: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    context: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    triage: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    verdict: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    intent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, onupdate=_utc_now, nullable=False,
    )

    def as_dict(self) -> dict[str, bool]:
        return {a: bool(getattr(self, a, governor.default_prefs().get(a, False)))
                for a in governor.AGENTS}


async def resolve(session: AsyncSession, *, tenant_id: str) -> dict[str, bool]:
    """Effective ``{agent: bool}``: the per-tenant row, else the Governor defaults.
    Fail-open — ANY error (including the table not existing yet) returns the defaults,
    so the suite behaves identically pre-migration."""
    try:
        row = (await session.execute(
            select(AgenticPrefRow).where(AgenticPrefRow.tenant_id == tenant_id)
        )).scalar_one_or_none()
        if row is not None:
            return row.as_dict()
    except Exception:
        pass
    return governor.default_prefs()


async def get_effective(session: AsyncSession, *, tenant_id: str) -> dict:
    """Resolved prefs + the agent catalog (names + defaults) for the UI toggles."""
    return {
        "agents": await resolve(session, tenant_id=tenant_id),
        "defaults": governor.default_prefs(),
        "catalog": list(governor.AGENTS),
    }


async def set_prefs(session: AsyncSession, *, tenant_id: str, agents: dict) -> dict[str, bool]:
    """Upsert per-tenant toggles (only known agents kept; unknown keys ignored). Fail-open
    — returns the merged map even if persistence is unavailable (pre-migration)."""
    base = governor.default_prefs()
    for k, v in (agents or {}).items():
        if k in base:
            base[k] = bool(v)
    try:
        stmt = (
            pg_insert(AgenticPrefRow)
            .values(tenant_id=tenant_id, updated_at=_utc_now(), **base)
            .on_conflict_do_update(
                index_elements=[AgenticPrefRow.tenant_id],
                set_={**base, "updated_at": _utc_now()},
            )
        )
        await session.execute(stmt)
        await session.commit()
    except Exception:
        pass
    return base
