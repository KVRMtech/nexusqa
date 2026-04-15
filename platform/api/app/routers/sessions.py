"""
Platform API — Session routes (Modules 1 & 2).

CRUD for KT sessions, events and transcripts.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Path
from pydantic import BaseModel
from sqlalchemy import select, desc

from nexus_sdk.db.models import SessionRow

from ..database import require_db, new_id, utc_now, row_to_dict

router = APIRouter(tags=["Sessions"])


# ─── GET Endpoints ────────────────────────────────────────────

@router.get("/api/v1/sessions")
async def list_sessions(tenant_id: str = Query("t-1")):
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
async def get_session(session_id: str = Path(...)):
    factory = require_db()
    async with factory() as db:
        row = await db.get(SessionRow, session_id)
        if not row:
            raise HTTPException(404, f"Session {session_id} not found")
        d = row_to_dict(row)
        return {k: v for k, v in d.items() if k not in ("events", "transcript")}


@router.get("/api/v1/sessions/{session_id}/events")
async def get_session_events(session_id: str = Path(...)):
    factory = require_db()
    async with factory() as db:
        row = await db.get(SessionRow, session_id)
        if not row:
            raise HTTPException(404, f"Session {session_id} not found")
        return row.events or []


@router.get("/api/v1/sessions/{session_id}/transcript")
async def get_session_transcript(session_id: str = Path(...)):
    factory = require_db()
    async with factory() as db:
        row = await db.get(SessionRow, session_id)
        if not row:
            raise HTTPException(404, f"Session {session_id} not found")
        return row.transcript or []


# ─── POST / Create ────────────────────────────────────────────

class CreateSessionRequest(BaseModel):
    tenant_id: str
    title: str
    session_type: str = "knowledge_transfer"
    sme_name: str = ""


@router.post("/api/v1/sessions")
async def create_session(req: CreateSessionRequest):
    factory = require_db()
    async with factory() as db:
        row = SessionRow(
            session_id=new_id(), tenant_id=req.tenant_id, title=req.title,
            session_type=req.session_type, sme_name=req.sme_name,
            status="scheduled", created_at=utc_now(),
        )
        db.add(row)
        await db.commit()
        return {"session_id": row.session_id, "status": "created"}


# ─── CANCEL ───────────────────────────────────────────────────

class UpdateSessionRequest(BaseModel):
    status: str | None = None
    rules_extracted: int | None = None
    confidence_score: float | None = None


@router.patch("/api/v1/sessions/{session_id}")
async def update_session(session_id: str, req: UpdateSessionRequest):
    """Update mutable session fields (status, rules_extracted, etc.)."""
    factory = require_db()
    async with factory() as db:
        row = await db.get(SessionRow, session_id)
        if not row:
            raise HTTPException(404, f"Session {session_id} not found")
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
async def cancel_session(session_id: str = Path(...)):
    """Mark session as cancelled in the database."""
    factory = require_db()
    async with factory() as db:
        row = await db.get(SessionRow, session_id)
        if not row:
            raise HTTPException(404, f"Session {session_id} not found")
        if row.status in ("completed", "cancelled"):
            raise HTTPException(409, f"Session is already {row.status}")
        row.status = "cancelled"
        await db.commit()
        return {"session_id": session_id, "status": "cancelled"}


# ─── DELETE ───────────────────────────────────────────────────

@router.delete("/api/v1/sessions/{session_id}")
async def delete_session(session_id: str = Path(...)):
    """Permanently delete a session."""
    factory = require_db()
    async with factory() as db:
        row = await db.get(SessionRow, session_id)
        if not row:
            raise HTTPException(404, f"Session {session_id} not found")
        await db.delete(row)
        await db.commit()
        return {"deleted": True, "session_id": session_id}
