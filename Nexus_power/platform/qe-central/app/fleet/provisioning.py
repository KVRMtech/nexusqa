"""QE-Central Phase-7 — tenant provisioning + lifecycle (onboard a client in one call).

Onboarding 20+ clients must be a REPEATABLE OPERATION, not a manual scramble.
This module is the one place that:

  * :func:`provision_tenant`  — creates the tenant record (the ``nexus.tenants``
    registry row, INSERT-only for QE-Central, ON CONFLICT no-op), stamps QE-Central's
    OWN mutable lifecycle + plan/quota control record (``tenant_provisioning``,
    qecentral bounded context), and mints the tenant's FIRST admin principal token
    (Verdict-audience, ``role='admin'``, the new ``tenant_id``, NO platform-admin
    marker).  Returns the onboarding :class:`TenantHandle`.
  * :func:`suspend_tenant` / :func:`resume_tenant` — flip the lifecycle status so a
    tenant's crawls / regression cycles are refused (suspend) or allowed (resume)
    at the shared ``driver.create_cycle`` / crawl-dispatch choke points.
  * :func:`offboard_tenant` — revoke the tenant's tokens, mark it offboarding, and
    SCHEDULE data-retention per policy.  It NEVER hard-deletes evidence — evidence
    is a regulated proof-of-behavior record; a retention job (a separate seam)
    performs any eventual deletion, and ONLY when a purge was explicitly requested.

Discipline (every operation):
  * IDEMPOTENT — re-provisioning an existing tenant returns its handle (a fresh
    token); suspending an already-suspended tenant is a no-op; etc.
  * FAIL-CLOSED in a DEPLOYED env — :func:`provision_tenant` / :func:`offboard_tenant`
    reuse the Phase-6 boot-safety checks and REFUSE if the process is a deployed env
    still wearing development defaults (dev KEK / default secrets).  (Suspend/resume
    stay available as an emergency stop.)
  * RLS EVERYWHERE — the control record is written through a tenant-scoped qecentral
    session; the registry row through the tenant-scoped substrate session.

ZERO LLM.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import new_id, tenant_scoped_qec_session, tenant_scoped_substrate_session, utc_now
from ..db.fleet_models import TenantProvisioningRow
from ..security.boot_validator import collect_boot_violations
from .lifecycle import (
    ACTION_OFFBOARD,
    ACTION_RESUME,
    ACTION_SUSPEND,
    STATUS_ACTIVE,
    STATUS_OFFBOARDING,
    STATUS_SUSPENDED,
    TenantLifecycleError,
    TenantProvisioningRecord,
    assert_tenant_operational,
    resolve_transition,
)
# Plan/quota semantics are owned by app.fleet.quota (the fleet quota sub-assignment);
# provisioning REUSES it — resolve_plan() accepts the stored ``tenant_provisioning.plan``
# column as its ``plan_name`` hint, the seam quota.py documents for exactly this caller.
from .quota import DEFAULT_PLAN_NAME, resolve_plan
from .rbac import mint_tenant_principal_jwt

logger = logging.getLogger(__name__)

# A permissive-but-real email shape (not RFC-perfect — rejects the obvious junk).
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Idempotent registry bootstrap (mirror of artifacts.creator._TENANT_BOOTSTRAP_SQL,
# with the caller-supplied plan/name/domain).  ON CONFLICT (tenant_id) DO NOTHING:
# an already-bootstrapped tenant (e.g. from a prior crawl) is never overwritten —
# QE-Central's authoritative lifecycle lives in tenant_provisioning regardless.
_TENANT_REGISTRY_UPSERT_SQL = text(
    "INSERT INTO tenants (tenant_id, name, domain, plan, status) "
    "VALUES (:tid, :name, :domain, :plan, 'active') "
    "ON CONFLICT (tenant_id) DO NOTHING"
)


class ProvisioningError(Exception):
    """A provisioning/lifecycle operation was refused (fail-closed).

    Carries an HTTP ``status_code`` so the fleet router maps it to a clean 4xx
    (422 bad input, 403 fail-closed deploy-safety, 409 conflict) — never a 500.
    """

    def __init__(self, message: str, *, status_code: int = 422) -> None:
        self.status_code = int(status_code)
        self.message = str(message)
        super().__init__(self.message)


@dataclass
class TenantHandle:
    """The onboarding handle returned by :func:`provision_tenant`.

    Carries the client's bootstrap credential (``admin_token``) — returned ONCE to
    the operator, NEVER logged.  ``created`` is False on an idempotent
    re-provision.  ``quota`` is the resolved plan envelope (``None`` = unlimited).
    """

    tenant_id: str
    name: str
    plan: str
    admin_email: str
    status: str
    domain: str
    admin_token: str
    token_expires_at: datetime
    token_ttl_seconds: int
    quota: dict
    created: bool
    provisioned_at: Optional[datetime] = None

    def as_dict(self, *, include_token: bool = True) -> dict:
        """JSON-serialisable view.  Set ``include_token=False`` to redact the credential."""
        out = {
            "tenant_id": self.tenant_id,
            "name": self.name,
            "plan": self.plan,
            "admin_email": self.admin_email,
            "status": self.status,
            "domain": self.domain,
            "token_expires_at": self.token_expires_at.isoformat() if self.token_expires_at else None,
            "token_ttl_seconds": self.token_ttl_seconds,
            "quota": self.quota,
            "created": self.created,
            "provisioned_at": self.provisioned_at.isoformat() if self.provisioned_at else None,
        }
        if include_token:
            out["admin_token"] = self.admin_token
        return out


@dataclass
class LifecycleResult:
    """The result of a suspend / resume / offboard operation."""

    tenant_id: str
    status: str
    action: str
    changed: bool
    retention_until: Optional[datetime] = None
    tokens_revoked_at: Optional[datetime] = None
    purge_requested: bool = False
    evidence_retained: bool = True
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "tenant_id": self.tenant_id,
            "status": self.status,
            "action": self.action,
            "changed": self.changed,
            "retention_until": self.retention_until.isoformat() if self.retention_until else None,
            "tokens_revoked_at": (
                self.tokens_revoked_at.isoformat() if self.tokens_revoked_at else None
            ),
            "purge_requested": self.purge_requested,
            "evidence_retained": self.evidence_retained,
            **({"detail": self.detail} if self.detail else {}),
        }


# ── fail-closed deploy-safety (reuse the Phase-6 safety spine) ──────────────

def ensure_deploy_safe(operation: str) -> None:
    """REFUSE ``operation`` in a DEPLOYED env still wearing development defaults.

    Reuses :func:`app.security.boot_validator.collect_boot_violations` (the same
    checks the boot gate makes): in ``staging``/``production`` with ANY violation
    (dev KEK, default JWT/explorer secret, default DB password) it raises
    :class:`ProvisioningError` (403).  INERT in development/test — never blocks
    local dev or the test suite.  A secret VALUE is never surfaced (only the
    setting name + reason, per the boot validator's contract).
    """
    if not settings.is_deployed_env:
        return
    violations = collect_boot_violations(settings)
    if violations:
        logger.critical(
            "qec.fleet.deploy_unsafe_refused",
            extra={"operation": operation, "violation_count": len(violations)},
        )
        raise ProvisioningError(
            f"{operation} refused — deployed environment is wearing "
            f"{len(violations)} development default(s): " + "; ".join(violations),
            status_code=403,
        )


# ── helpers ─────────────────────────────────────────────────────────────────

def _normalize_plan(plan: str) -> str:
    """Canonicalise a requested plan name (trim/lower); empty ⇒ the unlimited default.

    The name is STORED on ``tenant_provisioning.plan`` and later fed to
    :func:`app.fleet.quota.resolve_plan` as its ``plan_name`` hint; an unknown name
    resolves there to the generous default (never a surprise tighten).
    """
    return str(plan or "").strip().lower() or DEFAULT_PLAN_NAME


def _validate_email(admin_email: str) -> str:
    email = (admin_email or "").strip()
    if not email or not _EMAIL_RE.match(email) or len(email) > 320:
        raise ProvisioningError("admin_email must be a valid email address", status_code=422)
    return email


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return s[:40] or "tenant"


def _derive_domain(tenant_id: str, name: str, explicit: Optional[str]) -> str:
    """A deterministic, UNIQUE registry domain.

    An explicit domain is honoured (validated for basic shape); otherwise a domain
    is derived that EMBEDS the ``tenant_id`` so it is unique per tenant and never
    collides with another tenant's derived domain.
    """
    if explicit and explicit.strip():
        dom = explicit.strip().lower()
        if len(dom) > 200 or " " in dom:
            raise ProvisioningError("domain must be a bare hostname (<= 200 chars)", status_code=422)
        return dom
    return f"{_slug(name)}-{tenant_id}.verdict.internal"[:200]


async def load_tenant_provisioning(
    session: AsyncSession, tenant_id: str,
) -> Optional[TenantProvisioningRow]:
    """Load one tenant's provisioning row within an EXISTING tenant-scoped session.

    Returns ``None`` when the tenant has no control record (an un-provisioned
    tenant — treated as ACTIVE / UNLIMITED downstream).
    """
    return (await session.execute(
        select(TenantProvisioningRow).where(TenantProvisioningRow.tenant_id == tenant_id)
    )).scalar_one_or_none()


async def assert_tenant_operational_db(
    session: AsyncSession, tenant_id: str, *, operation: str = "cycle",
) -> None:
    """Fail-closed lifecycle gate over an EXISTING tenant-scoped session.

    Loads the tenant's control record and applies the PURE
    :func:`app.fleet.lifecycle.assert_tenant_operational` gate — passes silently
    for an ACTIVE or un-provisioned tenant, raises
    :class:`app.fleet.lifecycle.TenantNotOperational` (403) for a suspended /
    offboarding / deleted tenant.  Called at the shared cycle-creation +
    crawl-dispatch choke points so a suspended tenant is blocked on BOTH the
    autonomous daemon and the human-triggered paths.
    """
    row = await load_tenant_provisioning(session, tenant_id)
    if row is None:
        return
    assert_tenant_operational(
        TenantProvisioningRecord.from_row(row), tenant_id=tenant_id, operation=operation,
    )


async def get_tenant_provisioning(tenant_id: str) -> Optional[TenantProvisioningRecord]:
    """Load a tenant's lifecycle record (opens its own tenant-scoped session)."""
    tid = str(tenant_id or "").strip()
    if not tid:
        return None
    async with tenant_scoped_qec_session(tid) as session:
        row = await load_tenant_provisioning(session, tid)
        return TenantProvisioningRecord.from_row(row) if row is not None else None


# ── provisioning ─────────────────────────────────────────────────────────────

async def provision_tenant(
    name: str,
    plan: str,
    admin_email: str,
    *,
    tenant_id: Optional[str] = None,
    domain: Optional[str] = None,
    quota_overrides: Optional[dict] = None,
    actor: str = "operator",
) -> TenantHandle:
    """Onboard a CLIENT TENANT in one call — registry row + control record + token.

    Steps (fail-closed in a deployed env wearing dev defaults):
      1. create the ``nexus.tenants`` registry row (ON CONFLICT no-op — the FK
         target + source-of-truth for tenant existence);
      2. stamp QE-Central's ``tenant_provisioning`` control record (status active,
         plan, admin_email) — IDEMPOTENT: a tenant that already has a record is
         returned as-is (``created=False``);
      3. mint the tenant's FIRST admin principal token (Verdict-audience,
         ``role='admin'``, the new ``tenant_id``, NO platform-admin marker).

    Args:
        name: human display name for the tenant (required).
        plan: the quota plan (see :mod:`app.fleet.quota`); unknown ⇒ unlimited.
        admin_email: the first admin's email (required, validated).
        tenant_id: reuse a specific id (IDEMPOTENCY KEY — re-provisioning the same
            id returns the existing handle); a fresh UUID is minted when omitted.
        domain: explicit registry domain; derived (unique per tenant) when omitted.
        quota_overrides: per-tenant deltas over the plan envelope.
        actor: who performed the onboarding (audit).

    Raises:
        ProvisioningError: invalid input, a deployed-env safety refusal (403), or a
            registry domain conflict (409).
    """
    ensure_deploy_safe("provision_tenant")

    display_name = (name or "").strip()
    if not display_name:
        raise ProvisioningError("name is required", status_code=422)
    email = _validate_email(admin_email)
    plan_name = _normalize_plan(plan)
    overrides = dict(quota_overrides or {})

    tid = str(tenant_id or "").strip() or new_id()
    registry_domain = _derive_domain(tid, display_name, domain)

    # ── 1) registry row (nexus.tenants) — INSERT-only, ON CONFLICT no-op ──────
    try:
        async with tenant_scoped_substrate_session(tid) as s:
            await s.execute(_TENANT_REGISTRY_UPSERT_SQL, {
                "tid": tid, "name": display_name[:200],
                "domain": registry_domain, "plan": plan_name[:50],
            })
    except IntegrityError as exc:
        # The tenants.domain UNIQUE constraint (a caller-supplied domain already in
        # use by a DIFFERENT tenant) — the derived domain embeds tenant_id and can
        # never collide, so this only fires for an explicit duplicate domain.
        logger.warning(
            "qec.fleet.registry_conflict",
            extra={"tenant_id": tid, "domain": registry_domain, "error": str(exc)[:200]},
        )
        raise ProvisioningError(
            f"registry domain {registry_domain!r} is already in use", status_code=409,
        )

    # ── 2) control record (qecentral) — IDEMPOTENT upsert ─────────────────────
    now = utc_now()
    async with tenant_scoped_qec_session(tid) as session:
        row = await load_tenant_provisioning(session, tid)
        if row is None:
            row = TenantProvisioningRow(
                tenant_id=tid,
                plan=plan_name,
                status=STATUS_ACTIVE,
                admin_email=email,
                display_name=display_name[:200],
                quota_overrides=overrides,
                provisioned_at=now,
                actor=str(actor or "")[:200],
                reason="provisioned",
            )
            session.add(row)
            await session.flush()
            created = True
        else:
            created = False
        record = TenantProvisioningRecord.from_row(row)

    # ── 3) mint the first admin principal token ───────────────────────────────
    admin_token, expires_at = mint_tenant_principal_jwt(tid, record.admin_email or email)
    # Resolve the plan's quota envelope via the fleet quota authority, feeding the
    # STORED plan column as the resolve hint (its documented seam).
    quota = resolve_plan(tid, plan_name=(record.plan or plan_name)).as_dict()

    logger.info(
        "qec.fleet.provisioned",
        extra={
            "tenant_id": tid, "plan": record.plan, "status": record.status,
            "created": created, "actor": str(actor or ""),
            "admin_email": record.admin_email,  # not a secret; the token is never logged
        },
    )
    return TenantHandle(
        tenant_id=tid,
        name=record.display_name or display_name,
        plan=record.plan or plan_name,
        admin_email=record.admin_email or email,
        status=record.status,
        domain=registry_domain,
        admin_token=admin_token,
        token_expires_at=expires_at,
        token_ttl_seconds=settings.qec_onboarding_token_ttl_seconds,
        quota=quota,
        created=created,
        provisioned_at=record.provisioned_at or now,
    )


# ── lifecycle (suspend / resume / offboard) ─────────────────────────────────

async def _apply_lifecycle_action(
    tenant_id: str,
    action: str,
    *,
    actor: str,
    reason: str = "",
    retention_days: Optional[int] = None,
    purge: bool = False,
) -> LifecycleResult:
    """Apply a lifecycle transition to a tenant's control record (idempotent).

    Loads (or creates) the record inside a tenant-scoped transaction, resolves the
    legal transition (raising :class:`TenantLifecycleError` on an illegal move),
    stamps the target status + the relevant lifecycle timestamps, and returns the
    result.  Offboarding sets ``retention_until`` + ``tokens_revoked_at`` and
    stores the ``purge_requested`` flag — it NEVER deletes evidence.
    """
    tid = str(tenant_id or "").strip()
    if not tid:
        raise ProvisioningError("tenant_id is required", status_code=422)

    now = utc_now()
    async with tenant_scoped_qec_session(tid) as session:
        row = await load_tenant_provisioning(session, tid)
        current = row.status if row is not None else None
        try:
            target, changed = resolve_transition(current, action)
        except TenantLifecycleError as exc:
            raise ProvisioningError(exc.message, status_code=exc.status_code)

        if row is None:
            # Suspend/offboard a tenant that was never fleet-provisioned: create a
            # control record so the lifecycle gate can enforce it.  (Resume on a
            # non-existent record is an idempotent no-op — target == active.)
            if not changed:
                return LifecycleResult(
                    tenant_id=tid, status=target, action=action, changed=False,
                    detail={"note": "no control record — already effectively active"},
                )
            row = TenantProvisioningRow(
                tenant_id=tid, plan=DEFAULT_PLAN_NAME, status=STATUS_ACTIVE,
                provisioned_at=now, actor=str(actor or "")[:200],
            )
            session.add(row)
            await session.flush()

        retention_until = row.retention_until
        tokens_revoked_at = row.tokens_revoked_at

        if changed:
            row.status = target
            if target == STATUS_SUSPENDED:
                row.suspended_at = now
            elif target == STATUS_ACTIVE:
                row.suspended_at = None  # resume clears the suspension marker
            elif target == STATUS_OFFBOARDING:
                row.offboarding_started_at = now
                days = (
                    settings.qec_offboard_retention_days
                    if retention_days is None else max(0, int(retention_days))
                )
                retention_until = now + timedelta(days=days)
                tokens_revoked_at = now  # revoke the tenant's principal tokens
                row.retention_until = retention_until
                row.tokens_revoked_at = tokens_revoked_at
                if purge:
                    row.purge_requested = True
            row.actor = str(actor or "")[:200]
            row.reason = str(reason or action)[:2000]
            row.updated_at = now
        elif action == ACTION_OFFBOARD and purge and not row.purge_requested:
            # Idempotent re-offboard that ADDS a purge request (never un-sets one).
            row.purge_requested = True
            row.updated_at = now

        purge_flag = bool(row.purge_requested)

    logger.info(
        "qec.fleet.lifecycle",
        extra={"tenant_id": tid, "action": action, "status": target,
               "changed": changed, "actor": str(actor or ""), "purge": purge_flag},
    )
    return LifecycleResult(
        tenant_id=tid, status=target, action=action, changed=changed,
        retention_until=retention_until, tokens_revoked_at=tokens_revoked_at,
        purge_requested=purge_flag, evidence_retained=True,
    )


async def suspend_tenant(tenant_id: str, *, actor: str = "operator", reason: str = "") -> LifecycleResult:
    """Suspend a tenant — its crawls / regression cycles are then REFUSED (403).

    Idempotent (already-suspended ⇒ no-op).  Deliberately NOT gated by the
    deploy-safety check: suspend is an emergency stop and must always be available.
    """
    return await _apply_lifecycle_action(tenant_id, ACTION_SUSPEND, actor=actor, reason=reason)


async def resume_tenant(tenant_id: str, *, actor: str = "operator", reason: str = "") -> LifecycleResult:
    """Resume a suspended tenant back to ACTIVE.  Idempotent (already-active ⇒ no-op).

    Raises :class:`ProvisioningError` (409) on an illegal move (e.g. resume an
    offboarding/deleted tenant).
    """
    return await _apply_lifecycle_action(tenant_id, ACTION_RESUME, actor=actor, reason=reason)


async def offboard_tenant(
    tenant_id: str,
    *,
    actor: str = "operator",
    reason: str = "",
    retention_days: Optional[int] = None,
    purge: bool = False,
) -> LifecycleResult:
    """Offboard a tenant: revoke tokens, mark offboarding, SCHEDULE data-retention.

    Evidence is RETAINED (never hard-deleted here) — a retention job performs any
    eventual deletion, and only when ``purge`` was explicitly requested.  The
    tenant's crawls / cycles are refused from this point (offboarding is
    non-operational).  Fail-closed in a deployed env wearing dev defaults.

    Args:
        retention_days: how long evidence is kept (default ``QEC_OFFBOARD_RETENTION_DAYS``).
        purge: request eventual hard-deletion of evidence after retention lapses
            (the explicit retention flag — WITHOUT it evidence is kept indefinitely).
    """
    ensure_deploy_safe("offboard_tenant")
    return await _apply_lifecycle_action(
        tenant_id, ACTION_OFFBOARD, actor=actor, reason=reason,
        retention_days=retention_days, purge=purge,
    )


__all__ = [
    "ProvisioningError",
    "TenantHandle",
    "LifecycleResult",
    "ensure_deploy_safe",
    "load_tenant_provisioning",
    "assert_tenant_operational_db",
    "get_tenant_provisioning",
    "provision_tenant",
    "suspend_tenant",
    "resume_tenant",
    "offboard_tenant",
]
