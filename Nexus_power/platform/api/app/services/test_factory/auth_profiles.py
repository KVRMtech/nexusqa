"""Per-artifact authentication profile — a captured, authenticated browser session
(Playwright ``storageState``) so a generated test can run from a COLD session.

The session JSON contains auth cookies / tokens, so it is SENSITIVE. It is:
  * ENCRYPTED AT REST via the per-tenant ``EnvelopeService`` (the same path the
    qTest/TestRail push uses) — stored as ``EnvelopeBlob.to_bytes()`` in a BYTEA
    column, bound to the artifact via AAD so a blob can't be replayed elsewhere;
  * NEVER stored in plaintext, NEVER written into a test, NEVER returned to the
    client (only injected server-side into a run bundle as ``vkpower.auth.json``);
  * REFUSED (not silently dropped to plaintext) if encryption is unavailable.

The ORM model is defined here in app code (binds the SDK ``Base``); the table is
created out-of-band by an idempotent RLS migration (scripts/apply_auth_profiles.sql).
Mirrors the surface_prefs / run_screenshots pattern. Degrades safe pre-migration
(every helper swallows a missing table and behaves as "no profile").
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import DateTime, LargeBinary, String, delete as sql_delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from nexus_sdk.db import Base
from nexus_sdk.security.envelope import EnvelopeBlob

logger = logging.getLogger(__name__)

# A captured session well under this; the cap stops a runaway/garbage upload.
MAX_STORAGE_STATE_BYTES = 2 * 1024 * 1024  # 2 MiB


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class E2EAuthProfileRow(Base):
    """One encrypted authentication profile per (artifact, tenant)."""

    __tablename__ = "e2e_auth_profiles"

    artifact_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    blob: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)  # EnvelopeBlob.to_bytes()
    label: Mapped[str] = mapped_column(String(200), nullable=False, default="")  # non-secret note (host/user)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, onupdate=_utc_now, nullable=False,
    )


async def save_profile(
    session: AsyncSession,
    *,
    envelope,
    tenant_id: str,
    artifact_id: str,
    storage_state_json: str,
    label: str = "",
) -> None:
    """Encrypt + persist a captured session. Raises if encryption is unavailable
    (we never store a session in plaintext) or the payload is empty/oversize.
    Caller commits."""
    if not (storage_state_json or "").strip():
        raise ValueError("empty session")
    if len(storage_state_json.encode("utf-8")) > MAX_STORAGE_STATE_BYTES:
        raise ValueError("session too large")
    if envelope is None:
        raise RuntimeError("encryption unavailable — refusing to store a session in plaintext")
    blob = await envelope.encrypt(
        tenant_id, storage_state_json.encode("utf-8"), aad=artifact_id.encode("utf-8"),
    )
    raw = blob.to_bytes()
    stmt = (
        pg_insert(E2EAuthProfileRow)
        .values(
            artifact_id=artifact_id, tenant_id=tenant_id, blob=raw,
            label=(label or "")[:200], created_at=_utc_now(),
        )
        .on_conflict_do_update(
            index_elements=[E2EAuthProfileRow.artifact_id, E2EAuthProfileRow.tenant_id],
            set_={"blob": raw, "label": (label or "")[:200], "created_at": _utc_now()},
        )
    )
    await session.execute(stmt)


async def get_storage_state(
    session: AsyncSession, *, envelope, tenant_id: str, artifact_id: str,
) -> str | None:
    """Decrypt the stored session JSON for injection into a server-side run, or
    None if absent / encryption unavailable / pre-migration. Never raises."""
    try:
        row = (await session.execute(
            select(E2EAuthProfileRow).where(
                E2EAuthProfileRow.artifact_id == artifact_id,
                E2EAuthProfileRow.tenant_id == tenant_id,
            )
        )).scalar_one_or_none()
    except Exception as exc:  # table missing (pre-migration) / DB error
        logger.debug("auth_profiles.fetch_skipped artifact=%s err=%s", artifact_id, exc)
        return None
    if row is None or envelope is None:
        return None
    try:
        blob = EnvelopeBlob.from_bytes(bytes(row.blob))
        plaintext = await envelope.decrypt(
            tenant_id, blob, expected_aad=artifact_id.encode("utf-8"),
        )
        return plaintext.decode("utf-8")
    except Exception as exc:  # corrupt blob / decrypt failure — fail closed (no auth)
        logger.warning("auth_profiles.decrypt_failed artifact=%s err=%s", artifact_id, str(exc)[:200])
        return None


async def get_status(
    session: AsyncSession, *, tenant_id: str, artifact_id: str,
) -> dict:
    """Non-secret status for the UI (never returns the session)."""
    try:
        row = (await session.execute(
            select(E2EAuthProfileRow).where(
                E2EAuthProfileRow.artifact_id == artifact_id,
                E2EAuthProfileRow.tenant_id == tenant_id,
            )
        )).scalar_one_or_none()
    except Exception:
        return {"present": False}
    if row is None:
        return {"present": False}
    return {
        "present": True,
        "label": row.label or "",
        "captured_at": row.created_at.isoformat() if row.created_at else None,
    }


async def clear_profile(
    session: AsyncSession, *, tenant_id: str, artifact_id: str,
) -> bool:
    """Delete the stored profile. Caller commits. Returns True if a row existed."""
    res = await session.execute(
        sql_delete(E2EAuthProfileRow).where(
            E2EAuthProfileRow.artifact_id == artifact_id,
            E2EAuthProfileRow.tenant_id == tenant_id,
        )
    )
    return bool(res.rowcount)


__all__ = [
    "E2EAuthProfileRow", "save_profile", "get_storage_state", "get_status",
    "clear_profile", "MAX_STORAGE_STATE_BYTES",
]
