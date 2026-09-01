"""
Platform API — Session routes (Modules 1 & 2).

CRUD for KT sessions, events and transcripts.
All endpoints require JWT authentication and enforce tenant isolation
by binding tenant_id to the authenticated user's tenant.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel
from sqlalchemy import select, desc

from nexus_sdk.db.models import SessionRow

from ..auth import get_current_user
from ..database import require_db, new_id, utc_now, row_to_dict

router = APIRouter(tags=["Sessions"])


# ─── Helpers ──────────────────────────────────────────────────

def _tenant_id(user: dict) -> str:
    """Extract tenant_id from authenticated user context."""
    return user["tenant_id"]


async def _get_session_or_404(db, session_id: str, tenant_id: str) -> "SessionRow":
    """Fetch session and enforce tenant ownership."""
    row = await db.get(SessionRow, session_id)
    if not row or row.tenant_id != tenant_id:
        raise HTTPException(404, f"Session {session_id} not found")
    return row


# ─── GET Endpoints ────────────────────────────────────────────

@router.get("/api/v1/sessions")
async def list_sessions(user: dict = Depends(get_current_user)):
    tenant_id = _tenant_id(user)
    factory = require_db()
    async with factory() as db:
        result = await db.execute(
            select(SessionRow)
            .where(SessionRow.tenant_id == tenant_id)
            .order_by(desc(SessionRow.created_at))
        )
        return [
            {k: v for k, v in row_to_dict(r).items() if k not in ("events", "transcript")}
            for r in result.scalars().all()
        ]


@router.get("/api/v1/sessions/{session_id}")
async def get_session(
    session_id: str = Path(...),
    user: dict = Depends(get_current_user),
):
    tenant_id = _tenant_id(user)
    factory = require_db()
    async with factory() as db:
        row = await _get_session_or_404(db, session_id, tenant_id)
        d = row_to_dict(row)
        return {k: v for k, v in d.items() if k not in ("events", "transcript")}


@router.get("/api/v1/sessions/{session_id}/events")
async def get_session_events(
    session_id: str = Path(...),
    user: dict = Depends(get_current_user),
):
    tenant_id = _tenant_id(user)
    factory = require_db()
    async with factory() as db:
        row = await _get_session_or_404(db, session_id, tenant_id)
        return row.events or []


@router.get("/api/v1/sessions/{session_id}/transcript")
async def get_session_transcript(
    session_id: str = Path(...),
    user: dict = Depends(get_current_user),
):
    tenant_id = _tenant_id(user)
    factory = require_db()
    async with factory() as db:
        row = await _get_session_or_404(db, session_id, tenant_id)
        return row.transcript or []


# ─── POST / Create ────────────────────────────────────────────

class CreateSessionRequest(BaseModel):
    title: str
    session_type: str = "knowledge_transfer"
    sme_name: str = ""


@router.post("/api/v1/sessions")
async def create_session(
    req: CreateSessionRequest,
    user: dict = Depends(get_current_user),
):
    tenant_id = _tenant_id(user)
    factory = require_db()
    async with factory() as db:
        row = SessionRow(
            session_id=new_id(), tenant_id=tenant_id, title=req.title,
            session_type=req.session_type, sme_name=req.sme_name,
            status="scheduled", created_at=utc_now(),
        )
        db.add(row)
        await db.commit()
        return {"session_id": row.session_id, "status": "created"}


# ─── UPDATE ───────────────────────────────────────────────────

class UpdateSessionRequest(BaseModel):
    status: str | None = None
    rules_extracted: int | None = None
    confidence_score: float | None = None


@router.patch("/api/v1/sessions/{session_id}")
async def update_session(
    session_id: str,
    req: UpdateSessionRequest,
    user: dict = Depends(get_current_user),
):
    """Update mutable session fields (status, rules_extracted, etc.)."""
    tenant_id = _tenant_id(user)
    factory = require_db()
    async with factory() as db:
        row = await _get_session_or_404(db, session_id, tenant_id)
        if req.status is not None:
            row.status = req.status
        if req.rules_extracted is not None:
            row.rules_extracted = req.rules_extracted
        if req.confidence_score is not None:
            row.confidence_score = req.confidence_score
        row.updated_at = utc_now()
        await db.commit()
        return {"session_id": session_id, "status": row.status}


@router.post("/api/v1/sessions/{session_id}/cancel")
async def cancel_session(
    session_id: str = Path(...),
    user: dict = Depends(get_current_user),
):
    """Mark session as cancelled in the database."""
    tenant_id = _tenant_id(user)
    factory = require_db()
    async with factory() as db:
        row = await _get_session_or_404(db, session_id, tenant_id)
        if row.status in ("completed", "cancelled"):
            raise HTTPException(409, f"Session is already {row.status}")
        row.status = "cancelled"
        await db.commit()
        return {"session_id": session_id, "status": "cancelled"}


# ─── DELETE ───────────────────────────────────────────────────

@router.delete("/api/v1/sessions/{session_id}")
async def delete_session(
    session_id: str = Path(...),
    user: dict = Depends(get_current_user),
):
    """Permanently delete a session."""
    tenant_id = _tenant_id(user)
    factory = require_db()
    async with factory() as db:
        row = await _get_session_or_404(db, session_id, tenant_id)
        await db.delete(row)
        await db.commit()
        return {"deleted": True, "session_id": session_id}
