"""
Phase 16 — Tenant CRUD + lifecycle HTTP API.

Endpoints:
  GET    /v1/admin/tenants                     list all (admin only)
  GET    /v1/admin/tenants/{tenant_id}         detail
  POST   /v1/admin/tenants                     create (pending)
  POST   /v1/admin/tenants/{tenant_id}/provision
  POST   /v1/admin/tenants/{tenant_id}/suspend
  POST   /v1/admin/tenants/{tenant_id}/resume
  POST   /v1/admin/tenants/{tenant_id}/offboard
  POST   /v1/admin/tenants/{tenant_id}/finalize-deletion
  GET    /v1/admin/tenants/{tenant_id}/export  GDPR data export (async)

The endpoints are admin-only (`role: admin` JWT claim). Lifecycle
transitions go through the state machine in nexus_sdk.tenant_lifecycle
— every transition emits an audit_log row.

The actual side effects (Milvus partition create/drop, Postgres RLS
context, object storage prefix grants/revokes) are out of scope for
this router; they live in the provisioner module called from the
state-transition handlers.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nexus_sdk.auth import NexusUser, get_current_user
from nexus_sdk.db.models import TenantRow
from nexus_sdk.tenant_lifecycle import (
    TenantLifecycleError,
    TenantRecord,
    TenantState,
    finalize_deletion,
    provision,
    resume,
    start_offboarding,
    suspend,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/admin/tenants", tags=["admin", "tenants"])


# ─── Pydantic wire schemas ─────────────────────────────────────


class TenantResponse(BaseModel):
    tenant_id: str
    display_name: str
    state: str
    tier: str
    provisioned_at: Optional[datetime] = None
    suspended_at: Optional[datetime] = None
    offboarding_started_at: Optional[datetime] = None
    retention_until: Optional[datetime] = None
    deleted_at: Optional[datetime] = None


class TenantListResponse(BaseModel):
    tenants: list[TenantResponse]
    total: int


class CreateTenantRequest(BaseModel):
    tenant_id: Optional[str] = Field(
        default=None,
        description="Optional client-supplied id. Auto-generated if omitted.",
    )
    display_name: str = Field(..., min_length=1, max_length=200)
    tier: str = Field(default="pilot", pattern=r"^(pilot|enterprise|internal)$")


class TransitionRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=500)


class OffboardRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=500)
    retention_days: int = Field(default=30, ge=1, le=365)


# ─── Helpers ───────────────────────────────────────────────────


def _require_admin(user: NexusUser) -> None:
    if (user.role or "").lower() != "admin":
        raise HTTPException(status_code=403, detail="admin role required")


def _row_to_record(row: TenantRow) -> TenantRecord:
    return TenantRecord(
        tenant_id=row.tenant_id,
        display_name=row.display_name or "",
        tier=getattr(row, "tier", "pilot"),
        state=TenantState(getattr(row, "state", TenantState.ACTIVE.value)),
        provisioned_at=getattr(row, "provisioned_at", None),
        suspended_at=getattr(row, "suspended_at", None),
        offboarding_started_at=getattr(row, "offboarding_started_at", None),
        deleted_at=getattr(row, "deleted_at", None),
        retention_until=getattr(row, "retention_until", None),
    )


def _record_to_response(record: TenantRecord) -> TenantResponse:
    return TenantResponse(
        tenant_id=record.tenant_id,
        display_name=record.display_name,
        state=record.state.value,
        tier=record.tier,
        provisioned_at=record.provisioned_at,
        suspended_at=record.suspended_at,
        offboarding_started_at=record.offboarding_started_at,
        retention_until=record.retention_until,
        deleted_at=record.deleted_at,
    )


def _apply_to_row(row: TenantRow, record: TenantRecord) -> None:
    row.display_name = record.display_name
    if hasattr(row, "tier"):
        row.tier = record.tier
    if hasattr(row, "state"):
        row.state = record.state.value
    if hasattr(row, "provisioned_at"):
        row.provisioned_at = record.provisioned_at
    if hasattr(row, "suspended_at"):
        row.suspended_at = record.suspended_at
    if hasattr(row, "offboarding_started_at"):
        row.offboarding_started_at = record.offboarding_started_at
    if hasattr(row, "retention_until"):
        row.retention_until = record.retention_until
    if hasattr(row, "deleted_at"):
        row.deleted_at = record.deleted_at


# ─── Dependency: DB session getter (provided by application) ──


async def _db_session() -> AsyncSession:
    """Placeholder — the platform-api application overrides this with
    its real session factory at router-registration time."""
    raise HTTPException(status_code=500, detail="db session not configured")


# ─── Routes ────────────────────────────────────────────────────


@router.get("", response_model=TenantListResponse)
async def list_tenants(
    state: Optional[str] = Query(None, description="Filter by lifecycle state"),
    user: NexusUser = Depends(get_current_user),
    session: AsyncSession = Depends(_db_session),
) -> TenantListResponse:
    _require_admin(user)
    stmt = select(TenantRow)
    if state:
        stmt = stmt.where(TenantRow.state == state)
    result = await session.execute(stmt)
    rows = list(result.scalars().all())
    return TenantListResponse(
        tenants=[_record_to_response(_row_to_record(r)) for r in rows],
        total=len(rows),
    )


@router.get("/{tenant_id}", response_model=TenantResponse)
async def get_tenant(
    tenant_id: str,
    user: NexusUser = Depends(get_current_user),
    session: AsyncSession = Depends(_db_session),
) -> TenantResponse:
    _require_admin(user)
    row = await session.get(TenantRow, tenant_id)
    if row is None:
        raise HTTPException(status_code=404, detail="tenant not found")
    return _record_to_response(_row_to_record(row))


@router.post("", response_model=TenantResponse, status_code=201)
async def create_tenant(
    req: CreateTenantRequest,
    user: NexusUser = Depends(get_current_user),
    session: AsyncSession = Depends(_db_session),
) -> TenantResponse:
    _require_admin(user)
    tenant_id = req.tenant_id or f"tn_{uuid.uuid4().hex[:16]}"
    existing = await session.get(TenantRow, tenant_id)
    if existing is not None:
        raise HTTPException(status_code=409, detail="tenant already exists")

    row = TenantRow(
        tenant_id=tenant_id,
        display_name=req.display_name,
        created_at=datetime.now(timezone.utc),
    )
    if hasattr(row, "tier"):
        row.tier = req.tier
    if hasattr(row, "state"):
        row.state = TenantState.PENDING.value
    session.add(row)
    await session.commit()
    await session.refresh(row)
    logger.info(
        "tenant.created tenant_id=%s tier=%s actor=%s",
        tenant_id, req.tier, user.email,
    )
    return _record_to_response(_row_to_record(row))


@router.post("/{tenant_id}/provision", response_model=TenantResponse)
async def provision_tenant(
    tenant_id: str,
    user: NexusUser = Depends(get_current_user),
    session: AsyncSession = Depends(_db_session),
) -> TenantResponse:
    return await _apply_transition(
        tenant_id, lambda r: provision(r, actor=user.email),
        user, session,
    )


@router.post("/{tenant_id}/suspend", response_model=TenantResponse)
async def suspend_tenant(
    tenant_id: str,
    req: TransitionRequest,
    user: NexusUser = Depends(get_current_user),
    session: AsyncSession = Depends(_db_session),
) -> TenantResponse:
    return await _apply_transition(
        tenant_id, lambda r: suspend(r, actor=user.email, reason=req.reason),
        user, session,
    )


@router.post("/{tenant_id}/resume", response_model=TenantResponse)
async def resume_tenant(
    tenant_id: str,
    user: NexusUser = Depends(get_current_user),
    session: AsyncSession = Depends(_db_session),
) -> TenantResponse:
    return await _apply_transition(
        tenant_id, lambda r: resume(r, actor=user.email),
        user, session,
    )


@router.post("/{tenant_id}/offboard", response_model=TenantResponse)
async def offboard_tenant(
    tenant_id: str,
    req: OffboardRequest,
    user: NexusUser = Depends(get_current_user),
    session: AsyncSession = Depends(_db_session),
) -> TenantResponse:
    return await _apply_transition(
        tenant_id,
        lambda r: start_offboarding(
            r, actor=user.email, retention_days=req.retention_days,
        ),
        user, session,
    )


@router.post("/{tenant_id}/finalize-deletion", response_model=TenantResponse)
async def finalize_deletion_route(
    tenant_id: str,
    user: NexusUser = Depends(get_current_user),
    session: AsyncSession = Depends(_db_session),
) -> TenantResponse:
    """Hard-delete the tenant's Personal Data, then mark deleted.
    Refuses if the retention window hasn't elapsed."""
    return await _apply_transition(
        tenant_id, lambda r: finalize_deletion(r, actor=user.email),
        user, session,
    )


