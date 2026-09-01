"""Per-surface derivation preferences — cost control for the visual-evidence layer.

A customer can turn OFF surfaces they don't need so the expensive vision (gpt-4o)
extractors never run for them ($0 + faster):

  * ``storyboard``      → gates the ``action_extractor`` (gpt-4o scene actions)
  * ``pages_forms``     → gates ``page_visit`` / ``page_action`` / ``form_snapshot``
                          (gpt-4o); NOTE: Test Cases need this, so turning it off
                          disables test generation (the UI degrades gracefully)
  * ``three_d_journey`` → display-only (reads the always-on, deterministic
                          scene_grouper output — no extractor to gate, ~$0)

Resolution order (most specific wins): per-artifact override → per-tenant default
→ system default (everything ON, so existing behavior is byte-identical unless a
customer opts out).

The ORM model is defined here in app code (binds the SDK's ``Base`` so it shares
the registry); the table is created out-of-band by an idempotent migration
(infrastructure SQL) with the standard tenant RLS policy. Nothing here
auto-creates it. Mirrors the ``script_versions`` pattern.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Boolean, DateTime, String, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from nexus_sdk.db import Base

# The three toggleable surfaces. System default = all ON (preserve behavior).
SURFACES: tuple[str, ...] = ("storyboard", "pages_forms", "three_d_journey")
_TENANT_DEFAULT_KEY = ""  # artifact_id sentinel for the per-tenant default row


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class SurfacePrefRow(Base):
    """One surface-preference row.

    ``artifact_id == ''`` is the per-tenant DEFAULT; any other value is a
    per-artifact override. Composite PK keeps one row per (tenant, artifact).
    """

    __tablename__ = "surface_prefs"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    artifact_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_TENANT_DEFAULT_KEY)

    storyboard: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    pages_forms: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    three_d_journey: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, onupdate=_utc_now, nullable=False,
    )

    def as_dict(self) -> dict[str, bool]:
        return {
            "storyboard": bool(self.storyboard),
            "pages_forms": bool(self.pages_forms),
            "three_d_journey": bool(self.three_d_journey),
        }


def _system_default() -> dict[str, bool]:
    """Everything ON — so an org that never sets a preference is unaffected."""
    return {s: True for s in SURFACES}


def _coerce(surfaces: dict[str, Any] | None) -> dict[str, bool]:
    base = _system_default()
    for s in SURFACES:
        if surfaces and s in surfaces:
            base[s] = bool(surfaces[s])
    return base


async def _fetch(session: AsyncSession, *, tenant_id: str, artifact_id: str) -> SurfacePrefRow | None:
    return (
        await session.execute(
            select(SurfacePrefRow).where(
                SurfacePrefRow.tenant_id == tenant_id,
                SurfacePrefRow.artifact_id == artifact_id,
            )
        )
    ).scalar_one_or_none()


async def resolve_surfaces(
    session: AsyncSession, *, tenant_id: str, artifact_id: str,
) -> dict[str, bool]:
    """Effective enabled-set for an artifact: override → tenant default → all-on.

    Never raises — if the table doesn't exist yet (pre-migration) the caller's
    surrounding try/except keeps the all-on default so derivation is unchanged.
    """
    row = await _fetch(session, tenant_id=tenant_id, artifact_id=artifact_id)
    if row is not None:
        return row.as_dict()
    tenant_row = await _fetch(session, tenant_id=tenant_id, artifact_id=_TENANT_DEFAULT_KEY)
    if tenant_row is not None:
        return tenant_row.as_dict()
    return _system_default()


async def get_effective(
    session: AsyncSession, *, tenant_id: str, artifact_id: str,
) -> dict[str, Any]:
    """The resolved prefs plus where each came from (for the UI)."""
    override = await _fetch(session, tenant_id=tenant_id, artifact_id=artifact_id)
    tenant_row = await _fetch(session, tenant_id=tenant_id, artifact_id=_TENANT_DEFAULT_KEY)
    if override is not None:
        source = "artifact"
        effective = override.as_dict()
    elif tenant_row is not None:
        source = "tenant_default"
        effective = tenant_row.as_dict()
    else:
        source = "system_default"
        effective = _system_default()
    return {
        "surfaces": effective,
        "source": source,
        "tenant_default": tenant_row.as_dict() if tenant_row else None,
        "has_artifact_override": override is not None,
    }


async def _upsert(
    session: AsyncSession, *, tenant_id: str, artifact_id: str, surfaces: dict[str, Any],
) -> dict[str, bool]:
    vals = _coerce(surfaces)
    stmt = (
        pg_insert(SurfacePrefRow)
        .values(tenant_id=tenant_id, artifact_id=artifact_id, updated_at=_utc_now(), **vals)
        .on_conflict_do_update(
            index_elements=[SurfacePrefRow.tenant_id, SurfacePrefRow.artifact_id],
            set_={**vals, "updated_at": _utc_now()},
        )
    )
    await session.execute(stmt)
    await session.commit()
    return vals


async def set_artifact_override(
    session: AsyncSession, *, tenant_id: str, artifact_id: str, surfaces: dict[str, Any],
) -> dict[str, bool]:
    """Set a per-artifact override (what a recording actually derives)."""
    return await _upsert(session, tenant_id=tenant_id, artifact_id=artifact_id, surfaces=surfaces)


async def set_tenant_default(
    session: AsyncSession, *, tenant_id: str, surfaces: dict[str, Any],
) -> dict[str, bool]:
    """Set the org-wide default applied to artifacts without their own override."""
    return await _upsert(
        session, tenant_id=tenant_id, artifact_id=_TENANT_DEFAULT_KEY, surfaces=surfaces,
    )
