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

import json
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


# ── Form-login profile (AUTH PERMANENCE) ─────────────────────────────────────
# A captured storageState (above) EXPIRES with the app's session TTL, so a
# server-side run's green is only reproducible until the cookie dies. A
# form-login profile instead stores the CREDENTIALS + the login form shape, so
# the runner's globalSetup logs in FRESH every run (compiler `_AUTH_SETUP_TS`,
# strategy 'form') and the green self-renews. Same encryption guarantees as the
# session profile: AAD-bound EnvelopeBlob, never plaintext, never returned. It
# reuses the e2e_auth_profiles table under a distinct pseudo-key so it needs no
# new migration and can't collide with the (real-artifact-keyed) session row.
_FORMLOGIN_KEY = "formlogin::{}".format

# Only these keys are persisted (defence-in-depth: never store stray fields).
_FORMLOGIN_FIELDS = ("login_path", "submit_label", "fields", "user", "password",
                     "user_env", "password_env")


async def save_form_login(
    session: AsyncSession, *, envelope, tenant_id: str, artifact_id: str,
    config: dict, label: str = "",
) -> None:
    """Encrypt + persist a form-login profile (credentials + form shape). Raises
    if encryption is unavailable (never plaintext) or the payload is empty.
    Caller commits."""
    clean = {k: config[k] for k in _FORMLOGIN_FIELDS if k in config}
    if not clean.get("user") or not clean.get("password"):
        raise ValueError("form-login needs a user and password")
    if envelope is None:
        raise RuntimeError("encryption unavailable — refusing to store credentials in plaintext")
    key = _FORMLOGIN_KEY(artifact_id)
    payload = json.dumps(clean).encode("utf-8")
    if len(payload) > MAX_STORAGE_STATE_BYTES:
        raise ValueError("form-login config too large")
    blob = await envelope.encrypt(tenant_id, payload, aad=key.encode("utf-8"))
    raw = blob.to_bytes()
    stmt = (
        pg_insert(E2EAuthProfileRow)
        .values(artifact_id=key, tenant_id=tenant_id, blob=raw,
                label=(label or "form-login")[:200], created_at=_utc_now())
        .on_conflict_do_update(
            index_elements=[E2EAuthProfileRow.artifact_id, E2EAuthProfileRow.tenant_id],
            set_={"blob": raw, "label": (label or "form-login")[:200], "created_at": _utc_now()},
        )
    )
    await session.execute(stmt)


async def get_form_login(
    session: AsyncSession, *, envelope, tenant_id: str, artifact_id: str,
) -> dict | None:
    """Decrypt the stored form-login profile (config + creds) for a server-side
    run, or None if absent / encryption unavailable / pre-migration. Never raises."""
    key = _FORMLOGIN_KEY(artifact_id)
    try:
        row = (await session.execute(
            select(E2EAuthProfileRow).where(
                E2EAuthProfileRow.artifact_id == key,
                E2EAuthProfileRow.tenant_id == tenant_id,
            )
        )).scalar_one_or_none()
    except Exception as exc:
        logger.debug("form_login.fetch_skipped artifact=%s err=%s", artifact_id, exc)
        return None
    if row is None or envelope is None:
        return None
    try:
        blob = EnvelopeBlob.from_bytes(bytes(row.blob))
        plaintext = await envelope.decrypt(tenant_id, blob, expected_aad=key.encode("utf-8"))
        data = json.loads(plaintext.decode("utf-8"))
        return data if isinstance(data, dict) else None
    except Exception as exc:
        logger.warning("form_login.decrypt_failed artifact=%s err=%s", artifact_id, str(exc)[:200])
        return None


def build_form_login_bundle(config: dict) -> tuple[dict, dict]:
    """Split a stored form-login profile into (auth_config, login_env):
      * auth_config → written to vkpower.auth.config.json (strategy 'form',
        NON-secret: login path + form field shape only);
      * login_env   → NEXUS_LOGIN_USER / NEXUS_LOGIN_PASSWORD for the run env
        (the SECRETS, kept out of the bundle exactly like the compiler intends).
    The runner's globalSetup reads both and logs in fresh before the suite."""
    user_env = config.get("user_env") or "NEXUS_LOGIN_USER"
    pass_env = config.get("password_env") or "NEXUS_LOGIN_PASSWORD"
    fields = config.get("fields") or [
        {"label": "Email", "value": "user"},
        {"label": "Password", "value": "password"},
    ]
    auth_config = {
        "strategy": "form",
        "loginPath": config.get("login_path") or "/",
        "userEnv": user_env,
        "passwordEnv": pass_env,
        "submitLabel": config.get("submit_label") or "Sign in",
        "fields": fields,
    }
    login_env = {user_env: config.get("user") or "", pass_env: config.get("password") or ""}
    return auth_config, login_env


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
    "save_form_login", "get_form_login", "build_form_login_bundle",
    "clear_profile", "MAX_STORAGE_STATE_BYTES",
]
