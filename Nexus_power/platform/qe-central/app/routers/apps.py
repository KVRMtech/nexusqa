"""QE-Central — client app registry (``/api/v1/qec/apps``, R-8 merged table).

POST registers a client application; credentials are envelope-encrypted
(KMS, AAD = ``app_id``) via the SAME refuse-plaintext discipline as
``auth_profiles.save_profile`` (503 — never a silent plaintext fallback).
Credentials are NEVER echoed back; responses expose ``has_credentials``
only.  Reads are open to any authenticated tenant member; mutations
require admin|manager (platform-api RBAC parity).
"""
from __future__ import annotations

import json
import logging
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..auth import require_auth, require_role
from ..db import new_id, row_to_dict, tenant_scoped_qec_session, utc_now
from ..db.models import ClientAppRow

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/qec", tags=["QEC Apps"])

_MUTATE = require_role("admin", "manager")

# Statuses an operator may set via PATCH; 'deleted' only via DELETE.
_SETTABLE_STATUSES = frozenset({"active", "paused"})


class AppCreate(BaseModel):
    """Registration payload for one client application."""

    name: str = Field(min_length=1, max_length=200)
    base_url: str = Field(min_length=1, max_length=2000)
    canonical_host: str = Field(default="", max_length=500)
    # Login credentials — envelope-encrypted at rest, never echoed.
    credentials: dict | None = None
    answer_key: dict = Field(default_factory=dict)
    env_attestation: dict = Field(default_factory=dict)
    fences: dict = Field(default_factory=dict)
    repo_binding: dict = Field(default_factory=dict)
    schedule: dict = Field(default_factory=dict)
    budgets: dict = Field(default_factory=dict)


class AppUpdate(BaseModel):
    """Partial update; every field optional.  ``credentials`` rotates the
    envelope blob; ``status`` may be 'active'|'paused'."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    base_url: str | None = Field(default=None, min_length=1, max_length=2000)
    canonical_host: str | None = Field(default=None, max_length=500)
    credentials: dict | None = None
    answer_key: dict | None = None
    env_attestation: dict | None = None
    fences: dict | None = None
    repo_binding: dict | None = None
    schedule: dict | None = None
    budgets: dict | None = None
    status: str | None = None


def _validated_base_url(base_url: str) -> str:
    """Require an absolute http(s) URL; return it normalised (stripped)."""
    url = (base_url or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise HTTPException(
            status_code=422, detail="base_url must be an absolute http(s) URL",
        )
    return url


def _derive_canonical_host(base_url: str, explicit: str) -> str:
    """Use the explicit canonical_host when given, else the URL hostname."""
    host = (explicit or "").strip().lower()
    if host:
        return host[:500]
    return (urlparse(base_url).hostname or "").lower()[:500]


async def _encrypt_credentials(
    request: Request, tenant_id: str, app_id: str, credentials: dict,
) -> bytes:
    """Envelope-encrypt a credentials dict (AAD = app_id).

    REFUSES with 503 when the envelope service is unavailable — we never
    store credentials in plaintext (auth_profiles.py:71-72 rule).
    """
    envelope = getattr(request.app.state, "envelope_service", None)
    if envelope is None:
        raise HTTPException(
            status_code=503,
            detail="encryption unavailable — refusing to store credentials in plaintext",
        )
    plaintext = json.dumps(credentials, sort_keys=True).encode("utf-8")
    try:
        blob = await envelope.encrypt(tenant_id, plaintext, aad=app_id.encode("utf-8"))
    except Exception as exc:
        logger.error(
            "qec.apps.creds_encrypt_failed",
            extra={"app_id": app_id, "error": str(exc)[:200]},
        )
        raise HTTPException(status_code=503, detail="credential encryption failed")
    return blob.to_bytes()


def _public_view(row: ClientAppRow) -> dict:
    """Serialise a row WITHOUT the ciphertext; expose has_credentials only."""
    d = row_to_dict(row)
    d.pop("creds_blob", None)
    d["has_credentials"] = bool(row.creds_blob)
    return d


async def _require_app(session, tenant_id: str, app_id: str) -> ClientAppRow:
    """Fetch one tenant-owned app row or 404 (mirrors ``_require_artifact``)."""
    row = (
        await session.execute(
            select(ClientAppRow).where(
                ClientAppRow.app_id == app_id,
                ClientAppRow.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="app not found")
    return row


@router.post("/apps", status_code=201)
async def create_app(
    payload: AppCreate, request: Request, user: dict = Depends(_MUTATE),
) -> dict:
    """Register a client app; encrypt creds; store answer_key (design §3.1)."""
    tenant_id = user["tenant_id"]
    base_url = _validated_base_url(payload.base_url)
    app_id = new_id()

    creds_blob: bytes | None = None
    if payload.credentials:
        creds_blob = await _encrypt_credentials(
            request, tenant_id, app_id, payload.credentials,
        )

    row = ClientAppRow(
        app_id=app_id,
        tenant_id=tenant_id,
        name=payload.name.strip()[:200],
        base_url=base_url,
        canonical_host=_derive_canonical_host(base_url, payload.canonical_host),
        creds_blob=creds_blob,
        answer_key=payload.answer_key or {},
        env_attestation=payload.env_attestation or {},
        fences=payload.fences or {},
        repo_binding=payload.repo_binding or {},
        schedule=payload.schedule or {},
        budgets=payload.budgets or {},
        status="active",
    )
    async with tenant_scoped_qec_session(tenant_id) as session:
        session.add(row)
        await session.flush()
        result = _public_view(row)

    logger.info(
        "qec.apps.created",
        extra={
            "tenant_id": tenant_id,
            "app_id": app_id,
            "canonical_host": row.canonical_host,
            "has_credentials": bool(creds_blob),
            "actor": user.get("sub", ""),
        },
    )
    return result


@router.get("/apps")
async def list_apps(user: dict = Depends(require_auth)) -> dict:
    """List the tenant's registered apps (creds never included)."""
    tenant_id = user["tenant_id"]
    async with tenant_scoped_qec_session(tenant_id) as session:
        rows = (
            await session.execute(
                select(ClientAppRow)
                .where(ClientAppRow.tenant_id == tenant_id)
                .order_by(ClientAppRow.created_at.desc())
            )
        ).scalars().all()
        return {"apps": [_public_view(r) for r in rows], "total": len(rows)}


