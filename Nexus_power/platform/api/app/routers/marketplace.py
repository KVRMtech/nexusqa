"""Marketplace + sovereign-tier admin endpoints.

Endpoints
---------

  Marketplace (publicly listed; writes require admin/api role):
    GET    /api/v1/marketplace/listings
    POST   /api/v1/marketplace/listings           — create listing draft
    GET    /api/v1/marketplace/listings/{id}
    POST   /api/v1/marketplace/listings/{id}/versions   — submit version
    POST   /api/v1/marketplace/versions/{id}/review     — operator decision

  Install requests (tenant-scoped):
    GET    /api/v1/marketplace/install-requests
    POST   /api/v1/marketplace/install-requests
    POST   /api/v1/marketplace/install-requests/{id}/decide

  Tenant tier + federation + telemetry:
    GET    /api/v1/sovereign/tier
    PUT    /api/v1/sovereign/tier
    GET    /api/v1/sovereign/relationships
    PUT    /api/v1/sovereign/relationships
    DELETE /api/v1/sovereign/relationships/{related}/{kind}
    GET    /api/v1/sovereign/telemetry
    PUT    /api/v1/sovereign/telemetry

  Compliance evidence:
    POST   /api/v1/compliance/exports
    GET    /api/v1/compliance/exports
    GET    /api/v1/compliance/exports/{id}
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from nexus_sdk.integrations import IntegrationManifest, load_manifest, ManifestError

from ..auth import get_current_user
from ..compliance import EvidencePackager, EvidencePackagerConfig
from ..database import require_db

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Marketplace & Sovereign"])


# ── Schema projections ─────────────────────────────────────────


_md = sa.MetaData()


marketplace_listings = sa.Table(
    "marketplace_listings",
    _md,
    sa.Column("listing_id", sa.String(64), primary_key=True),
    sa.Column("plugin_id", sa.String(128), nullable=False),
    sa.Column("display_name", sa.String(256), nullable=False),
    sa.Column("vendor", sa.String(128), nullable=False),
    sa.Column("tier", sa.String(16), nullable=False),
    sa.Column("description", sa.Text),
    sa.Column("tags", ARRAY(sa.String(64)), nullable=False),
    sa.Column("documentation_url", sa.String(512)),
    sa.Column("repository_url", sa.String(512)),
    sa.Column("support_contact", sa.String(256)),
    sa.Column("review_state", sa.String(16), nullable=False),
    sa.Column("is_core", sa.Boolean, nullable=False),
    sa.Column("metadata_json", JSONB, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)


marketplace_listing_versions = sa.Table(
    "marketplace_listing_versions",
    _md,
    sa.Column("version_id", sa.String(64), primary_key=True),
    sa.Column("listing_id", sa.String(64), nullable=False),
    sa.Column("version", sa.String(32), nullable=False),
    sa.Column("manifest_yaml", sa.Text, nullable=False),
    sa.Column("manifest_json", JSONB, nullable=False),
    sa.Column("manifest_sha256", sa.String(64), nullable=False),
    sa.Column("review_state", sa.String(16), nullable=False),
    sa.Column("reviewed_by", sa.String(128)),
    sa.Column("reviewed_at", sa.DateTime(timezone=True)),
    sa.Column("review_notes", sa.Text),
    sa.Column("published_at", sa.DateTime(timezone=True)),
    sa.Column("yanked_at", sa.DateTime(timezone=True)),
    sa.Column("metadata_json", JSONB, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)


marketplace_install_requests = sa.Table(
    "marketplace_install_requests",
    _md,
    sa.Column("request_id", sa.String(64), primary_key=True),
    sa.Column("tenant_id", sa.String(64), nullable=False),
    sa.Column("listing_id", sa.String(64), nullable=False),
    sa.Column("version_id", sa.String(64), nullable=False),
    sa.Column("requested_by", sa.String(128)),
    sa.Column("state", sa.String(16), nullable=False),
    sa.Column("scopes_requested", ARRAY(sa.String(64)), nullable=False),
    sa.Column("metadata_json", JSONB, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("decided_by", sa.String(128)),
    sa.Column("decided_at", sa.DateTime(timezone=True)),
    sa.Column("note", sa.String(512)),
)


tenant_tiers = sa.Table(
    "tenant_tiers",
    _md,
    sa.Column("tenant_id", sa.String(64), primary_key=True),
    sa.Column("tier", sa.String(16), nullable=False),
    sa.Column("compliance_regimes", ARRAY(sa.String(32)), nullable=False),
    sa.Column("data_residency", sa.String(32), nullable=False),
    sa.Column("byok_required", sa.Boolean, nullable=False),
    sa.Column("byok_kek_uri", sa.String(512)),
    sa.Column("audit_retention_days", sa.Integer, nullable=False),
    sa.Column("telemetry_opt_in", sa.Boolean, nullable=False),
    sa.Column("metadata_json", JSONB, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)


tenant_relationships = sa.Table(
    "tenant_relationships",
    _md,
    sa.Column("tenant_id", sa.String(64), primary_key=True),
    sa.Column("related_tenant_id", sa.String(64), primary_key=True),
    sa.Column("relationship_kind", sa.String(32), primary_key=True),
    sa.Column("share_scope", sa.String(32), nullable=False),
    sa.Column("metadata_json", JSONB, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_by", sa.String(128)),
)


compliance_evidence_exports = sa.Table(
    "compliance_evidence_exports",
    _md,
    sa.Column("export_id", sa.String(64), primary_key=True),
    sa.Column("tenant_id", sa.String(64), nullable=False),
    sa.Column("requested_by", sa.String(128), nullable=False),
    sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
    sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
    sa.Column("scopes", ARRAY(sa.String(32)), nullable=False),
    sa.Column("manifest", JSONB, nullable=False),
    sa.Column("manifest_sha256", sa.String(64), nullable=False),
    sa.Column("signature", sa.String(256)),
    sa.Column("status", sa.String(16), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("completed_at", sa.DateTime(timezone=True)),
    sa.Column("storage_uri", sa.String(512)),
)


telemetry_optout = sa.Table(
    "telemetry_optout",
    _md,
    sa.Column("tenant_id", sa.String(64), primary_key=True),
    sa.Column("opted_out", sa.Boolean, nullable=False),
    sa.Column("opted_out_at", sa.DateTime(timezone=True)),
    sa.Column("opted_out_by", sa.String(128)),
    sa.Column("categories", ARRAY(sa.String(32)), nullable=False),
    sa.Column("metadata_json", JSONB, nullable=False),
)


# ── Helpers ────────────────────────────────────────────────────


_PRIVILEGED = frozenset({"admin", "manager"})
_OPERATOR = frozenset({"admin", "api"})


def _require_priv(user: dict) -> None:
    if user.get("role", "viewer") not in _PRIVILEGED:
        raise HTTPException(403, "admin or manager required")


def _require_operator(user: dict) -> None:
    """Marketplace review + tenant-tier writes are operator-only."""
    if user.get("role", "viewer") not in _OPERATOR:
        raise HTTPException(403, "platform operator role required")


async def _set_tenant(session: AsyncSession, tenant_id: str) -> None:
    await session.execute(
        sa.text("SELECT set_config('nexus.current_tenant_id', :tid, true)"),
        {"tid": tenant_id},
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash_manifest(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _validate_manifest_text(text: str) -> IntegrationManifest:
    """Validate a manifest payload against the Phase 0 schema."""
    import tempfile, os
    fd, path = tempfile.mkstemp(suffix=".yaml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        return load_manifest(path)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# ── DTOs ───────────────────────────────────────────────────────


class CreateListingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plugin_id: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=256)
    vendor: str = Field(min_length=1, max_length=128)
    tier: str = "standard"
    description: Optional[str] = Field(default=None, max_length=4096)
    tags: list[str] = Field(default_factory=list, max_length=16)
    documentation_url: Optional[str] = Field(default=None, max_length=512)
    repository_url: Optional[str] = Field(default=None, max_length=512)
    support_contact: Optional[str] = Field(default=None, max_length=256)
    is_core: bool = False

    @field_validator("tier")
    @classmethod
    def _tier(cls, v: str) -> str:
        if v not in ("standard", "enterprise", "sovereign", "community"):
            raise ValueError(f"invalid tier: {v}")
        return v


class ListingOut(BaseModel):
    listing_id: str
    plugin_id: str
    display_name: str
    vendor: str
    tier: str
    description: Optional[str] = None
    tags: list[str]
    documentation_url: Optional[str] = None
    repository_url: Optional[str] = None
    support_contact: Optional[str] = None
    review_state: str
    is_core: bool
    created_at: str
    updated_at: str


class SubmitVersionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str = Field(min_length=1, max_length=32)
    manifest_yaml: str = Field(min_length=10, max_length=64 * 1024)


class ListingVersionOut(BaseModel):
    version_id: str
    listing_id: str
    version: str
    manifest_sha256: str
    review_state: str
    reviewed_by: Optional[str] = None
    reviewed_at: Optional[str] = None
    review_notes: Optional[str] = None
    published_at: Optional[str] = None
    yanked_at: Optional[str] = None
    created_at: str


class ReviewVersionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: str
    notes: Optional[str] = Field(default=None, max_length=4000)

    @field_validator("decision")
    @classmethod
    def _decision(cls, v: str) -> str:
        if v not in ("approve", "reject"):
            raise ValueError(f"invalid decision: {v}")
        return v


class CreateInstallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    listing_id: str = Field(min_length=1, max_length=64)
    version_id: str = Field(min_length=1, max_length=64)
    scopes_requested: list[str] = Field(default_factory=list, max_length=32)


class InstallRequestOut(BaseModel):
    request_id: str
    listing_id: str
    version_id: str
    requested_by: Optional[str] = None
    state: str
    scopes_requested: list[str]
    created_at: str
    decided_by: Optional[str] = None
    decided_at: Optional[str] = None
    note: Optional[str] = None


class DecideInstallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: str
    note: Optional[str] = Field(default=None, max_length=512)

    @field_validator("decision")
    @classmethod
    def _dec(cls, v: str) -> str:
        if v not in ("approve", "reject", "revoke"):
            raise ValueError(f"invalid decision: {v}")
        return v


class TenantTierOut(BaseModel):
    tenant_id: str
    tier: str
    compliance_regimes: list[str]
    data_residency: str
    byok_required: bool
    byok_kek_uri: Optional[str] = None
    audit_retention_days: int
    telemetry_opt_in: bool
    metadata: dict[str, Any]
    created_at: str
    updated_at: str


class UpdateTenantTierRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tier: Optional[str] = None
    compliance_regimes: Optional[list[str]] = None
    data_residency: Optional[str] = None
    byok_required: Optional[bool] = None
    byok_kek_uri: Optional[str] = Field(default=None, max_length=512)
    audit_retention_days: Optional[int] = Field(default=None, ge=30, le=3650)
    telemetry_opt_in: Optional[bool] = None
    metadata: Optional[dict[str, Any]] = None

    @field_validator("tier")
    @classmethod
    def _tier(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        if v not in ("standard", "pro", "sovereign", "community"):
            raise ValueError(f"invalid tier: {v}")
        return v


class RelationshipOut(BaseModel):
    related_tenant_id: str
    relationship_kind: str
    share_scope: str
    metadata: dict[str, Any]
    created_at: str


class UpsertRelationshipRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    related_tenant_id: str = Field(min_length=1, max_length=64)
    relationship_kind: str
    share_scope: str = "none"
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("relationship_kind")
    @classmethod
    def _kind(cls, v: str) -> str:
        if v not in ("parent", "child", "peer"):
            raise ValueError(f"invalid relationship_kind: {v}")
        return v

    @field_validator("share_scope")
    @classmethod
    def _scope(cls, v: str) -> str:
        if v not in ("none", "public", "cards", "atlas", "all"):
            raise ValueError(f"invalid share_scope: {v}")
        return v


class TelemetryOptOutOut(BaseModel):
    opted_out: bool
    opted_out_at: Optional[str] = None
    opted_out_by: Optional[str] = None
    categories: list[str]


class SetTelemetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    opted_out: bool
    categories: list[str] = Field(default_factory=list, max_length=32)


class CreateExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    period_start: datetime
    period_end: datetime
    scopes: list[str] = Field(default_factory=list, max_length=16)

    @field_validator("scopes")
    @classmethod
    def _scopes(cls, v: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for s in v:
            if not isinstance(s, str) or not s:
                continue
            if s in seen:
                continue
            seen.add(s)
            out.append(s)
        return out


class ExportOut(BaseModel):
    export_id: str
    requested_by: str
    period_start: str
    period_end: str
    scopes: list[str]
    manifest_sha256: str
    status: str
    created_at: str
    completed_at: Optional[str] = None
    storage_uri: Optional[str] = None


# ── Mappers ────────────────────────────────────────────────────


def _listing_to_out(row) -> ListingOut:
    return ListingOut(
        listing_id=row["listing_id"],
        plugin_id=row["plugin_id"],
        display_name=row["display_name"],
        vendor=row["vendor"],
        tier=row["tier"],
        description=row["description"],
        tags=list(row["tags"] or []),
        documentation_url=row["documentation_url"],
        repository_url=row["repository_url"],
        support_contact=row["support_contact"],
        review_state=row["review_state"],
        is_core=bool(row["is_core"]),
        created_at=row["created_at"].isoformat(),
        updated_at=row["updated_at"].isoformat(),
    )


def _version_to_out(row) -> ListingVersionOut:
    return ListingVersionOut(
        version_id=row["version_id"],
        listing_id=row["listing_id"],
        version=row["version"],
        manifest_sha256=row["manifest_sha256"],
        review_state=row["review_state"],
        reviewed_by=row.get("reviewed_by"),
        reviewed_at=(
            row["reviewed_at"].isoformat() if row.get("reviewed_at") else None
        ),
        review_notes=row.get("review_notes"),
        published_at=(
            row["published_at"].isoformat() if row.get("published_at") else None
        ),
        yanked_at=(
            row["yanked_at"].isoformat() if row.get("yanked_at") else None
        ),
        created_at=row["created_at"].isoformat(),
    )


def _install_to_out(row) -> InstallRequestOut:
    return InstallRequestOut(
        request_id=row["request_id"],
        listing_id=row["listing_id"],
        version_id=row["version_id"],
        requested_by=row.get("requested_by"),
        state=row["state"],
        scopes_requested=list(row["scopes_requested"] or []),
        created_at=row["created_at"].isoformat(),
        decided_by=row.get("decided_by"),
        decided_at=(
            row["decided_at"].isoformat() if row.get("decided_at") else None
        ),
        note=row.get("note"),
    )


def _tier_to_out(row) -> TenantTierOut:
    return TenantTierOut(
        tenant_id=row["tenant_id"],
        tier=row["tier"],
        compliance_regimes=list(row["compliance_regimes"] or []),
        data_residency=row["data_residency"],
        byok_required=bool(row["byok_required"]),
        byok_kek_uri=row["byok_kek_uri"],
        audit_retention_days=int(row["audit_retention_days"]),
        telemetry_opt_in=bool(row["telemetry_opt_in"]),
        metadata=dict(row["metadata_json"] or {}),
        created_at=row["created_at"].isoformat(),
        updated_at=row["updated_at"].isoformat(),
    )


def _relationship_to_out(row) -> RelationshipOut:
    return RelationshipOut(
        related_tenant_id=row["related_tenant_id"],
        relationship_kind=row["relationship_kind"],
        share_scope=row["share_scope"],
        metadata=dict(row["metadata_json"] or {}),
        created_at=row["created_at"].isoformat(),
    )


def _export_to_out(row) -> ExportOut:
    return ExportOut(
        export_id=row["export_id"],
        requested_by=row["requested_by"],
        period_start=row["period_start"].isoformat(),
        period_end=row["period_end"].isoformat(),
        scopes=list(row["scopes"] or []),
        manifest_sha256=row["manifest_sha256"],
        status=row["status"],
        created_at=row["created_at"].isoformat(),
        completed_at=(
            row["completed_at"].isoformat() if row.get("completed_at") else None
        ),
        storage_uri=row.get("storage_uri"),
    )


# ── Marketplace listings ──────────────────────────────────────


@router.get("/api/v1/marketplace/listings", response_model=list[ListingOut])
async def list_listings(
    review_state: Optional[str] = "approved",
    user: dict = Depends(get_current_user),  # noqa: ARG001
) -> list[ListingOut]:
    factory = require_db()
    async with factory() as session:
        stmt = sa.select(marketplace_listings)
        if review_state:
            stmt = stmt.where(
                marketplace_listings.c.review_state == review_state
            )
        stmt = stmt.order_by(marketplace_listings.c.display_name.asc())
        rows = (await session.execute(stmt)).mappings().all()
    return [_listing_to_out(r) for r in rows]


@router.post(
    "/api/v1/marketplace/listings",
    response_model=ListingOut,
    status_code=201,
)
async def create_listing(
    body: CreateListingRequest,
    user: dict = Depends(get_current_user),
) -> ListingOut:
    _require_operator(user)
    factory = require_db()
    now = _now()
    listing_id = uuid.uuid4().hex
    async with factory() as session:
        try:
            await session.execute(
                sa.insert(marketplace_listings).values(
                    listing_id=listing_id,
                    plugin_id=body.plugin_id,
                    display_name=body.display_name,
                    vendor=body.vendor,
                    tier=body.tier,
                    description=body.description,
                    tags=body.tags,
                    documentation_url=body.documentation_url,
                    repository_url=body.repository_url,
                    support_contact=body.support_contact,
                    review_state="draft",
                    is_core=body.is_core,
                    metadata_json={},
                    created_at=now,
                    updated_at=now,
                )
            )
            await session.commit()
        except sa.exc.IntegrityError as exc:
            await session.rollback()
            raise HTTPException(
                409, {"code": "plugin_id_conflict", "plugin_id": body.plugin_id}
            ) from exc
        row = (
            await session.execute(
                sa.select(marketplace_listings).where(
                    marketplace_listings.c.listing_id == listing_id,
                )
            )
        ).mappings().first()
    if row is None:
        raise HTTPException(500, "listing_not_persisted")
    return _listing_to_out(row)


@router.get("/api/v1/marketplace/listings/{listing_id}", response_model=ListingOut)
async def get_listing(
    listing_id: str,
    user: dict = Depends(get_current_user),  # noqa: ARG001
) -> ListingOut:
    factory = require_db()
    async with factory() as session:
        row = (
            await session.execute(
                sa.select(marketplace_listings).where(
                    marketplace_listings.c.listing_id == listing_id,
                )
            )
        ).mappings().first()
    if row is None:
        raise HTTPException(404, "listing_not_found")
    return _listing_to_out(row)


@router.post(
    "/api/v1/marketplace/listings/{listing_id}/versions",
    response_model=ListingVersionOut,
    status_code=201,
)
async def submit_listing_version(
    listing_id: str,
    body: SubmitVersionRequest,
    user: dict = Depends(get_current_user),
) -> ListingVersionOut:
    _require_operator(user)
    factory = require_db()
    # Validate the manifest against the Phase 0 schema.
    try:
        manifest = _validate_manifest_text(body.manifest_yaml)
    except ManifestError as exc:
        raise HTTPException(
            400, {"code": "invalid_manifest", "detail": str(exc)}
        ) from exc

    version_id = uuid.uuid4().hex
    sha = _hash_manifest(body.manifest_yaml)
    manifest_json = json.loads(manifest.model_dump_json())
    now = _now()
    async with factory() as session:
        existing = (
            await session.execute(
                sa.select(marketplace_listings).where(
                    marketplace_listings.c.listing_id == listing_id,
                )
            )
        ).mappings().first()
        if existing is None:
            raise HTTPException(404, "listing_not_found")
        # Core listings auto-approve; community ones land in 'submitted'.
        initial_state = "approved" if existing["is_core"] else "submitted"
        try:
            await session.execute(
                sa.insert(marketplace_listing_versions).values(
                    version_id=version_id,
                    listing_id=listing_id,
                    version=body.version,
                    manifest_yaml=body.manifest_yaml,
                    manifest_json=manifest_json,
                    manifest_sha256=sha,
                    review_state=initial_state,
                    metadata_json={},
                    created_at=now,
                )
            )
            await session.commit()
        except sa.exc.IntegrityError as exc:
            await session.rollback()
            raise HTTPException(
                409,
                {
                    "code": "version_conflict",
                    "version": body.version,
                },
            ) from exc
        row = (
            await session.execute(
                sa.select(marketplace_listing_versions).where(
                    marketplace_listing_versions.c.version_id == version_id,
                )
            )
        ).mappings().first()
    if row is None:
        raise HTTPException(500, "version_not_persisted")
    return _version_to_out(row)


@router.post(
    "/api/v1/marketplace/versions/{version_id}/review",
    response_model=ListingVersionOut,
)
async def review_listing_version(
    version_id: str,
    body: ReviewVersionRequest,
    user: dict = Depends(get_current_user),
) -> ListingVersionOut:
    _require_operator(user)
    factory = require_db()
    now = _now()
    new_state = "approved" if body.decision == "approve" else "rejected"
    async with factory() as session:
        existing = (
            await session.execute(
                sa.select(marketplace_listing_versions).where(
                    marketplace_listing_versions.c.version_id == version_id,
                )
            )
        ).mappings().first()
        if existing is None:
            raise HTTPException(404, "version_not_found")
        if existing["review_state"] in ("rejected", "yanked"):
            raise HTTPException(
                409,
                {
                    "code": "version_already_terminal",
                    "state": existing["review_state"],
                },
            )
        values = {
            "review_state": new_state,
            "reviewed_by": user.get("user_id"),
            "reviewed_at": now,
            "review_notes": body.notes,
        }
        if new_state == "approved":
            values["published_at"] = now
        await session.execute(
            sa.update(marketplace_listing_versions)
            .where(marketplace_listing_versions.c.version_id == version_id)
            .values(**values)
        )
        # Auto-promote listing to approved on first approved version.
        if new_state == "approved":
            await session.execute(
                sa.update(marketplace_listings)
                .where(
                    marketplace_listings.c.listing_id
                    == existing["listing_id"]
                )
                .values(review_state="approved", updated_at=now)
            )
        await session.commit()
        row = (
            await session.execute(
                sa.select(marketplace_listing_versions).where(
                    marketplace_listing_versions.c.version_id == version_id,
                )
            )
        ).mappings().first()
    return _version_to_out(row)


# ── Install requests ──────────────────────────────────────────


@router.get(
    "/api/v1/marketplace/install-requests",
    response_model=list[InstallRequestOut],
)
async def list_install_requests(
    state: Optional[str] = None,
    user: dict = Depends(get_current_user),
) -> list[InstallRequestOut]:
    factory = require_db()
    tenant_id = user["tenant_id"]
    async with factory() as session:
        await _set_tenant(session, tenant_id)
        stmt = sa.select(marketplace_install_requests).where(
            marketplace_install_requests.c.tenant_id == tenant_id
        )
        if state:
            stmt = stmt.where(marketplace_install_requests.c.state == state)
        stmt = stmt.order_by(
            marketplace_install_requests.c.created_at.desc()
        )
        rows = (await session.execute(stmt)).mappings().all()
    return [_install_to_out(r) for r in rows]


@router.post(
    "/api/v1/marketplace/install-requests",
    response_model=InstallRequestOut,
    status_code=201,
)
async def create_install_request(
    body: CreateInstallRequest,
    user: dict = Depends(get_current_user),
) -> InstallRequestOut:
    _require_priv(user)
    factory = require_db()
    tenant_id = user["tenant_id"]
    now = _now()
    request_id = uuid.uuid4().hex
    async with factory() as session:
        # Verify the listing + version are approved (otherwise install
        # is impossible).
        version_row = (
            await session.execute(
                sa.select(marketplace_listing_versions).where(
                    marketplace_listing_versions.c.version_id == body.version_id,
                    marketplace_listing_versions.c.listing_id == body.listing_id,
                )
            )
        ).mappings().first()
        if version_row is None:
            raise HTTPException(404, "version_not_found")
        if version_row["review_state"] not in ("approved", "published"):
            raise HTTPException(
                422,
                {
                    "code": "version_not_approved",
                    "state": version_row["review_state"],
                },
            )
        listing_row = (
            await session.execute(
                sa.select(marketplace_listings).where(
                    marketplace_listings.c.listing_id == body.listing_id,
                )
            )
        ).mappings().first()
        if listing_row is None:
            raise HTTPException(404, "listing_not_found")
        # Core / first-party listings auto-approve install requests;
        # third-party listings need an operator decision.
        initial_state = "auto_approved" if listing_row["is_core"] else "pending"

        await _set_tenant(session, tenant_id)
        await session.execute(
            sa.insert(marketplace_install_requests).values(
                request_id=request_id,
                tenant_id=tenant_id,
                listing_id=body.listing_id,
                version_id=body.version_id,
                requested_by=user.get("user_id"),
                state=initial_state,
                scopes_requested=body.scopes_requested,
                metadata_json={},
                created_at=now,
            )
        )
        await session.commit()
        row = (
            await session.execute(
                sa.select(marketplace_install_requests).where(
                    marketplace_install_requests.c.request_id == request_id,
                )
            )
        ).mappings().first()
    return _install_to_out(row)


@router.post(
    "/api/v1/marketplace/install-requests/{request_id}/decide",
    response_model=InstallRequestOut,
)
async def decide_install_request(
    request_id: str,
    body: DecideInstallRequest,
    user: dict = Depends(get_current_user),
) -> InstallRequestOut:
    _require_operator(user)
    factory = require_db()
    new_state = {
        "approve": "approved",
        "reject": "rejected",
        "revoke": "revoked",
    }[body.decision]
    now = _now()
    async with factory() as session:
        # Operator views don't set a tenant context; RLS would block the
        # update. We bypass by setting the row's own tenant_id as context.
        target = (
            await session.execute(
                sa.text(
                    "SELECT tenant_id FROM marketplace_install_requests "
                    "WHERE request_id = :rid"
                ),
                {"rid": request_id},
            )
        ).first()
        if target is None:
            raise HTTPException(404, "request_not_found")
        await _set_tenant(session, target[0])
        await session.execute(
            sa.update(marketplace_install_requests)
            .where(
                marketplace_install_requests.c.request_id == request_id,
            )
            .values(
                state=new_state,
                decided_by=user.get("user_id"),
                decided_at=now,
                note=body.note,
            )
        )
        await session.commit()
        row = (
            await session.execute(
                sa.select(marketplace_install_requests).where(
                    marketplace_install_requests.c.request_id == request_id,
                )
            )
        ).mappings().first()
    return _install_to_out(row)


# ── Tenant tier ───────────────────────────────────────────────


@router.get("/api/v1/sovereign/tier", response_model=TenantTierOut)
async def get_tier(
    user: dict = Depends(get_current_user),
) -> TenantTierOut:
    factory = require_db()
    tenant_id = user["tenant_id"]
    async with factory() as session:
        await _set_tenant(session, tenant_id)
        row = (
            await session.execute(
                sa.select(tenant_tiers).where(
                    tenant_tiers.c.tenant_id == tenant_id,
                )
            )
        ).mappings().first()
        if row is None:
            # Backfill a default row so the UI never has to handle a missing tier.
            now = _now()
            await session.execute(
                sa.insert(tenant_tiers).values(
                    tenant_id=tenant_id,
                    tier="standard",
                    compliance_regimes=[],
                    data_residency="us",
                    byok_required=False,
                    audit_retention_days=365,
                    telemetry_opt_in=True,
                    metadata_json={},
                    created_at=now,
                    updated_at=now,
                )
            )
            await session.commit()
            row = (
                await session.execute(
                    sa.select(tenant_tiers).where(
                        tenant_tiers.c.tenant_id == tenant_id,
                    )
                )
            ).mappings().first()
    if row is None:
        raise HTTPException(500, "tier_unavailable")
    return _tier_to_out(row)


@router.put("/api/v1/sovereign/tier", response_model=TenantTierOut)
async def update_tier(
    body: UpdateTenantTierRequest,
    user: dict = Depends(get_current_user),
) -> TenantTierOut:
    _require_operator(user)
    factory = require_db()
    tenant_id = user["tenant_id"]
    now = _now()
    update_values: dict[str, Any] = {"updated_at": now}
    if body.tier is not None:
        update_values["tier"] = body.tier
    if body.compliance_regimes is not None:
        update_values["compliance_regimes"] = body.compliance_regimes
    if body.data_residency is not None:
        update_values["data_residency"] = body.data_residency[:32]
    if body.byok_required is not None:
        update_values["byok_required"] = body.byok_required
    if body.byok_kek_uri is not None:
        update_values["byok_kek_uri"] = body.byok_kek_uri
    if body.audit_retention_days is not None:
        update_values["audit_retention_days"] = body.audit_retention_days
    if body.telemetry_opt_in is not None:
        update_values["telemetry_opt_in"] = body.telemetry_opt_in
    if body.metadata is not None:
        update_values["metadata_json"] = body.metadata

    async with factory() as session:
        await _set_tenant(session, tenant_id)
        # UPSERT pattern: insert if absent, otherwise update.
        existing = (
            await session.execute(
                sa.select(tenant_tiers).where(
                    tenant_tiers.c.tenant_id == tenant_id,
                )
            )
        ).mappings().first()
        if existing is None:
            base = {
                "tenant_id": tenant_id,
                "tier": "standard",
                "compliance_regimes": [],
                "data_residency": "us",
                "byok_required": False,
                "audit_retention_days": 365,
                "telemetry_opt_in": True,
                "metadata_json": {},
                "created_at": now,
            }
            base.update(update_values)
            await session.execute(sa.insert(tenant_tiers).values(**base))
        else:
            await session.execute(
                sa.update(tenant_tiers)
                .where(tenant_tiers.c.tenant_id == tenant_id)
                .values(**update_values)
            )
        await session.commit()
        row = (
            await session.execute(
                sa.select(tenant_tiers).where(
                    tenant_tiers.c.tenant_id == tenant_id,
                )
            )
        ).mappings().first()
    if row is None:
        raise HTTPException(500, "tier_lost")
    return _tier_to_out(row)


# ── Federation / relationships ────────────────────────────────


@router.get(
    "/api/v1/sovereign/relationships", response_model=list[RelationshipOut]
)
async def list_relationships(
    user: dict = Depends(get_current_user),
) -> list[RelationshipOut]:
    factory = require_db()
    tenant_id = user["tenant_id"]
    async with factory() as session:
        await _set_tenant(session, tenant_id)
        rows = (
            await session.execute(
                sa.select(tenant_relationships)
                .where(tenant_relationships.c.tenant_id == tenant_id)
                .order_by(
                    tenant_relationships.c.relationship_kind.asc(),
                    tenant_relationships.c.related_tenant_id.asc(),
                )
            )
        ).mappings().all()
    return [_relationship_to_out(r) for r in rows]


@router.put(
    "/api/v1/sovereign/relationships", response_model=RelationshipOut
)
async def upsert_relationship(
    body: UpsertRelationshipRequest,
    user: dict = Depends(get_current_user),
) -> RelationshipOut:
    _require_operator(user)
    factory = require_db()
    tenant_id = user["tenant_id"]
    if tenant_id == body.related_tenant_id:
        raise HTTPException(400, "cannot relate a tenant to itself")
    now = _now()
    async with factory() as session:
        await _set_tenant(session, tenant_id)
        stmt = pg_insert(tenant_relationships).values(
            tenant_id=tenant_id,
            related_tenant_id=body.related_tenant_id,
            relationship_kind=body.relationship_kind,
            share_scope=body.share_scope,
            metadata_json=body.metadata,
            created_at=now,
            updated_by=user.get("user_id"),
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[
                tenant_relationships.c.tenant_id,
                tenant_relationships.c.related_tenant_id,
                tenant_relationships.c.relationship_kind,
            ],
            set_={
                "share_scope": stmt.excluded.share_scope,
                "metadata_json": stmt.excluded.metadata_json,
                "updated_by": stmt.excluded.updated_by,
            },
        )
        await session.execute(stmt)
        await session.commit()
        row = (
            await session.execute(
                sa.select(tenant_relationships).where(
                    tenant_relationships.c.tenant_id == tenant_id,
                    tenant_relationships.c.related_tenant_id == body.related_tenant_id,
                    tenant_relationships.c.relationship_kind == body.relationship_kind,
                )
            )
        ).mappings().first()
    if row is None:
        raise HTTPException(500, "relationship_lost")
    return _relationship_to_out(row)


@router.delete(
    "/api/v1/sovereign/relationships/{related_tenant_id}/{relationship_kind}",
    status_code=204,
)
async def delete_relationship(
    related_tenant_id: str,
    relationship_kind: str,
    user: dict = Depends(get_current_user),
) -> None:
    _require_operator(user)
    factory = require_db()
    tenant_id = user["tenant_id"]
    async with factory() as session:
        await _set_tenant(session, tenant_id)
        result = await session.execute(
            sa.delete(tenant_relationships).where(
                tenant_relationships.c.tenant_id == tenant_id,
                tenant_relationships.c.related_tenant_id == related_tenant_id,
                tenant_relationships.c.relationship_kind == relationship_kind,
            )
        )
        if result.rowcount == 0:
            raise HTTPException(404, "relationship_not_found")
        await session.commit()


# ── Telemetry ─────────────────────────────────────────────────


@router.get(
    "/api/v1/sovereign/telemetry", response_model=TelemetryOptOutOut
)
async def get_telemetry(
    user: dict = Depends(get_current_user),
) -> TelemetryOptOutOut:
    factory = require_db()
    tenant_id = user["tenant_id"]
    async with factory() as session:
        await _set_tenant(session, tenant_id)
        row = (
            await session.execute(
                sa.select(telemetry_optout).where(
                    telemetry_optout.c.tenant_id == tenant_id,
                )
            )
        ).mappings().first()
    if row is None:
        return TelemetryOptOutOut(
            opted_out=False, categories=[],
        )
    return TelemetryOptOutOut(
        opted_out=bool(row["opted_out"]),
        opted_out_at=(
            row["opted_out_at"].isoformat() if row["opted_out_at"] else None
        ),
        opted_out_by=row["opted_out_by"],
        categories=list(row["categories"] or []),
    )


@router.put(
    "/api/v1/sovereign/telemetry", response_model=TelemetryOptOutOut
)
async def set_telemetry(
    body: SetTelemetryRequest,
    user: dict = Depends(get_current_user),
) -> TelemetryOptOutOut:
    _require_operator(user)
    factory = require_db()
    tenant_id = user["tenant_id"]
    now = _now()
    async with factory() as session:
        await _set_tenant(session, tenant_id)
        stmt = pg_insert(telemetry_optout).values(
            tenant_id=tenant_id,
            opted_out=body.opted_out,
            opted_out_at=now if body.opted_out else None,
            opted_out_by=user.get("user_id") if body.opted_out else None,
            categories=body.categories,
            metadata_json={},
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[telemetry_optout.c.tenant_id],
            set_={
                "opted_out": stmt.excluded.opted_out,
                "opted_out_at": stmt.excluded.opted_out_at,
                "opted_out_by": stmt.excluded.opted_out_by,
                "categories": stmt.excluded.categories,
            },
        )
        await session.execute(stmt)
        await session.commit()
        row = (
            await session.execute(
                sa.select(telemetry_optout).where(
                    telemetry_optout.c.tenant_id == tenant_id,
                )
            )
        ).mappings().first()
    return TelemetryOptOutOut(
        opted_out=bool(row["opted_out"]),
        opted_out_at=(
            row["opted_out_at"].isoformat() if row["opted_out_at"] else None
        ),
        opted_out_by=row["opted_out_by"],
        categories=list(row["categories"] or []),
    )


# ── Compliance evidence exports ───────────────────────────────


def _packager_config_from_env() -> EvidencePackagerConfig:
    import os as _os
    signing_key_env = _os.getenv("NEXUS_EVIDENCE_SIGNING_KEY")
    key_bytes: Optional[bytes] = (
        signing_key_env.encode("utf-8") if signing_key_env else None
    )
    storage_dir = _os.getenv("NEXUS_EVIDENCE_STORAGE_DIR")
    return EvidencePackagerConfig(
        signing_key=key_bytes,
        storage_dir=storage_dir,
    )


@router.post("/api/v1/compliance/exports", response_model=ExportOut, status_code=201)
async def request_compliance_export(
    body: CreateExportRequest,
    user: dict = Depends(get_current_user),
) -> ExportOut:
    _require_operator(user)
    if body.period_end <= body.period_start:
        raise HTTPException(400, "period_end must be after period_start")
    factory = require_db()
    tenant_id = user["tenant_id"]
    now = _now()
    export_id = uuid.uuid4().hex
    scopes_in = body.scopes or [
        "echoes",
        "plugin_events",
        "scim_users",
        "scim_groups",
        "atlas",
        "knowledge_cards",
    ]
    pending_manifest = {
        "schema": "nexus.compliance.evidence.v1",
        "export_id": export_id,
        "tenant_id": tenant_id,
        "period_start": body.period_start.isoformat(),
        "period_end": body.period_end.isoformat(),
        "scopes": scopes_in,
        "requested_by": user.get("user_id"),
        "requested_at": now.isoformat(),
        "status": "pending",
    }
    pending_canonical = json.dumps(
        pending_manifest, sort_keys=True, separators=(",", ":")
    )
    pending_sha = hashlib.sha256(pending_canonical.encode("utf-8")).hexdigest()

    async with factory() as session:
        await _set_tenant(session, tenant_id)
        await session.execute(
            sa.insert(compliance_evidence_exports).values(
                export_id=export_id,
                tenant_id=tenant_id,
                requested_by=user["user_id"],
                period_start=body.period_start,
                period_end=body.period_end,
                scopes=scopes_in,
                manifest=pending_manifest,
                manifest_sha256=pending_sha,
                status="running",
                created_at=now,
            )
        )
        await session.commit()

        # Build the actual bundle. The packager only issues SELECTs;
        # writes against ``compliance_evidence_exports`` happen below.
        try:
            packager = EvidencePackager(_packager_config_from_env())
            bundle = await packager.build(
                session,
                tenant_id=tenant_id,
                period_start=body.period_start,
                period_end=body.period_end,
                scopes=tuple(scopes_in),
                export_id=export_id,
            )
            completed_at = _now()
            await session.execute(
                sa.update(compliance_evidence_exports)
                .where(compliance_evidence_exports.c.export_id == export_id)
                .values(
                    manifest=bundle.manifest,
                    manifest_sha256=bundle.manifest_sha256,
                    signature=bundle.signature,
                    status="succeeded",
                    completed_at=completed_at,
                    storage_uri=bundle.storage_uri,
                )
            )
            await session.commit()
        except Exception as exc:  # pragma: no cover — failure path
            logger.exception("compliance.export_failed", extra={"export_id": export_id})
            failed_manifest = dict(pending_manifest)
            failed_manifest["status"] = "failed"
            failed_manifest["error"] = str(exc)
            await session.execute(
                sa.update(compliance_evidence_exports)
                .where(compliance_evidence_exports.c.export_id == export_id)
                .values(
                    manifest=failed_manifest,
                    status="failed",
                    completed_at=_now(),
                )
            )
            await session.commit()

        row = (
            await session.execute(
                sa.select(compliance_evidence_exports).where(
                    compliance_evidence_exports.c.export_id == export_id,
                )
            )
        ).mappings().first()
    return _export_to_out(row)


@router.get("/api/v1/compliance/exports", response_model=list[ExportOut])
async def list_compliance_exports(
    limit: int = 50,
    user: dict = Depends(get_current_user),
) -> list[ExportOut]:
    factory = require_db()
    tenant_id = user["tenant_id"]
    limit = max(1, min(200, int(limit)))
    async with factory() as session:
        await _set_tenant(session, tenant_id)
        rows = (
            await session.execute(
                sa.select(compliance_evidence_exports)
                .where(compliance_evidence_exports.c.tenant_id == tenant_id)
                .order_by(
                    compliance_evidence_exports.c.created_at.desc()
                )
                .limit(limit)
            )
        ).mappings().all()
    return [_export_to_out(r) for r in rows]


@router.get(
    "/api/v1/compliance/exports/{export_id}", response_model=ExportOut
)
async def get_compliance_export(
    export_id: str,
    user: dict = Depends(get_current_user),
) -> ExportOut:
    factory = require_db()
    tenant_id = user["tenant_id"]
    async with factory() as session:
        await _set_tenant(session, tenant_id)
        row = (
            await session.execute(
                sa.select(compliance_evidence_exports).where(
                    compliance_evidence_exports.c.tenant_id == tenant_id,
                    compliance_evidence_exports.c.export_id == export_id,
                )
            )
        ).mappings().first()
    if row is None:
        raise HTTPException(404, "export_not_found")
    return _export_to_out(row)
