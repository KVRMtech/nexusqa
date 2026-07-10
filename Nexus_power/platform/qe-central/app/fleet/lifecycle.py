"""QE-Central Phase-7 — the tenant lifecycle state machine (pure, fail-closed).

A CLIENT TENANT walks a small, explicit lifecycle graph:

    (none) --provision--> active <--resume-- suspended
                            |  \\                 |
                       suspend  \\ offboard       | offboard
                            |     \\              v
                            v      +-------> offboarding --finalize--> deleted
                        suspended                (evidence retained until the
                                                  retention window lapses)

This module is the PURE core (mirrors the ``app.security.prod_guard`` /
``nexus_sdk.tenant_lifecycle`` shape): a status vocabulary, the legal-transition
matrix, and :func:`assert_tenant_operational` — the fail-closed gate the cycle
driver / crawl dispatch call so a SUSPENDED (or offboarding / deleted) tenant can
never have a crawl or regression cycle fired against it.

BACKWARD-COMPAT (load-bearing): a tenant with NO provisioning record (all of
today's tenants) is treated as OPERATIONAL — the gate passes silently — so the
existing behavior is byte-for-byte preserved.  Only an EXPLICITLY non-active
record blocks work.

ZERO LLM, no I/O — every function is a pure predicate over a record/status, so
the whole gate is unit-testable with a plain namespace or mapping.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

# ── Lifecycle status vocabulary (tenant_provisioning.status) ────────────────
STATUS_ACTIVE = "active"
STATUS_SUSPENDED = "suspended"
STATUS_OFFBOARDING = "offboarding"
STATUS_DELETED = "deleted"

STATUSES = frozenset({STATUS_ACTIVE, STATUS_SUSPENDED, STATUS_OFFBOARDING, STATUS_DELETED})

#: The ONLY status in which a tenant may have crawls / cycles fired.
OPERATIONAL_STATUSES = frozenset({STATUS_ACTIVE})
#: A tenant in one of these can never transition again to active work.
TERMINAL_STATUSES = frozenset({STATUS_DELETED})

# ── Lifecycle actions (the verbs the fleet router / provisioning expose) ─────
ACTION_PROVISION = "provision"
ACTION_SUSPEND = "suspend"
ACTION_RESUME = "resume"
ACTION_OFFBOARD = "offboard"
ACTION_FINALIZE = "finalize"

#: Legal ``(current_status -> {target_status})`` transitions.  A brand-new tenant
#: (no record) provisions to ``active``; ``deleted`` is terminal.  A same-status
#: move (e.g. suspend an already-suspended tenant) is handled as an IDEMPOTENT
#: no-op by :func:`resolve_transition`, not by this matrix.
_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    STATUS_ACTIVE: frozenset({STATUS_SUSPENDED, STATUS_OFFBOARDING}),
    STATUS_SUSPENDED: frozenset({STATUS_ACTIVE, STATUS_OFFBOARDING}),
    STATUS_OFFBOARDING: frozenset({STATUS_DELETED}),
    STATUS_DELETED: frozenset(),
}

#: Which target status each mutating action drives toward.
_ACTION_TARGET: dict[str, str] = {
    ACTION_SUSPEND: STATUS_SUSPENDED,
    ACTION_RESUME: STATUS_ACTIVE,
    ACTION_OFFBOARD: STATUS_OFFBOARDING,
    ACTION_FINALIZE: STATUS_DELETED,
}


class TenantLifecycleError(Exception):
    """An ILLEGAL lifecycle transition (e.g. resume a deleted tenant).

    Carries an HTTP ``status_code`` (409 — the request conflicts with the
    tenant's current state) so a router can surface a clean, auditable refusal.
    """

    def __init__(self, message: str, *, status_code: int = 409) -> None:
        self.status_code = int(status_code)
        self.message = str(message)
        super().__init__(self.message)


class TenantNotOperational(Exception):
    """Raised when a crawl / cycle is attempted for a NON-operational tenant.

    Fail-closed signal mirroring :class:`app.security.prod_guard.OnboardingRefused`
    — carries an HTTP ``status_code`` (403; the tenant is suspended/offboarding),
    the offending ``status``, and an honest ``reason`` so the daemon can skip the
    app and a router can map it to a clean 403.
    """

    def __init__(
        self, *, tenant_id: str = "", status: str = "", operation: str = "cycle",
        status_code: int = 403,
    ) -> None:
        self.status_code = int(status_code)
        self.tenant_id = str(tenant_id or "")
        self.status = str(status or "")
        self.operation = str(operation or "cycle")
        self.reason = (
            f"tenant is {self.status or 'not operational'!r} — "
            f"a {self.operation} may run only for an ACTIVE tenant"
        )
        super().__init__(self.reason)

    def as_http_detail(self) -> dict:
        """A structured, JSON-serialisable HTTP ``detail`` body (never a bare str)."""
        return {
            "refused": True,
            "reason": "tenant_not_operational",
            "status": self.status,
            "operation": self.operation,
            "message": self.reason,
        }


@dataclass(frozen=True)
class TenantProvisioningRecord:
    """A pure, I/O-free view of one tenant's provisioning/lifecycle state.

    Built from the ``tenant_provisioning`` ORM row (:meth:`from_row`) OR any
    mapping/namespace (:meth:`from_any`) so every predicate here is testable with
    a plain object.  ``None`` timestamps mean "that lifecycle event has not
    happened".
    """

    tenant_id: str
    plan: str = ""
    status: str = STATUS_ACTIVE
    admin_email: str = ""
    display_name: str = ""
    quota_overrides: Mapping[str, Any] = None  # type: ignore[assignment]
    provisioned_at: Optional[datetime] = None
    suspended_at: Optional[datetime] = None
    offboarding_started_at: Optional[datetime] = None
    retention_until: Optional[datetime] = None
    tokens_revoked_at: Optional[datetime] = None
    purge_requested: bool = False
    actor: str = ""
    reason: str = ""

    @classmethod
    def from_row(cls, row: Any) -> "TenantProvisioningRecord":
        """Build from a ``TenantProvisioningRow`` ORM instance."""
        return cls(
            tenant_id=str(getattr(row, "tenant_id", "") or ""),
            plan=str(getattr(row, "plan", "") or ""),
            status=str(getattr(row, "status", "") or STATUS_ACTIVE),
            admin_email=str(getattr(row, "admin_email", "") or ""),
            display_name=str(getattr(row, "display_name", "") or ""),
            quota_overrides=dict(getattr(row, "quota_overrides", None) or {}),
            provisioned_at=getattr(row, "provisioned_at", None),
            suspended_at=getattr(row, "suspended_at", None),
            offboarding_started_at=getattr(row, "offboarding_started_at", None),
            retention_until=getattr(row, "retention_until", None),
            tokens_revoked_at=getattr(row, "tokens_revoked_at", None),
            purge_requested=bool(getattr(row, "purge_requested", False)),
            actor=str(getattr(row, "actor", "") or ""),
            reason=str(getattr(row, "reason", "") or ""),
        )

    @property
    def is_operational(self) -> bool:
        """True when the tenant may have crawls / cycles fired (status active)."""
        return self.status in OPERATIONAL_STATUSES


def is_operational(record_or_status: Any) -> bool:
    """True when ``record_or_status`` represents an OPERATIONAL tenant.

    Accepts a :class:`TenantProvisioningRecord`, a status string, a mapping with a
    ``status`` key, or ``None``.  ``None`` (no provisioning record) ⇒ True —
    backward-compat: an un-provisioned tenant is operational.
    """
    if record_or_status is None:
        return True
    if isinstance(record_or_status, TenantProvisioningRecord):
        return record_or_status.is_operational
    if isinstance(record_or_status, str):
        return record_or_status in OPERATIONAL_STATUSES
    if isinstance(record_or_status, Mapping):
        return str(record_or_status.get("status") or STATUS_ACTIVE) in OPERATIONAL_STATUSES
    status = getattr(record_or_status, "status", STATUS_ACTIVE)
    return str(status or STATUS_ACTIVE) in OPERATIONAL_STATUSES


def assert_tenant_operational(
    record_or_status: Any, *, tenant_id: str = "", operation: str = "cycle",
) -> None:
    """REFUSE (raise :class:`TenantNotOperational`) unless the tenant is operational.

    The single fail-closed gate the cycle driver / crawl dispatch call.  Passes
    SILENTLY when the tenant is ACTIVE or has no provisioning record; raises 403
    when it is suspended / offboarding / deleted.
    """
    if is_operational(record_or_status):
        return
    status = ""
    if isinstance(record_or_status, TenantProvisioningRecord):
        status = record_or_status.status
        tenant_id = tenant_id or record_or_status.tenant_id
    elif isinstance(record_or_status, str):
        status = record_or_status
    elif isinstance(record_or_status, Mapping):
        status = str(record_or_status.get("status") or "")
    else:
        status = str(getattr(record_or_status, "status", "") or "")
    raise TenantNotOperational(tenant_id=tenant_id, status=status, operation=operation)


def resolve_transition(current: Optional[str], action: str) -> tuple[str, bool]:
    """Resolve ``(current_status, action)`` → ``(target_status, changed)`` (pure).

    ``current`` is ``None`` for a tenant with no record yet.  Returns the target
    status and whether it is a REAL change (``False`` ⇒ idempotent no-op, e.g.
    suspending an already-suspended tenant).  Raises :class:`TenantLifecycleError`
    on an illegal move (e.g. resume a deleted tenant, offboard nothing).

    ``provision`` is handled by the provisioning flow itself (not here); this
    resolves the mutating lifecycle verbs suspend / resume / offboard / finalize.
    """
    cur = (current or STATUS_ACTIVE) if current is not None else None
    target = _ACTION_TARGET.get(action)
    if target is None:
        raise TenantLifecycleError(f"unknown lifecycle action {action!r}", status_code=422)

    # No record yet: only offboard/suspend make sense on an implicit-active tenant.
    if cur is None:
        cur = STATUS_ACTIVE

    if cur == target:
        # Idempotent: already in the requested state → no-op (never an error).
        return target, False

    allowed = _ALLOWED_TRANSITIONS.get(cur, frozenset())
    if target not in allowed:
        raise TenantLifecycleError(
            f"illegal transition {cur!r} -> {target!r} (action={action})",
            status_code=409,
        )
    return target, True


__all__ = [
    "STATUS_ACTIVE",
    "STATUS_SUSPENDED",
    "STATUS_OFFBOARDING",
    "STATUS_DELETED",
    "STATUSES",
    "OPERATIONAL_STATUSES",
    "TERMINAL_STATUSES",
    "ACTION_PROVISION",
    "ACTION_SUSPEND",
    "ACTION_RESUME",
    "ACTION_OFFBOARD",
    "ACTION_FINALIZE",
    "TenantLifecycleError",
    "TenantNotOperational",
    "TenantProvisioningRecord",
    "is_operational",
    "assert_tenant_operational",
    "resolve_transition",
]