@router.get("/apps/{app_id}")
async def get_app(app_id: str, user: dict = Depends(require_auth)) -> dict:
    """Fetch one app (404 when absent or foreign-tenant — RLS + WHERE)."""
    tenant_id = user["tenant_id"]
    async with tenant_scoped_qec_session(tenant_id) as session:
        row = await _require_app(session, tenant_id, app_id)
        return _public_view(row)


@router.patch("/apps/{app_id}")
async def update_app(
    app_id: str,
    payload: AppUpdate,
    request: Request,
    user: dict = Depends(_MUTATE),
) -> dict:
    """Partial update (pause/resume, fences/budgets, credential rotation)."""
    tenant_id = user["tenant_id"]

    if payload.status is not None and payload.status not in _SETTABLE_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"status must be one of: {'|'.join(sorted(_SETTABLE_STATUSES))}",
        )

    async with tenant_scoped_qec_session(tenant_id) as session:
        row = await _require_app(session, tenant_id, app_id)
        if row.status == "deleted":
            raise HTTPException(status_code=409, detail="app is deleted")

        if payload.base_url is not None:
            row.base_url = _validated_base_url(payload.base_url)
            row.canonical_host = _derive_canonical_host(
                row.base_url, payload.canonical_host or row.canonical_host,
            )
        elif payload.canonical_host is not None:
            row.canonical_host = _derive_canonical_host(row.base_url, payload.canonical_host)

        if payload.name is not None:
            row.name = payload.name.strip()[:200]
        for field in (
            "answer_key", "env_attestation", "fences",
            "repo_binding", "schedule", "budgets",
        ):
            value = getattr(payload, field)
            if value is not None:
                setattr(row, field, value)
        if payload.status is not None:
            row.status = payload.status
        if payload.credentials is not None:
            row.creds_blob = await _encrypt_credentials(
                request, tenant_id, app_id, payload.credentials,
            )
        row.updated_at = utc_now()
        await session.flush()
        result = _public_view(row)

    logger.info(
        "qec.apps.updated",
        extra={"tenant_id": tenant_id, "app_id": app_id, "actor": user.get("sub", "")},
    )
    return result


@router.delete("/apps/{app_id}")
async def delete_app(app_id: str, user: dict = Depends(require_role("admin"))) -> dict:
    """Soft-delete: zero the credential ciphertext + mark status='deleted'.

    Mirrors the repo_connections revoke discipline (ciphertext zeroed, row
    retained for audit).  Admin-only — destructive-adjacent.
    """
    tenant_id = user["tenant_id"]
    async with tenant_scoped_qec_session(tenant_id) as session:
        row = await _require_app(session, tenant_id, app_id)
        row.creds_blob = None
        row.status = "deleted"
        row.updated_at = utc_now()

    logger.info(
        "qec.apps.deleted",
        extra={"tenant_id": tenant_id, "app_id": app_id, "actor": user.get("sub", "")},
    )
    return {"app_id": app_id, "status": "deleted", "credentials_zeroed": True}
