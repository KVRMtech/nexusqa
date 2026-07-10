"""QE-Central Phase-7 — the FLEET provisioning API (``/api/v1/qec/tenants``).

The operator surface that turns onboarding a CLIENT into ONE call:

  * ``POST   /tenants``                 — provision a tenant (registry row +
    control record + first-admin token); returns the onboarding handle.
  * ``POST   /tenants/{id}/suspend``    — suspend (crawls/cycles then refused).
  * ``POST   /tenants/{id}/resume``     — resume a suspended tenant.
  * ``DELETE /tenants/{id}``            — offboard (revoke tokens + schedule
    data-retention); evidence is RETAINED (never hard-deleted here).
  * ``GET    /tenants/{id}``            — the tenant's lifecycle/plan status.

RBAC is a NEW PLATFORM SUPER-ADMIN scope (:func:`app.fleet.rbac.require_platform_admin`):
every endpoint requires ``role='admin'`` AND the ``platform_admin`` marker, so a
tenant's OWN admin (which has ``role='admin'`` but no marker) can NOT provision,
suspend, or offboard other tenants — the fleet-operator boundary the plain
role-gate cannot draw.  Imports come from the fleet SUBMODULES directly (not the
package ``__init__``) so this router is independent of the fleet quota
sub-package's export surface.  ZERO LLM.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ..fleet.provisioning import (
    ProvisioningError,
    offboard_tenant,
    provision_tenant,
    resume_tenant,
    suspend_tenant,
)
from ..fleet.rbac import require_platform_admin
from ..fleet.provisioning import get_tenant_provisioning

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/qec", tags=["QEC Fleet"])

_SUPER_ADMIN = require_platform_admin


def _actor(user: dict) -> str:
    return str(user.get("sub") or user.get("email") or "operator")


class ProvisionRequest(BaseModel):
    """Onboard one client tenant."""

    name: str = Field(min_length=1, max_length=200)
    admin_email: str = Field(min_length=3, max_length=320)
    plan: str = Field(default="", max_length=50)
    # Reuse a specific id → IDEMPOTENCY KEY (re-provisioning returns the handle).
    tenant_id: str | None = Field(default=None, max_length=64)
    domain: str | None = Field(default=None, max_length=200)
    quota_overrides: dict | None = None


class LifecycleRequest(BaseModel):
    """Suspend / resume payload — an auditable reason (optional)."""

    reason: str = Field(default="", max_length=2000)


@router.post("/tenants", status_code=201)
async def provision(payload: ProvisionRequest, user: dict = Depends(_SUPER_ADMIN)) -> dict:
    """Provision a client tenant; return the onboarding handle (incl. admin token).

    IDEMPOTENT: re-provisioning a ``tenant_id`` that already exists returns its
    handle with ``created=false`` (and a fresh token).  Fail-closed in a deployed
    env still wearing development defaults.
    """
    try:
        handle = await provision_tenant(
            payload.name, payload.plan, payload.admin_email,
            tenant_id=payload.tenant_id, domain=payload.domain,
            quota_overrides=payload.quota_overrides, actor=_actor(user),
        )
    except ProvisioningError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    except ValueError as exc:  # defensive — mint/tenant guards
        raise HTTPException(status_code=422, detail=str(exc))
    logger.info(
        "qec.fleet.api.provisioned",
        extra={"tenant_id": handle.tenant_id, "created": handle.created,
               "plan": handle.plan, "actor": _actor(user)},
    )
    return handle.as_dict(include_token=True)


@router.post("/tenants/{tenant_id}/suspend")
async def suspend(
    tenant_id: str, payload: LifecycleRequest, user: dict = Depends(_SUPER_ADMIN),
) -> dict:
    """Suspend a tenant — its crawls / regression cycles are then refused."""
    try:
        result = await suspend_tenant(tenant_id, actor=_actor(user), reason=payload.reason)
    except ProvisioningError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    return result.as_dict()


@router.post("/tenants/{tenant_id}/resume")
async def resume(
    tenant_id: str, payload: LifecycleRequest, user: dict = Depends(_SUPER_ADMIN),
) -> dict:
    """Resume a suspended tenant back to ACTIVE (idempotent)."""
    try:
        result = await resume_tenant(tenant_id, actor=_actor(user), reason=payload.reason)
    except ProvisioningError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    return result.as_dict()


@router.delete("/tenants/{tenant_id}")
async def offboard(
    tenant_id: str,
    user: dict = Depends(_SUPER_ADMIN),
    retention_days: int | None = Query(
        default=None, ge=0,
        description="Days evidence is retained after offboarding (default: QEC_OFFBOARD_RETENTION_DAYS).",
    ),
    purge: bool = Query(
        default=False,
        description="Request eventual hard-deletion of evidence after retention lapses "
                    "(the explicit retention flag; WITHOUT it evidence is kept indefinitely).",
    ),
    reason: str = Query(default="", max_length=2000),
) -> dict:
    """Offboard a tenant: revoke tokens + schedule data-retention.

    Evidence is RETAINED (never hard-deleted here); a purge is only SCHEDULED when
    ``purge=true``.  Fail-closed in a deployed env wearing development defaults.
    """
    try:
        result = await offboard_tenant(
            tenant_id, actor=_actor(user), reason=reason,
            retention_days=retention_days, purge=purge,
        )
    except ProvisioningError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    return result.as_dict()


@router.get("/tenants/{tenant_id}")
async def get_tenant(tenant_id: str, user: dict = Depends(_SUPER_ADMIN)) -> dict:
    """The tenant's lifecycle + plan status (404 when never fleet-provisioned)."""
    record = await get_tenant_provisioning(tenant_id)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail="tenant has no provisioning record (never fleet-provisioned)",
        )
    return {
        "tenant_id": record.tenant_id,
        "plan": record.plan,
        "status": record.status,
        "admin_email": record.admin_email,
        "display_name": record.display_name,
        "is_operational": record.is_operational,
        "provisioned_at": record.provisioned_at.isoformat() if record.provisioned_at else None,
        "suspended_at": record.suspended_at.isoformat() if record.suspended_at else None,
        "offboarding_started_at": (
            record.offboarding_started_at.isoformat() if record.offboarding_started_at else None
        ),
        "retention_until": record.retention_until.isoformat() if record.retention_until else None,
        "tokens_revoked_at": record.tokens_revoked_at.isoformat() if record.tokens_revoked_at else None,
        "purge_requested": record.purge_requested,
    }
