"""Platform API — Integration installation administration.

Endpoints
---------

* ``GET    /api/v1/integrations/catalog``                 — manifests of
   integrations the platform recognises (filesystem-loaded at startup
   into ``app.state.integration_catalog``).
* ``GET    /api/v1/integrations/installations``           — per-tenant
   installations (does not return ciphertext).
* ``POST   /api/v1/integrations/{integration_id}/installations``
   — create or update an installation. Credentials in the request body
   are immediately enveloped (``EnvelopeService``) and only the
   ciphertext is persisted.
* ``GET    /api/v1/integrations/{integration_id}/installations/{installation_id}``
   — fetch one installation (no ciphertext).
* ``DELETE /api/v1/integrations/{integration_id}/installations/{installation_id}``
   — uninstall (zeroes ciphertext column, marks status=revoked).
* ``POST   /api/v1/integrations/{integration_id}/installations/{installation_id}/healthcheck``
   — synchronous probe (delegates to the integration runtime).

The wiring is responsible for placing two objects on
``request.app.state``:

* ``integration_catalog: dict[str, IntegrationManifest]``
* ``envelope_service: EnvelopeService``

Both are required for write paths; reads degrade gracefully.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.dialects.postgresql import ARRAY, BYTEA, JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from nexus_sdk.integrations import IntegrationManifest
from nexus_sdk.security.envelope import (
    EnvelopeBlob,
    EnvelopeError,
    EnvelopeService,
)

from ..auth import get_current_user
from ..database import require_db

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Integrations"], prefix="/api/v1/integrations")


# ── Schema projection (matches migration 019) ──────────────────


_md = sa.MetaData()


integration_installations = sa.Table(
    "integration_installations",
    _md,
    sa.Column("installation_id", sa.String(64), primary_key=True),
    sa.Column("tenant_id", sa.String(64), nullable=False),
    sa.Column("integration_id", sa.String(128), nullable=False),
    sa.Column("integration_version", sa.String(32), nullable=False),
    sa.Column("display_name", sa.String(256)),
    sa.Column("config", JSONB, nullable=False),
    sa.Column("encrypted_credentials", BYTEA),
    sa.Column("dek_id", sa.String(128)),
    sa.Column("kek_id", sa.String(128)),
    sa.Column("scopes_granted", ARRAY(sa.String(64)), nullable=False),
    sa.Column("status", sa.String(32), nullable=False),
    sa.Column("last_health_at", sa.DateTime(timezone=True)),
    sa.Column("last_error", sa.Text),
    sa.Column("installed_by", sa.String(128)),
    sa.Column("installed_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)


_PRIVILEGED_ROLES = frozenset({"admin", "manager"})


def _require_privileged(user: dict) -> None:
    role = user.get("role", "viewer")
    if role not in _PRIVILEGED_ROLES:
        raise HTTPException(
            status_code=403,
            detail="integration mutations require admin or manager role",
        )


async def _set_tenant_context(session: AsyncSession, tenant_id: str) -> None:
    await session.execute(
        sa.text("SELECT set_config('nexus.current_tenant_id', :tid, true)"),
        {"tid": tenant_id},
    )


def _catalog(request: Request) -> dict[str, IntegrationManifest]:
    return getattr(request.app.state, "integration_catalog", {}) or {}


def _envelope(request: Request) -> EnvelopeService:
    svc = getattr(request.app.state, "envelope_service", None)
    if svc is None:
        raise HTTPException(
            status_code=503, detail="envelope_service_unavailable"
        )
    return svc


# ── DTOs ────────────────────────────────────────────────────────


class CatalogEntry(BaseModel):
    integration_id: str
    version: str
    display_name: str
    vendor: str
    tier: str
    capabilities: list[str]
    auth_method: str
    documentation_url: Optional[str] = None
    tags: list[str] = []


class InstallationOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    installation_id: str
    tenant_id: str
    integration_id: str
    integration_version: str
    display_name: Optional[str] = None
    config: dict
    scopes_granted: list[str]
    status: str
    has_credentials: bool
    kek_id: Optional[str] = None
    last_health_at: Optional[str] = None
    last_error: Optional[str] = None
    installed_by: Optional[str] = None
    installed_at: Optional[str] = None
    updated_at: Optional[str] = None


class InstallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: Optional[str] = Field(default=None, max_length=256)
    config: dict = Field(default_factory=dict)
    credentials: Optional[dict] = Field(
        default=None,
        description=(
            "Credentials to envelope-encrypt and persist. The body is "
            "JSON-serialised and encrypted with the tenant KEK before "
            "leaving this request handler; no plaintext is ever stored."
        ),
    )
    scopes_granted: list[str] = Field(default_factory=list, max_length=64)

    @field_validator("scopes_granted")
    @classmethod
    def _unique_scopes(cls, v: list[str]) -> list[str]:
        if len(set(v)) != len(v):
            raise ValueError("scopes_granted must be unique")
        for s in v:
            if not s or len(s) > 64:
                raise ValueError("each scope must be 1..64 chars")
        return v


# ── Helpers ────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _row_to_out(row) -> InstallationOut:
    return InstallationOut(
        installation_id=row["installation_id"],
        tenant_id=row["tenant_id"],
        integration_id=row["integration_id"],
        integration_version=row["integration_version"],
        display_name=row["display_name"],
        config=row["config"] or {},
        scopes_granted=list(row["scopes_granted"] or []),
        status=row["status"],
        has_credentials=row["encrypted_credentials"] is not None,
        kek_id=row["kek_id"],
        last_health_at=(
            row["last_health_at"].isoformat() if row["last_health_at"] else None
        ),
        last_error=row["last_error"],
        installed_by=row["installed_by"],
        installed_at=(
            row["installed_at"].isoformat() if row["installed_at"] else None
        ),
        updated_at=(
            row["updated_at"].isoformat() if row["updated_at"] else None
        ),
    )


# ── Catalog ────────────────────────────────────────────────────


@router.get("/catalog", response_model=list[CatalogEntry])
async def get_catalog(
    request: Request,
    user: dict = Depends(get_current_user),
) -> list[CatalogEntry]:
    """Return all integrations registered at platform startup."""
    catalog = _catalog(request)
    entries: list[CatalogEntry] = []
    for m in catalog.values():
        entries.append(
            CatalogEntry(
                integration_id=m.id,
                version=m.version,
                display_name=m.display_name,
                vendor=m.vendor,
                tier=m.tier,
                capabilities=[c.value for c in m.capabilities],
                auth_method=m.auth.method.value,
                documentation_url=m.documentation_url,
                tags=list(m.tags),
            )
        )
    entries.sort(key=lambda e: e.integration_id)
    return entries


# ── Installations: read ────────────────────────────────────────


@router.get("/installations", response_model=list[InstallationOut])
async def list_installations(
    request: Request,
    user: dict = Depends(get_current_user),
) -> list[InstallationOut]:
    factory = require_db()
    tenant_id = user["tenant_id"]
    async with factory() as session:
        await _set_tenant_context(session, tenant_id)
        rows = (
            await session.execute(
                sa.select(integration_installations)
                .where(integration_installations.c.tenant_id == tenant_id)
                .order_by(integration_installations.c.integration_id)
            )
        ).mappings().all()
    return [_row_to_out(r) for r in rows]


@router.get(
    "/{integration_id}/installations/{installation_id}",
    response_model=InstallationOut,
)
async def get_installation(
    integration_id: str,
    installation_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
) -> InstallationOut:
    factory = require_db()
    tenant_id = user["tenant_id"]
    async with factory() as session:
        await _set_tenant_context(session, tenant_id)
        row = (
            await session.execute(
                sa.select(integration_installations).where(
                    integration_installations.c.tenant_id == tenant_id,
                    integration_installations.c.integration_id == integration_id,
                    integration_installations.c.installation_id
                    == installation_id,
                )
            )
        ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="installation_not_found")
    return _row_to_out(row)


# ── Installations: write ───────────────────────────────────────


@router.post(
    "/{integration_id}/installations",
    response_model=InstallationOut,
    status_code=201,
)
async def create_or_update_installation(
    integration_id: str,
    body: InstallRequest,
    request: Request,
    user: dict = Depends(get_current_user),
) -> InstallationOut:
    """Create or update the tenant's installation of an integration.

    A tenant may have at most one installation per integration_id
    (uniqueness enforced at the database level). Re-posting overwrites
    the existing row; credentials are re-enveloped on every write.
    """
    _require_privileged(user)
    tenant_id = user["tenant_id"]

    catalog = _catalog(request)
    manifest = catalog.get(integration_id)
    if manifest is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "unknown_integration",
                "integration_id": integration_id,
            },
        )

    # Validate that any requested scopes are a subset of the manifest's
    # declared scopes (defense in depth — runtime should enforce too).
    declared_scopes = set(manifest.auth.scopes)
    if declared_scopes:
        unknown = [s for s in body.scopes_granted if s not in declared_scopes]
        if unknown:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "scope_not_declared",
                    "scopes": unknown,
                },
            )

    # Envelope credentials before they touch the DB.
    encrypted_credentials: Optional[bytes] = None
    dek_id: Optional[str] = None
    kek_id: Optional[str] = None
    if body.credentials is not None:
        envelope = _envelope(request)
        plaintext = json.dumps(
            body.credentials, separators=(",", ":")
        ).encode("utf-8")
        try:
            blob = await envelope.encrypt(
                tenant_id, plaintext, aad=integration_id.encode("utf-8")
            )
        except EnvelopeError as exc:
            logger.error("integration.envelope_failed: %s", exc)
            raise HTTPException(
                status_code=500, detail="credential_encryption_failed"
            )
        encrypted_credentials = blob.to_bytes()
        dek_id = uuid.uuid4().hex
        kek_id = blob.kek_id

    factory = require_db()
    now = _now()
    async with factory() as session:
        await _set_tenant_context(session, tenant_id)
        existing = (
            await session.execute(
                sa.select(integration_installations).where(
                    integration_installations.c.tenant_id == tenant_id,
                    integration_installations.c.integration_id == integration_id,
                )
            )
        ).mappings().first()

        if existing is None:
            installation_id = uuid.uuid4().hex
            await session.execute(
                sa.insert(integration_installations).values(
                    installation_id=installation_id,
                    tenant_id=tenant_id,
                    integration_id=integration_id,
                    integration_version=manifest.version,
                    display_name=body.display_name or manifest.display_name,
                    config=body.config,
                    encrypted_credentials=encrypted_credentials,
                    dek_id=dek_id,
                    kek_id=kek_id,
                    scopes_granted=body.scopes_granted,
                    status=(
                        "connected"
                        if encrypted_credentials is not None
                        else "pending_auth"
                    ),
                    installed_by=user["user_id"],
                    installed_at=now,
                    updated_at=now,
                )
            )
        else:
            installation_id = existing["installation_id"]
            # Preserve existing ciphertext when no new credentials supplied.
            new_encrypted = (
                encrypted_credentials
                if body.credentials is not None
                else existing["encrypted_credentials"]
            )
            new_dek = (
                dek_id if body.credentials is not None else existing["dek_id"]
            )
            new_kek = (
                kek_id if body.credentials is not None else existing["kek_id"]
            )
            await session.execute(
                sa.update(integration_installations)
                .where(
                    integration_installations.c.installation_id
                    == installation_id,
                )
                .values(
                    integration_version=manifest.version,
                    display_name=body.display_name or existing["display_name"],
                    config=body.config,
                    encrypted_credentials=new_encrypted,
                    dek_id=new_dek,
                    kek_id=new_kek,
                    scopes_granted=body.scopes_granted,
                    status=(
                        "connected"
                        if new_encrypted is not None
                        else "pending_auth"
                    ),
                    last_error=None,
                    updated_at=now,
                )
            )

        await session.commit()
        row = (
            await session.execute(
                sa.select(integration_installations).where(
                    integration_installations.c.installation_id
                    == installation_id,
                )
            )
        ).mappings().first()
    if row is None:
        raise HTTPException(status_code=500, detail="installation_not_persisted")
    return _row_to_out(row)


@router.delete(
    "/{integration_id}/installations/{installation_id}",
    status_code=204,
    response_model=None,   # 204 has no body; be explicit so FastAPI never resolves
                           # the `-> None` return annotation (stringified under
                           # `from __future__ import annotations`) as a response_model.
)
async def uninstall(
    integration_id: str,
    installation_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
) -> None:
    """Revoke an installation.

    Marks status=revoked and zeroes the credential ciphertext column.
    The row remains for audit purposes; downstream services treat
    status=revoked as 'disabled'.
    """
    _require_privileged(user)
    tenant_id = user["tenant_id"]
    factory = require_db()
    async with factory() as session:
        await _set_tenant_context(session, tenant_id)
        result = await session.execute(
            sa.update(integration_installations)
            .where(
                integration_installations.c.tenant_id == tenant_id,
                integration_installations.c.integration_id == integration_id,
                integration_installations.c.installation_id == installation_id,
            )
            .values(
                status="revoked",
                encrypted_credentials=None,
                dek_id=None,
                kek_id=None,
                updated_at=_now(),
            )
        )
        if result.rowcount == 0:
            raise HTTPException(
                status_code=404, detail="installation_not_found"
            )
        await session.commit()


@router.post(
    "/{integration_id}/installations/{installation_id}/healthcheck",
)
async def healthcheck(
    integration_id: str,
    installation_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    """Run a synchronous health probe.

    The platform API does not own the integration runtime, so this
    endpoint delegates to the integration's declared health endpoint
    after retrieving the row (and verifying it exists for the tenant).
    Network probe is performed via httpx with a short timeout to keep
    the admin UX snappy.
    """
    _require_privileged(user)
    tenant_id = user["tenant_id"]
    catalog = _catalog(request)
    manifest = catalog.get(integration_id)
    if manifest is None:
        raise HTTPException(
            status_code=404, detail="unknown_integration"
        )

    factory = require_db()
    async with factory() as session:
        await _set_tenant_context(session, tenant_id)
        row = (
            await session.execute(
                sa.select(integration_installations).where(
                    integration_installations.c.tenant_id == tenant_id,
                    integration_installations.c.installation_id
                    == installation_id,
                )
            )
        ).mappings().first()

    if row is None:
        raise HTTPException(status_code=404, detail="installation_not_found")

    # Probe via httpx. We only return summary status; the actual
    # health-detail JSON is integration-specific.
    import httpx

    timeout = httpx.Timeout(manifest.health.timeout_seconds)
    endpoint = manifest.health.endpoint
    started = datetime.now(timezone.utc)
    try:
        async with httpx.AsyncClient(timeout=timeout) as http:
            resp = await http.get(endpoint)
        healthy = 200 <= resp.status_code < 300
        latency_ms = int(
            (datetime.now(timezone.utc) - started).total_seconds() * 1000
        )
        status_str = "healthy" if healthy else "degraded"
        error: Optional[str] = (
            None if healthy else f"HTTP {resp.status_code}"
        )
    except Exception as exc:
        healthy = False
        latency_ms = int(
            (datetime.now(timezone.utc) - started).total_seconds() * 1000
        )
        status_str = "unreachable"
        error = f"{type(exc).__name__}: {exc}"

    # Update health metadata.
    async with factory() as session:
        await _set_tenant_context(session, tenant_id)
        await session.execute(
            sa.update(integration_installations)
            .where(
                integration_installations.c.installation_id == installation_id,
            )
            .values(
                last_health_at=_now(),
                last_error=error,
                status=(
                    "connected"
                    if healthy
                    else ("degraded" if status_str == "degraded" else "error")
                ),
                updated_at=_now(),
            )
        )
        await session.commit()

    return {
        "installation_id": installation_id,
        "status": status_str,
        "healthy": healthy,
        "latency_ms": latency_ms,
        "checked_at": _now().isoformat(),
        "error": error,
    }
