"""
Phase 16 — Tenant lifecycle state machine.

A tenant moves through this state graph:

       ┌────────────────────────────────────────────────┐
       │                                                ▼
   ┌─────────┐    provision     ┌─────────┐  suspend  ┌──────────┐
   │ pending │ ───────────────► │ active  │ ────────► │suspended │
   └─────────┘                  └─────────┘ ◄──────── └──────────┘
                                  │  resume      │
                                  │              │
                                  │ offboard     │ offboard
                                  ▼              ▼
                             ┌─────────────────────────┐
                             │     offboarding         │
                             │  (export + retention)   │
                             └─────────┬───────────────┘
                                       │  retention expired
                                       ▼
                                  ┌─────────┐
                                  │ deleted │  (hard delete; immutable)
                                  └─────────┘

State transitions are explicit + audited. Every transition writes an
audit_log row. Once a tenant reaches `deleted` it is immutable — the
row exists for compliance but no Personal Data remains.

This module is the **state machine + event hooks**. The HTTP CRUD
endpoints in platform/api/app/routers/tenants.py call into it. The
provisioning side-effects (Milvus partition create, storage bucket
prefix, Postgres RLS context) live in tenant_provisioner.py.
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)


class TenantState(str, enum.Enum):
    PENDING = "pending"        # row created, provisioning hasn't run
    ACTIVE = "active"          # serving traffic
    SUSPENDED = "suspended"    # gateway 503; queue drained; data retained
    OFFBOARDING = "offboarding"  # export started; retention countdown active
    DELETED = "deleted"        # hard-deleted; immutable


class TenantLifecycleError(Exception):
    """An illegal transition or a precondition violation."""


# Allowed transitions. The (from, to) tuples define every legal move.
_ALLOWED_TRANSITIONS: dict[TenantState, set[TenantState]] = {
    TenantState.PENDING: {TenantState.ACTIVE, TenantState.DELETED},
    TenantState.ACTIVE: {TenantState.SUSPENDED, TenantState.OFFBOARDING},
    TenantState.SUSPENDED: {TenantState.ACTIVE, TenantState.OFFBOARDING},
    TenantState.OFFBOARDING: {TenantState.DELETED},
    TenantState.DELETED: set(),     # terminal
}


@dataclass
class TenantRecord:
    """Lightweight in-memory view of a tenant's lifecycle state.
    The DB-backed model lives in nexus_sdk.db.models.TenantRow; this
    is what the state machine operates on."""

    tenant_id: str
    state: TenantState
    display_name: str = ""
    tier: str = "pilot"                          # pilot | enterprise | internal
    provisioned_at: Optional[datetime] = None
    suspended_at: Optional[datetime] = None
    offboarding_started_at: Optional[datetime] = None
    deleted_at: Optional[datetime] = None
    retention_until: Optional[datetime] = None   # last day Personal Data is kept


# ─── Transition functions ──────────────────────────────────────


def can_transition(current: TenantState, target: TenantState) -> bool:
    """Pure check: is current → target a legal transition?"""
    return target in _ALLOWED_TRANSITIONS.get(current, set())


def transition(
    record: TenantRecord,
    target: TenantState,
    *,
    actor: str,
    reason: str = "",
    now: Optional[datetime] = None,
    retention_days: int = 30,
) -> TenantRecord:
    """Apply a transition. Returns a NEW TenantRecord with timestamps
    updated. Raises TenantLifecycleError on illegal moves.

    Callers must persist the returned record + write an audit_log row.
    """
    now = now or datetime.now(timezone.utc)
    if not can_transition(record.state, target):
        raise TenantLifecycleError(
            f"illegal transition {record.state.value} → {target.value} "
            f"for tenant {record.tenant_id!r}"
        )
    new = TenantRecord(
        tenant_id=record.tenant_id,
        state=target,
        display_name=record.display_name,
        tier=record.tier,
        provisioned_at=record.provisioned_at,
        suspended_at=record.suspended_at,
        offboarding_started_at=record.offboarding_started_at,
        deleted_at=record.deleted_at,
        retention_until=record.retention_until,
    )

    if target == TenantState.ACTIVE:
        if record.state == TenantState.PENDING:
            new.provisioned_at = now
        elif record.state == TenantState.SUSPENDED:
            new.suspended_at = None  # resume clears the suspension marker
    elif target == TenantState.SUSPENDED:
        new.suspended_at = now
    elif target == TenantState.OFFBOARDING:
        new.offboarding_started_at = now
        new.retention_until = now + timedelta(days=retention_days)
    elif target == TenantState.DELETED:
        new.deleted_at = now
        # Personal Data fields are cleared by the offboarding side-effect
        # job; the record-level fields stay so the audit trail survives.

    logger.info(
        "tenant_lifecycle.transition tenant=%s %s→%s actor=%s reason=%s",
        record.tenant_id, record.state.value, target.value, actor,
        reason[:200] if reason else "",
    )
    return new


# ─── Convenience wrappers ──────────────────────────────────────


def provision(record: TenantRecord, *, actor: str) -> TenantRecord:
    """pending → active."""
    return transition(record, TenantState.ACTIVE, actor=actor, reason="provisioned")


def suspend(record: TenantRecord, *, actor: str, reason: str) -> TenantRecord:
    """active → suspended. Caller must reason."""
    return transition(record, TenantState.SUSPENDED, actor=actor, reason=reason)


def resume(record: TenantRecord, *, actor: str) -> TenantRecord:
    """suspended → active."""
    return transition(record, TenantState.ACTIVE, actor=actor, reason="resumed")


def start_offboarding(
    record: TenantRecord, *, actor: str, retention_days: int = 30,
) -> TenantRecord:
    """active | suspended → offboarding. The retention clock starts here."""
    return transition(
        record, TenantState.OFFBOARDING,
        actor=actor, reason=f"offboarding started; retention={retention_days}d",
        retention_days=retention_days,
    )


def finalize_deletion(record: TenantRecord, *, actor: str) -> TenantRecord:
    """offboarding → deleted. Caller MUST have already hard-deleted the
    tenant's Personal Data before invoking this; the state machine
    doesn't do the side effects, it records the transition."""
    if record.state != TenantState.OFFBOARDING:
        raise TenantLifecycleError(
            f"can only finalize deletion from offboarding; was {record.state.value}"
        )
    if record.retention_until and datetime.now(timezone.utc) < record.retention_until:
        raise TenantLifecycleError(
            f"retention period not yet expired "
            f"(until {record.retention_until.isoformat()})"
        )
    return transition(record, TenantState.DELETED, actor=actor, reason="deletion finalized")


__all__ = [
    "TenantState",
    "TenantRecord",
    "TenantLifecycleError",
    "can_transition",
    "transition",
    "provision",
    "suspend",
    "resume",
    "start_offboarding",
    "finalize_deletion",
]
