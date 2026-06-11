"""Platform API — E2E Scenario Lifecycle routes (P3).

State management endpoints for the Test Studio:

  GET  /api/v1/e2e-architect/{artifact_id}/scenarios/state
       → Map of scenario_id → state row for an artifact.

  POST /api/v1/e2e-architect/{artifact_id}/scenarios/{scenario_id}/transition
       Body: {"new_state": str, "note": str}
       → Updated state row. Validates the transition against the lifecycle
         state machine.

  POST /api/v1/e2e-architect/{artifact_id}/scenarios/{scenario_id}/comment
       Body: {"body": str}
       → Updated state row (lazy-creates draft state if absent).

All endpoints are tenant-scoped via Depends(get_current_user). The
underlying state table also has Postgres RLS enforcing tenant_isolation.
"""
from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, Field
from sqlalchemy import select

from nexus_sdk.db.models import CanonicalArtifactRow

from ..database import require_db, tenant_scoped_session
from ..auth import get_current_user
from ..services.flywheel import ledger as flywheel_ledger
from ..services.e2e_lifecycle import (
    fetch_scenario_states_map,
    transition_scenario,
    add_scenario_comment,
    allowed_next_states,
)

router = APIRouter(tags=["E2E Lifecycle"])

_logger = logging.getLogger(__name__)


# ─── Request models ─────────────────────────────────────────────


class TransitionRequest(BaseModel):
    new_state: str = Field(..., description="Target lifecycle state")
    note: str = Field("", description="Optional reviewer note (audit trail)")


class CommentRequest(BaseModel):
    body: str = Field(..., min_length=1, max_length=5000)


# ─── Helpers ────────────────────────────────────────────────────


async def _assert_artifact_in_tenant(db, *, artifact_id: str, tenant_id: str) -> str:
    """Verify the artifact exists and belongs to this tenant.
    Returns the session_id so we can stamp it on new state rows.
    """
    result = await db.execute(
        select(CanonicalArtifactRow.session_id).where(
            CanonicalArtifactRow.artifact_id == artifact_id,
            CanonicalArtifactRow.tenant_id == tenant_id,
        )
    )
    session_id = result.scalar_one_or_none()
    if session_id is None:
        raise HTTPException(404, f"Artifact {artifact_id} not found")
    return session_id


# ─── Endpoints ──────────────────────────────────────────────────


@router.get("/api/v1/e2e-architect/{artifact_id}/scenarios/state")
async def get_scenario_states(
    artifact_id: str = Path(..., min_length=1),
    user: dict = Depends(get_current_user),
):
    """Return the map of scenario_id → state row for an artifact.

    Scenarios without a state row are NOT included — callers should treat
    missing entries as implicitly ``draft``.
    """
    tenant_id = user["tenant_id"]
    factory = require_db()
    async with factory() as db:
        await _assert_artifact_in_tenant(
            db, artifact_id=artifact_id, tenant_id=tenant_id,
        )
        states = await fetch_scenario_states_map(
            db, artifact_id=artifact_id, tenant_id=tenant_id,
        )
        return {
            "artifact_id": artifact_id,
            "states": states,
        }


@router.post(
    "/api/v1/e2e-architect/{artifact_id}/scenarios/{scenario_id}/transition"
)
async def transition_scenario_state(
    req: TransitionRequest,
    artifact_id: str = Path(..., min_length=1),
    scenario_id: str = Path(..., min_length=1),
    user: dict = Depends(get_current_user),
):
    """Transition a scenario to ``new_state``, appending an audit entry.

    Returns the updated state row. Raises 422 on illegal transitions.
    """
    tenant_id = user["tenant_id"]
    user_id = user.get("user_id", "")
    user_email = user.get("email", "")

    factory = require_db()
    async with factory() as db:
        session_id = await _assert_artifact_in_tenant(
            db, artifact_id=artifact_id, tenant_id=tenant_id,
        )
        state_row = await transition_scenario(
            db,
            artifact_id=artifact_id,
            scenario_id=scenario_id,
            new_state=req.new_state,
            user_id=user_id,
            user_email=user_email,
            note=req.note,
            tenant_id=tenant_id,
            session_id=session_id,
        )
        result = {
            "success": True,
            "state": state_row,
            "allowed_next_states": allowed_next_states(state_row["state"]),
        }

    # Flywheel (Phase 2): record the human's lifecycle DECISION — a de-identified
    # state-transition enum, never raw text. A SEPARATE best-effort session so a
    # pre-migration insert can never roll back the transition above; gated
    # default-OFF (capture_enabled short-circuits before any DB work).
    if flywheel_ledger.capture_enabled():
        try:
            async with tenant_scoped_session(tenant_id) as fly:
                await flywheel_ledger.record_label(
                    fly, tenant_id=tenant_id, decision_point="scenario_lifecycle",
                    artifact_id=artifact_id, scenario_id=scenario_id,
                    human_decision_enum=f"transition_to:{(req.new_state or '')[:18]}",
                    git_commit=os.getenv("NEXUS_GIT_COMMIT", ""),
                )
        except Exception:  # capture is optional — never affect the transition
            pass
    return result


@router.post(
    "/api/v1/e2e-architect/{artifact_id}/scenarios/{scenario_id}/comment"
)
async def add_comment(
    req: CommentRequest,
    artifact_id: str = Path(..., min_length=1),
    scenario_id: str = Path(..., min_length=1),
    user: dict = Depends(get_current_user),
):
    """Append a comment to a scenario's thread.

    Lazy-creates a draft state row if none exists. Returns the updated row.
    """
    tenant_id = user["tenant_id"]
    user_id = user.get("user_id", "")
    user_email = user.get("email", "")

    factory = require_db()
    async with factory() as db:
        session_id = await _assert_artifact_in_tenant(
            db, artifact_id=artifact_id, tenant_id=tenant_id,
        )
        state_row = await add_scenario_comment(
            db,
            artifact_id=artifact_id,
            scenario_id=scenario_id,
            body=req.body,
            user_id=user_id,
            user_email=user_email,
            tenant_id=tenant_id,
            session_id=session_id,
        )
        return {
            "success": True,
            "state": state_row,
        }