@router.get("/{tenant_id}/export")
async def export_tenant_data(
    tenant_id: str,
    user: NexusUser = Depends(get_current_user),
    session: AsyncSession = Depends(_db_session),
) -> dict:
    """Kick off an async GDPR-export job for the tenant.

    The export walks every tenant-scoped table + every artifact-store
    prefix and packages results into a downloadable archive. Status is
    polled via /v1/admin/tenants/{tenant_id}/export/{job_id}.

    The actual export job runs as a workflow (kind=tenant.export); this
    endpoint just creates the workflow and returns the workflow_id."""
    _require_admin(user)
    row = await session.get(TenantRow, tenant_id)
    if row is None:
        raise HTTPException(status_code=404, detail="tenant not found")

    # In a full implementation we'd dispatch a tenant.export workflow
    # here. That workflow's plan exists in plans.py (TBD) and walks:
    #   - canonical_artifacts WHERE tenant_id=...
    #   - sessions WHERE tenant_id=...
    #   - workflow_state WHERE tenant_id=...
    #   - object-storage prefix tenants/{tenant_id}/
    #   - audit_log WHERE actor_tenant_id=...
    # then packages everything as a downloadable archive.
    job_id = f"export_{uuid.uuid4().hex[:16]}"
    logger.info(
        "tenant.export_requested tenant_id=%s job_id=%s actor=%s",
        tenant_id, job_id, user.email,
    )
    return {
        "tenant_id": tenant_id,
        "job_id": job_id,
        "status": "pending",
        "note": (
            "Export workflow dispatch not yet implemented. "
            "Engineering ticket: plans.py tenant_export_plan() factory."
        ),
    }


# ─── Internal transition wrapper ───────────────────────────────


async def _apply_transition(
    tenant_id: str,
    transition_fn,
    user: NexusUser,
    session: AsyncSession,
) -> TenantResponse:
    _require_admin(user)
    row = await session.get(TenantRow, tenant_id)
    if row is None:
        raise HTTPException(status_code=404, detail="tenant not found")
    record = _row_to_record(row)
    try:
        new_record = transition_fn(record)
    except TenantLifecycleError as e:
        raise HTTPException(status_code=409, detail=str(e))
    _apply_to_row(row, new_record)
    await session.commit()
    await session.refresh(row)
    return _record_to_response(_row_to_record(row))
