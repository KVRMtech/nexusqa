"""QE-Central — client app registry (``/api/v1/qec/apps``, R-8 merged table).

POST registers a client application; credentials are envelope-encrypted
(KMS, AAD = ``app_id``) via the SAME refuse-plaintext discipline as
``auth_profiles.save_profile`` (503 — never a silent plaintext fallback).
Credentials are NEVER echoed back; responses expose ``has_credentials``
only.  Reads are open to any authenticated tenant member; mutations
require admin|manager (platform-api RBAC parity).
"""
from __future__ import annotations

import base64
import json
import logging
from datetime import datetime, timezone
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..auth import require_auth, require_role
from ..clients import repo_intel
from ..controlplane.scheduling.admission import ADMISSION
from ..db import new_id, row_to_dict, tenant_scoped_qec_session, utc_now
from ..db.controlplane_models import AppFingerprintRow
from ..db.models import ClientAppRow
from ..fleet import quota

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/qec", tags=["QEC Apps"])

_MUTATE = require_role("admin", "manager")

# Statuses an operator may set via PATCH; 'deleted' only via DELETE.
_SETTABLE_STATUSES = frozenset({"active", "paused"})

# Attestable environment kinds; only 'disposable' may host the mutating submit tier.
_ENV_KINDS = frozenset({"prod", "staging", "disposable"})
_SUBMIT_ENV_KIND = "disposable"


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


async def _seal_webhook_secret(
    request: Request, tenant_id: str, app_id: str, repo_binding: dict | None,
) -> dict:
    """Return ``repo_binding`` with any plaintext ``webhook_secret`` envelope-
    encrypted in place (AAD = app_id) as base64 ``webhook_secret_enc``.

    The webhook shared secret was stored in plaintext JSONB; both GitLab
    (X-Gitlab-Token) and GitHub (HMAC) need the cleartext at verify time, so we
    encrypt at rest and decrypt in the handler.  REFUSES (503) when a secret is
    present but encryption is unavailable — never store it in plaintext.
    """
    rb = dict(repo_binding or {})
    secret = str(rb.pop("webhook_secret", "") or "").strip()
    if not secret:
        rb.pop("webhook_secret_enc", None)
        return rb
    envelope = getattr(request.app.state, "envelope_service", None)
    if envelope is None:
        raise HTTPException(
            status_code=503,
            detail="encryption unavailable — refusing to store a webhook secret in plaintext",
        )
    try:
        blob = await envelope.encrypt(tenant_id, secret.encode("utf-8"), aad=app_id.encode("utf-8"))
    except Exception as exc:
        logger.error(
            "qec.apps.webhook_secret_encrypt_failed",
            extra={"app_id": app_id, "error": str(exc)[:200]},
        )
        raise HTTPException(status_code=503, detail="webhook secret encryption failed")
    rb["webhook_secret_enc"] = base64.b64encode(blob.to_bytes()).decode("ascii")
    return rb


_PROVIDER_BASE = {"gitlab": "https://gitlab.com", "github": "https://github.com"}


async def _prepare_repo_binding(
    request: Request, tenant_id: str, app_id: str, repo_binding: dict | None,
) -> dict:
    """Seal the webhook secret AND provision a repo-intel connection.

    When ``repo_binding`` carries a repo ``token`` (+ project + provider), relay it
    ONCE to repo-intel which KMS-seals it in its own store; persist only the
    returned ``connection_id`` and STRIP the raw token from qe-central.  Fail-open
    on intelligence: if repo-intel is unreachable the app is still created, marked
    ``repo_status='needs_reauth'`` (never a silent failure), and no token is kept.
    """
    rb = await _seal_webhook_secret(request, tenant_id, app_id, repo_binding)
    token = str(rb.pop("token", "") or "").strip()
    project = str(rb.get("project_path") or rb.get("project") or "").strip()
    provider = str(rb.get("provider") or "gitlab").strip().lower()
    base_url = str(rb.get("base_url") or _PROVIDER_BASE.get(provider, "")).strip()
    if token and project and base_url and provider in ("gitlab", "github", "generic_git"):
        try:
            conn = await repo_intel.create_connection(
                tenant_id=tenant_id, provider=provider, base_url=base_url,
                project_path=project, token=token, app_id=app_id,
                default_branch=str(rb.get("default_branch") or "main"),
                label=str(rb.get("label") or "")[:200],
            )
            rb["connection_id"] = conn.connection_id
            rb.pop("repo_status", None)
        except repo_intel.RepoIntelError as exc:
            logger.warning(
                "qec.apps.repo_connection_failed",
                extra={"app_id": app_id, "status": exc.status_code, "detail": exc.detail},
            )
            rb["repo_status"] = "needs_reauth"
    return rb


def _public_view(row: ClientAppRow) -> dict:
    """Serialise a row WITHOUT any secret material; expose has_* flags only."""
    d = row_to_dict(row)
    d.pop("creds_blob", None)
    d["has_credentials"] = bool(row.creds_blob)
    # Never echo the webhook secret (plaintext or ciphertext) — expose a flag.
    rb = dict(d.get("repo_binding") or {})
    has_webhook_secret = bool(rb.pop("webhook_secret", None)) or bool(rb.pop("webhook_secret_enc", None))
    d["repo_binding"] = rb
    d["has_webhook_secret"] = has_webhook_secret
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

    # Phase-7 fleet quota (fail-closed, OPT-IN): refuse a new app when the tenant's
    # plan caps ``max_apps`` and it is already at the cap.  The default plan leaves
    # ``max_apps`` unlimited, so this resolves the plan and returns immediately
    # (opening no session, running no query) — today's behaviour is unchanged.
    # Checked BEFORE credential encryption so a denied request never spends a KMS
    # envelope call.
    try:
        await quota.enforce_app_registration_quota(tenant_id)
    except quota.QuotaExceeded as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.as_http_detail())

    app_id = new_id()

    creds_blob: bytes | None = None
    if payload.credentials:
        creds_blob = await _encrypt_credentials(
            request, tenant_id, app_id, payload.credentials,
        )
    repo_binding = await _prepare_repo_binding(request, tenant_id, app_id, payload.repo_binding)

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
        repo_binding=repo_binding,
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
            "schedule", "budgets",
        ):
            value = getattr(payload, field)
            if value is not None:
                setattr(row, field, value)
        if payload.repo_binding is not None:
            # Seal the webhook secret + (re)provision the repo-intel connection.
            row.repo_binding = await _prepare_repo_binding(
                request, tenant_id, app_id, payload.repo_binding,
            )
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
        connection_id = str((row.repo_binding or {}).get("connection_id") or "").strip()
        row.creds_blob = None
        row.status = "deleted"
        row.updated_at = utc_now()

    # Revoke the sealed repo-intel connection (wipes its token + workdir). Best
    # effort — a repo-intel outage never blocks the delete.
    if connection_id:
        await repo_intel.revoke_connection(tenant_id=tenant_id, connection_id=connection_id)

    logger.info(
        "qec.apps.deleted",
        extra={"tenant_id": tenant_id, "app_id": app_id, "actor": user.get("sub", "")},
    )
    return {"app_id": app_id, "status": "deleted", "credentials_zeroed": True}
