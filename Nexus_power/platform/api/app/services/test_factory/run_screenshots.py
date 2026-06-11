"""Per-step ACTUAL run screenshots — the 'This run' side of the baseline-vs-actual
two-up, and the input the drift / visual-change verdicts need.

The bundled nexus-reporter (running in the runner or in CI) uploads the
failure-state PNG; we store the bytes in Postgres (BYTEA), RLS-scoped per tenant,
and serve them back through a tokened URL the browser's <img> can load.
Self-contained — no object store — which fits the on-prem single-VM posture.

The ORM model is defined here in app code (binds the SDK's ``Base`` so it shares
the registry); the table is created out-of-band by an idempotent RLS migration
(``scripts/apply_run_screenshots.sql``). Nothing here auto-creates it. Mirrors the
``surface_prefs`` / ``script_versions`` pattern.

Degrades safely PRE-migration: ``store_screenshot`` raises, the reporter logs a
warning and the step's ``screenshot_url`` stays empty — so the timeline shows
'awaiting failure capture' exactly as it does today. No regression.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, LargeBinary, String, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from nexus_sdk.db import Base

# Hard ceiling on a stored screenshot — a full-page PNG is comfortably under this;
# the cap stops a runaway/garbage upload from bloating a row.
MAX_SCREENSHOT_BYTES = 8 * 1024 * 1024  # 8 MiB
_ALLOWED_CONTENT_TYPES = frozenset({"image/png", "image/jpeg", "image/webp"})


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return uuid.uuid4().hex


class E2ERunScreenshotRow(Base):
    """One actual run screenshot (typically the failure-state frame), tenant-scoped.

    ``run_id`` is a plain column (NOT an FK) so a screenshot can be uploaded
    before the run row is ingested — the reporter uploads per-test, then posts
    the run at onEnd.
    """

    __tablename__ = "e2e_run_screenshots"

    screenshot_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    artifact_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    scenario_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    step_number: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    content_type: Mapped[str] = mapped_column(String(64), nullable=False, default="image/png")
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    image: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, nullable=False,
    )


def normalize_content_type(ct: str | None) -> str:
    ct = (ct or "").split(";")[0].strip().lower()
    return ct if ct in _ALLOWED_CONTENT_TYPES else "image/png"


async def store_screenshot(
    session: AsyncSession,
    *,
    tenant_id: str,
    artifact_id: str,
    run_id: str,
    scenario_id: str,
    step_number: int,
    content_type: str,
    image: bytes,
) -> str:
    """Persist one screenshot; returns its id. Raises ValueError on an empty or
    oversize image. Caller commits (tenant_scoped_session does so on exit)."""
    if not image:
        raise ValueError("empty image")
    if len(image) > MAX_SCREENSHOT_BYTES:
        raise ValueError(f"image too large ({len(image)} bytes > {MAX_SCREENSHOT_BYTES})")
    sid = _new_id()
    session.add(
        E2ERunScreenshotRow(
            screenshot_id=sid,
            run_id=(run_id or "")[:64],
            artifact_id=(artifact_id or "")[:64],
            tenant_id=tenant_id,
            scenario_id=(scenario_id or "")[:64],
            step_number=int(step_number or 0),
            content_type=normalize_content_type(content_type),
            byte_size=len(image),
            image=image,
            created_at=_utc_now(),
        )
    )
    await session.flush()
    return sid


async def fetch_screenshot(
    session: AsyncSession, *, tenant_id: str, screenshot_id: str,
) -> tuple[str, bytes] | None:
    """(content_type, bytes) for a stored screenshot, tenant-scoped. None if absent."""
    row = (
        await session.execute(
            select(E2ERunScreenshotRow).where(
                E2ERunScreenshotRow.screenshot_id == screenshot_id,
                E2ERunScreenshotRow.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    return (row.content_type or "image/png", bytes(row.image))


__all__ = [
    "E2ERunScreenshotRow",
    "store_screenshot",
    "fetch_screenshot",
    "normalize_content_type",
    "MAX_SCREENSHOT_BYTES",
]
