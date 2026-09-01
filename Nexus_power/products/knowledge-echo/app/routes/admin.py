"""Admin endpoints — recent dispatches + manual simulate.

These are protected by the platform JWT. The ``simulate`` endpoint is
the integration test path: feed in any text and watch the orchestrator
execute end-to-end against the live tenant config.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from nexus_sdk.auth import NexusUser, get_current_user

from ..classifier import SenderContext
from ..orchestrator import EchoInput

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Echo Admin"], prefix="/api/v1/echo")


def _orchestrator(request: Request):
    svc = getattr(request.app.state, "orchestrator", None)
    if svc is None:
        raise HTTPException(503, "echo_orchestrator_not_initialised")
    return svc


def _repo(request: Request):
    svc = getattr(request.app.state, "dispatch_repo", None)
    if svc is None:
        raise HTTPException(503, "dispatch_repo_not_initialised")
    return svc


class SimulateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=4000)
    user_id_ext: Optional[str] = Field(default=None, max_length=128)
    channel_id_ext: Optional[str] = Field(default=None, max_length=128)
    thread_ts: Optional[str] = Field(default=None, max_length=64)
    surface: str = "slack"


class SimulateResponse(BaseModel):
    dispatch_id: Optional[str]
    decision: str
    decision_reason: str
    effective_mode: str
    posted_message_ref: Optional[str] = None


@router.post("/simulate", response_model=SimulateResponse)
async def simulate(
    body: SimulateRequest,
    request: Request,
    user: NexusUser = Depends(get_current_user),
) -> SimulateResponse:
    if user.role not in ("admin", "manager", "api"):
        raise HTTPException(403, "admin_or_manager_required")
    orch = _orchestrator(request)
    result = await orch.process(
        EchoInput(
            tenant_id=user.tenant_id,
            trigger_surface=body.surface,
            trigger_plugin_event_id=None,
            user_id_ext=body.user_id_ext,
            channel_id_ext=body.channel_id_ext,
            text=body.text,
            sender=SenderContext(surface=body.surface, role=user.role),
            thread_ts=body.thread_ts,
        )
    )
    return SimulateResponse(
        dispatch_id=result.dispatch_id,
        decision=result.decision,
        decision_reason=result.decision_reason,
        effective_mode=result.effective_mode,
        posted_message_ref=result.posted_message_ref,
    )


@router.get("/dispatches")
async def recent_dispatches(
    request: Request,
    limit: int = 50,
    user: NexusUser = Depends(get_current_user),
) -> list[dict[str, Any]]:
    if user.role not in ("admin", "manager", "viewer", "api"):
        raise HTTPException(403, "auth_required")
    limit = max(1, min(500, int(limit)))
    repo = _repo(request)
    rows = await repo.recent_decisions(user.tenant_id, limit=limit)
    return [
        {
            "dispatch_id": r["dispatch_id"],
            "decision": r["decision"],
            "confidence_band": r["confidence_band"],
            "top_similarity": r["top_similarity"],
            "posted_message_ref": r["posted_message_ref"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        }
        for r in rows
    ]
