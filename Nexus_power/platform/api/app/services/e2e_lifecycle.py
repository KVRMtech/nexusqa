"""E2E Scenario lifecycle service — pure functions over E2EScenarioStateRow.

P3 Lifecycle & Curation:

Scenarios live in the canonical artifact's ``e2e_architect_cache`` JSON blob.
This module owns the *durable* layer (state, comments, audit log) that
survives cache regenerations.  A scenario without a row is implicitly in
``draft`` state — rows are created lazily on first transition.

State transitions are validated against the canonical state-machine here so
HTTP routers don't have to duplicate the rules.

Public surface:
    fetch_scenario_states_map(db, artifact_id, tenant_id)  → dict[scenario_id, dict]
    transition_scenario(db, ...)                           → updated state row dict
    add_scenario_comment(db, ...)                          → updated state row dict
    is_state_allowed(from_state, to_state)                 → bool

All functions take an async session and return plain dicts.  The router
layer handles auth, request validation, and HTTP error mapping.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nexus_sdk.db.models import (
    E2EScenarioStateRow,
    E2E_LIFECYCLE_STATES,
    E2E_STATE_DRAFT,
    E2E_STATE_REVIEWED,
    E2E_STATE_APPROVED,
    E2E_STATE_REJECTED,
    E2E_STATE_AUTOMATED,
    E2E_STATE_LIVE,
    E2E_STATE_STABLE,
    E2E_STATE_FAILING,
    E2E_EXPORTABLE_STATES,
)

_logger = logging.getLogger(__name__)


# ─── State Machine ───────────────────────────────────────────────────────
#
# Allowed transitions. A scenario can always revert to draft (e.g. when
# regenerated). approved → rejected is allowed (un-approval). live → failing
# is allowed (CI signal). Plus a few other reasonable moves.
#
# Keys = source state. Values = set of allowed destination states.
_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    E2E_STATE_DRAFT: frozenset({
        E2E_STATE_REVIEWED, E2E_STATE_APPROVED, E2E_STATE_REJECTED,
    }),
    E2E_STATE_REVIEWED: frozenset({
        E2E_STATE_DRAFT, E2E_STATE_APPROVED, E2E_STATE_REJECTED,
    }),
    E2E_STATE_APPROVED: frozenset({
        E2E_STATE_REVIEWED, E2E_STATE_REJECTED, E2E_STATE_AUTOMATED,
    }),
    E2E_STATE_REJECTED: frozenset({
        E2E_STATE_DRAFT, E2E_STATE_REVIEWED,
    }),
    E2E_STATE_AUTOMATED: frozenset({
        E2E_STATE_APPROVED, E2E_STATE_LIVE, E2E_STATE_FAILING,
    }),
    E2E_STATE_LIVE: frozenset({
        E2E_STATE_STABLE, E2E_STATE_FAILING, E2E_STATE_AUTOMATED,
    }),
    E2E_STATE_STABLE: frozenset({
        E2E_STATE_LIVE, E2E_STATE_FAILING,
    }),
    E2E_STATE_FAILING: frozenset({
        E2E_STATE_LIVE, E2E_STATE_AUTOMATED, E2E_STATE_REVIEWED,
    }),
}


def is_state_allowed(from_state: str, to_state: str) -> bool:
    """Return True when ``to_state`` is a legal successor of ``from_state``.

    Also returns True when both states are equal (idempotent no-op).
    """
    if to_state not in E2E_LIFECYCLE_STATES:
        return False
    if from_state == to_state:
        return True
    return to_state in _ALLOWED_TRANSITIONS.get(from_state, frozenset())


def allowed_next_states(from_state: str) -> list[str]:
    """List of legal next states from the given current state."""
    return sorted(_ALLOWED_TRANSITIONS.get(from_state, frozenset()))


def is_exportable_state(state: str) -> bool:
    """Whether scenarios in this state should be included in test exports."""
    return state in E2E_EXPORTABLE_STATES


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return str(uuid.uuid4())


def _row_to_dict(row: E2EScenarioStateRow) -> dict:
    return {
        "state_id": row.state_id,
        "artifact_id": row.artifact_id,
        "scenario_id": row.scenario_id,
        "tenant_id": row.tenant_id,
        "session_id": row.session_id,
        "state": row.state,
        "state_changed_at": row.state_changed_at.isoformat() if row.state_changed_at else None,
        "state_changed_by": row.state_changed_by,
        "state_changed_by_email": row.state_changed_by_email,
        "comments_json": list(row.comments_json or []),
        "audit_log_json": list(row.audit_log_json or []),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


# ─── Public functions ────────────────────────────────────────────────────


async def fetch_scenario_states_map(
    db: AsyncSession,
    *,
    artifact_id: str,
    tenant_id: str,
) -> dict[str, dict]:
    """Return a dict keyed by scenario_id of all state rows for this artifact.

    Scenarios without a row aren't included — callers should treat any
    missing scenario_id as implicitly in ``draft`` state.
    """
    result = await db.execute(
        select(E2EScenarioStateRow).where(
            E2EScenarioStateRow.artifact_id == artifact_id,
            E2EScenarioStateRow.tenant_id == tenant_id,
        )
    )
    rows = result.scalars().all()
    return {row.scenario_id: _row_to_dict(row) for row in rows}


async def _get_or_create_row(
    db: AsyncSession,
    *,
    artifact_id: str,
    scenario_id: str,
    tenant_id: str,
    session_id: str = "",
) -> E2EScenarioStateRow:
    """Fetch the state row, creating one in ``draft`` state if absent."""
    result = await db.execute(
        select(E2EScenarioStateRow).where(
            E2EScenarioStateRow.artifact_id == artifact_id,
            E2EScenarioStateRow.scenario_id == scenario_id,
            E2EScenarioStateRow.tenant_id == tenant_id,
        )
    )
    row = result.scalar_one_or_none()
    if row is not None:
        return row

    row = E2EScenarioStateRow(
        state_id=_new_id(),
        artifact_id=artifact_id,
        scenario_id=scenario_id,
        tenant_id=tenant_id,
        session_id=session_id,
        state=E2E_STATE_DRAFT,
        state_changed_at=_utc_now(),
        state_changed_by="",
        state_changed_by_email="",
        comments_json=[],
        audit_log_json=[],
        created_at=_utc_now(),
        updated_at=_utc_now(),
    )
    db.add(row)
    await db.flush()
    return row


async def transition_scenario(
    db: AsyncSession,
    *,
    artifact_id: str,
    scenario_id: str,
    new_state: str,
    user_id: str,
    user_email: str,
    note: str = "",
    tenant_id: str,
    session_id: str = "",
) -> dict:
    """Transition a scenario to ``new_state`` and append to the audit log.

    Raises HTTPException(422) if the transition is not allowed.
    Returns the updated state row as a dict.
    """
    if new_state not in E2E_LIFECYCLE_STATES:
        raise HTTPException(422, f"Unknown lifecycle state: {new_state!r}")

    row = await _get_or_create_row(
        db,
        artifact_id=artifact_id,
        scenario_id=scenario_id,
        tenant_id=tenant_id,
        session_id=session_id,
    )

    from_state = row.state
    if not is_state_allowed(from_state, new_state):
        raise HTTPException(
            422,
            f"Illegal transition: {from_state!r} → {new_state!r}. "
            f"Allowed: {allowed_next_states(from_state)}",
        )

    if from_state == new_state:
        # Idempotent no-op — return current row without bumping audit log
        return _row_to_dict(row)

    now = _utc_now()
    audit_entry = {
        "from_state": from_state,
        "to_state": new_state,
        "user_id": user_id,
        "email": user_email,
        "at": now.isoformat(),
        "note": note or "",
    }
    # SQLAlchemy doesn't track in-place mutations of JSON columns reliably;
    # reassign a new list so the row is marked dirty.
    row.audit_log_json = list(row.audit_log_json or []) + [audit_entry]
    row.state = new_state
    row.state_changed_at = now
    row.state_changed_by = user_id
    row.state_changed_by_email = user_email
    row.updated_at = now

    await db.commit()
    await db.refresh(row)
    _logger.info(
        "e2e_lifecycle.transition artifact=%s scenario=%s %s→%s by=%s",
        artifact_id, scenario_id, from_state, new_state, user_email or user_id,
    )
    return _row_to_dict(row)


async def add_scenario_comment(
    db: AsyncSession,
    *,
    artifact_id: str,
    scenario_id: str,
    body: str,
    user_id: str,
    user_email: str,
    tenant_id: str,
    session_id: str = "",
) -> dict:
    """Append a comment to a scenario's thread.

    Creates a draft state row if none exists. Returns the updated row.
    """
    body = (body or "").strip()
    if not body:
        raise HTTPException(422, "Comment body cannot be empty")
    if len(body) > 5000:
        raise HTTPException(422, "Comment body exceeds 5000 characters")

    row = await _get_or_create_row(
        db,
        artifact_id=artifact_id,
        scenario_id=scenario_id,
        tenant_id=tenant_id,
        session_id=session_id,
    )
    now = _utc_now()
    comment = {
        "comment_id": _new_id(),
        "user_id": user_id,
        "email": user_email,
        "body": body,
        "created_at": now.isoformat(),
    }
    row.comments_json = list(row.comments_json or []) + [comment]
    row.updated_at = now

    await db.commit()
    await db.refresh(row)
    return _row_to_dict(row)


def merge_states_into_scenarios(
    scenarios: list[dict],
    *,
    states_map: dict[str, dict],
) -> list[dict]:
    """Attach lifecycle state to each scenario dict in-place and return it.

    Scenarios without a state row get ``state='draft'`` and empty
    comments/audit fields so the frontend can render uniformly.
    """
    for sc in scenarios:
        sid = sc.get("scenario_id")
        state_row = states_map.get(sid) if sid else None
        if state_row:
            sc["state"] = state_row.get("state", E2E_STATE_DRAFT)
            sc["state_changed_at"] = state_row.get("state_changed_at")
            sc["state_changed_by"] = state_row.get("state_changed_by", "")
            sc["state_changed_by_email"] = state_row.get("state_changed_by_email", "")
            sc["comments"] = state_row.get("comments_json", [])
            sc["audit_log"] = state_row.get("audit_log_json", [])
        else:
            sc["state"] = E2E_STATE_DRAFT
            sc["state_changed_at"] = None
            sc["state_changed_by"] = ""
            sc["state_changed_by_email"] = ""
            sc["comments"] = []
            sc["audit_log"] = []
    return scenarios


def filter_exportable_scenarios(scenarios: list[dict]) -> list[dict]:
    """Return only scenarios whose state qualifies for test export
    (approved / automated / live / stable). Scenarios without a state field
    are assumed draft and excluded."""
    return [
        sc for sc in scenarios
        if sc.get("state") and is_exportable_state(sc["state"])
    ]
