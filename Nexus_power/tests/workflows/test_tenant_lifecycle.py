"""
Phase 16 — Tenant lifecycle state machine tests.

Covers every legal transition + every illegal one. The state graph is
small enough to enumerate exhaustively.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from nexus_sdk.tenant_lifecycle import (
    TenantLifecycleError,
    TenantRecord,
    TenantState,
    can_transition,
    finalize_deletion,
    provision,
    resume,
    start_offboarding,
    suspend,
    transition,
)


def _new_tenant(state: TenantState = TenantState.PENDING) -> TenantRecord:
    return TenantRecord(
        tenant_id="tn-1",
        display_name="Acme Corp",
        tier="pilot",
        state=state,
    )


# ─── Legal transitions ─────────────────────────────────────────


def test_pending_to_active_via_provision():
    t = _new_tenant(TenantState.PENDING)
    out = provision(t, actor="op@nexus.test")
    assert out.state == TenantState.ACTIVE
    assert out.provisioned_at is not None
    # Original record unchanged (immutable shape).
    assert t.state == TenantState.PENDING


def test_active_to_suspended():
    t = _new_tenant(TenantState.ACTIVE)
    out = suspend(t, actor="op@nexus.test", reason="abuse investigation")
    assert out.state == TenantState.SUSPENDED
    assert out.suspended_at is not None


def test_suspended_to_active_via_resume_clears_suspension_marker():
    t = _new_tenant(TenantState.SUSPENDED)
    t.suspended_at = datetime.now(timezone.utc)
    out = resume(t, actor="op@nexus.test")
    assert out.state == TenantState.ACTIVE
    assert out.suspended_at is None


def test_active_to_offboarding_sets_retention():
    t = _new_tenant(TenantState.ACTIVE)
    out = start_offboarding(t, actor="op@nexus.test", retention_days=30)
    assert out.state == TenantState.OFFBOARDING
    assert out.offboarding_started_at is not None
    assert out.retention_until is not None
    expected_until = (out.offboarding_started_at + timedelta(days=30))
    # Allow 1s tolerance for clock skew between transitions.
    assert abs((out.retention_until - expected_until).total_seconds()) < 1


def test_suspended_to_offboarding():
    """Direct offboard from suspended skips the active intermediate."""
    t = _new_tenant(TenantState.SUSPENDED)
    out = start_offboarding(t, actor="op@nexus.test")
    assert out.state == TenantState.OFFBOARDING


def test_offboarding_to_deleted_after_retention_elapsed():
    t = _new_tenant(TenantState.OFFBOARDING)
    t.retention_until = datetime.now(timezone.utc) - timedelta(seconds=1)
    out = finalize_deletion(t, actor="op@nexus.test")
    assert out.state == TenantState.DELETED
    assert out.deleted_at is not None


# ─── Illegal transitions ───────────────────────────────────────


def test_pending_cannot_suspend():
    t = _new_tenant(TenantState.PENDING)
    with pytest.raises(TenantLifecycleError, match="illegal transition"):
        suspend(t, actor="op@nexus.test", reason="x")


def test_active_cannot_provision_again():
    t = _new_tenant(TenantState.ACTIVE)
    with pytest.raises(TenantLifecycleError, match="illegal transition"):
        provision(t, actor="op@nexus.test")


def test_deleted_is_terminal():
    t = _new_tenant(TenantState.DELETED)
    for target in (
        TenantState.PENDING, TenantState.ACTIVE,
        TenantState.SUSPENDED, TenantState.OFFBOARDING,
    ):
        with pytest.raises(TenantLifecycleError):
            transition(t, target, actor="op@nexus.test")


def test_offboarding_cannot_go_back_to_active():
    """Once offboarding starts, no resume path. The customer must
    re-create a new tenant if they change their mind."""
    t = _new_tenant(TenantState.OFFBOARDING)
    with pytest.raises(TenantLifecycleError):
        transition(t, TenantState.ACTIVE, actor="op@nexus.test")


def test_offboarding_cannot_be_skipped_to_deleted_before_retention():
    """The retention window is a HARD lock — even an admin must wait."""
    t = _new_tenant(TenantState.OFFBOARDING)
    t.retention_until = datetime.now(timezone.utc) + timedelta(days=10)
    with pytest.raises(TenantLifecycleError, match="retention period"):
        finalize_deletion(t, actor="op@nexus.test")


def test_finalize_deletion_refuses_non_offboarding_source():
    """You can only finalize from offboarding, never from active."""
    t = _new_tenant(TenantState.ACTIVE)
    with pytest.raises(TenantLifecycleError):
        finalize_deletion(t, actor="op@nexus.test")


# ─── can_transition() pure function ───────────────────────────


def test_can_transition_exhaustive_matrix():
    """Every (from, to) pair: spot-check the full state graph."""
    legal = {
        (TenantState.PENDING, TenantState.ACTIVE),
        (TenantState.PENDING, TenantState.DELETED),
        (TenantState.ACTIVE, TenantState.SUSPENDED),
        (TenantState.ACTIVE, TenantState.OFFBOARDING),
        (TenantState.SUSPENDED, TenantState.ACTIVE),
        (TenantState.SUSPENDED, TenantState.OFFBOARDING),
        (TenantState.OFFBOARDING, TenantState.DELETED),
    }
    for src in TenantState:
        for tgt in TenantState:
            expected = (src, tgt) in legal
            assert can_transition(src, tgt) is expected, (
                f"can_transition({src.value} → {tgt.value}) "
                f"returned {can_transition(src, tgt)}, expected {expected}"
            )


# ─── Immutability semantics ────────────────────────────────────


def test_transition_returns_new_record_does_not_mutate_input():
    """The state machine is pure: input record is untouched."""
    t = _new_tenant(TenantState.ACTIVE)
    original_state = t.state
    original_suspended_at = t.suspended_at
    out = suspend(t, actor="op@nexus.test", reason="x")
    assert t.state == original_state
    assert t.suspended_at == original_suspended_at
    assert out.state != t.state
