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

from fastapi import APIRouter, Depends, HTTPException, Query, Request
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


# ═══════════════════════════════════════════════════════════════════════════
# TEAM A / PHASE A — the WORKER ANNOUNCEMENT seam (G1).
#
# Frozen wire shape: Nexus_power/contracts/fleet_heartbeat_v1.json — the
# explorer's producer half is engines/qe-explorer/app/heartbeat.py, and the two
# sides each assert the contract in their own process.
#
# MOUNTED UNDER /internal, DELIBERATELY, not under this file's /api prefix and
# not under a bare /fleet: the explorer holds no JWT, and the /internal prefix
# is the one boundary the per-fleet token middleware authenticates BEFORE any
# handler runs (M0.5 T-SEC-02 — auth.internal_auth_middleware). A bare /fleet
# prefix would have been anonymously reachable up to the handler, which is the
# exact hole T-SEC-02 closed once already.
#
# AUTH, two factors, same as every /internal route: the boundary middleware
# checks X-QEC-Token; each handler verifies the v2 X-QEC-Signature envelope
# over the exact body bytes, scope-bound to the worker id — so a captured
# heartbeat cannot be replayed as a different worker's. WORKER IDENTITY is the
# shared fleet secret for now (any fleet member can claim any worker_id); that
# is the Team F seam and the contract says so plainly.
# ═══════════════════════════════════════════════════════════════════════════

worker_router = APIRouter(prefix="/internal/fleet", tags=["QEC Fleet Workers"])

#: Contract constant — replaced by a per-worker key id when Team F lands.
WORKER_IDENTITY = "fleet-secret"


def _fleet_audit(endpoint: str, *, reason: str, **fields) -> None:
    """Structured refusal audit (mirrors internal._audit_refusal — WHAT and
    WHY, with ids to correlate; never a token, signature, nonce or body)."""
    logger.warning(
        "qec.security.fleet_worker_refused endpoint=%s reason=%s %s",
        endpoint, reason,
        " ".join(f"{k}={v}" for k, v in sorted(fields.items())),
    )


async def _signed_body(request: Request, *, endpoint: str, scope: str) -> dict:
    """Verify the scope-bound signature over the EXACT bytes; return the body.

    One function for both worker routes so neither can be added back with a
    weaker check. Raises 401 on any signature failure (stale, replayed,
    re-scoped, wrong key — the categories are logged by verify_signature)."""
    import json as _json

    from ..clients.config import SIGNATURE_HEADER, phase1_settings

    raw = await request.body()
    signature = request.headers.get(SIGNATURE_HEADER, "")
    if not phase1_settings.verify_signature(raw, signature, scope=scope):
        _fleet_audit(endpoint, reason="bad_or_replayed_signature",
                     has_signature=bool(signature))
        raise HTTPException(status_code=401, detail="invalid or missing signature")
    try:
        body = _json.loads(raw or b"{}")
    except _json.JSONDecodeError:
        body = None
    if not isinstance(body, dict):
        _fleet_audit(endpoint, reason="malformed_body")
        raise HTTPException(status_code=400, detail="malformed JSON body")
    return body


@worker_router.post("/workers/register")
async def register_explorer_worker(request: Request) -> dict:
    """A worker announces itself (idempotent by worker_id; re-registration is a
    restart and resets in_flight to 0 — see worker_registry.register_worker).

    The SCOPE is derived from the body's worker_id and the signature covers the
    body, so a caller cannot rewrite the id without invalidating its own
    signature."""
    from ..controlplane.scheduling import worker_registry

    raw_peek = await request.body()
    try:
        import json as _json
        wid_claim = str((_json.loads(raw_peek or b"{}") or {}).get("worker_id") or "")
    except Exception:
        wid_claim = ""
    body = await _signed_body(
        request, endpoint="worker-register",
        scope=f"worker-register:{wid_claim}")

    problem = worker_registry.validate_registration(body)
    if problem:
        _fleet_audit("worker-register", reason="invalid_registration",
                     worker_id=str(body.get("worker_id") or "(absent)"),
                     problem=problem[:200])
        raise HTTPException(status_code=422, detail=problem)

    meta = body.get("meta") if isinstance(body.get("meta"), dict) else {}
    out = await worker_registry.register_worker(
        worker_id=str(body["worker_id"]),
        url=str(body["url"]).strip(),
        allowlist_path=str(body["allowlist_path"]).strip(),
        capacity=int(body["capacity"]),
        tenant_affinity=str(body.get("tenant_affinity") or ""),
        meta={str(k)[:64]: str(v)[:200] for k, v in list(meta.items())[:16]},
    )
    return {
        "worker_id": out["worker_id"],
        "registered": True,
        "capacity": int(body["capacity"]),
        "heartbeat_interval_s": out["heartbeat_interval_s"],
        "heartbeat_ttl_s": out["heartbeat_ttl_s"],
        "worker_identity": WORKER_IDENTITY,
    }


@worker_router.post("/workers/{worker_id}/heartbeat")
async def explorer_worker_heartbeat(worker_id: str, request: Request) -> dict:
    """A worker proves it is alive and reports what it is actually running.

    404 for an UNKNOWN worker id is contract behaviour, not an error path: it
    tells the worker the registry was reset under it and it must RE-REGISTER,
    declaring capacity and affinity again, rather than be resurrected with
    defaults nobody chose."""
    from ..controlplane.scheduling import worker_registry

    body = await _signed_body(
        request, endpoint="worker-heartbeat",
        scope=f"worker-heartbeat:{worker_id}")
    if str(body.get("worker_id") or "") != worker_id:
        _fleet_audit("worker-heartbeat", reason="worker_id_mismatch",
                     path_worker=worker_id[:64])
        raise HTTPException(status_code=400, detail="worker_id path/body mismatch")

    status = str(body.get("status") or "") or None
    if status not in (None, worker_registry.STATUS_ACTIVE,
                      worker_registry.STATUS_DRAINING,
                      worker_registry.STATUS_DISABLED):
        status = None                      # an unknown status never writes
    try:
        in_flight = int(body.get("in_flight"))
    except (TypeError, ValueError):
        in_flight = None
    try:
        capacity = int(body.get("capacity"))
    except (TypeError, ValueError):
        capacity = None
    if capacity is not None and not (
            worker_registry.CAPACITY_MIN <= capacity <= worker_registry.CAPACITY_MAX):
        capacity = None

    known = await worker_registry.heartbeat(
        worker_id=worker_id, in_flight=in_flight, status=status,
        capacity=capacity)
    if not known:
        raise HTTPException(
            status_code=404,
            detail="unknown worker - re-register (the registry may have been reset)")
    return {
        "worker_id": worker_id,
        "acknowledged": True,
        "heartbeat_interval_s": worker_registry.heartbeat_interval_seconds(),
        "heartbeat_ttl_s": worker_registry.heartbeat_ttl_seconds(),
    }
