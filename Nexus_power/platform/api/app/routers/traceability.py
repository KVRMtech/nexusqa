"""
Platform API — Traceability routes (Module 7).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select, desc

from nexus_sdk.db.models import TraceRow

from ..database import require_db, row_to_dict
from ..auth import get_current_user

router = APIRouter(tags=["Traceability"])


@router.get("/api/v1/traceability")
async def list_traces(user: dict = Depends(get_current_user)):
    tenant_id = user["tenant_id"]
    factory = require_db()
    async with factory() as db:
        result = await db.execute(
            select(TraceRow)
            .where(TraceRow.tenant_id == tenant_id)
            .order_by(desc(TraceRow.created_at))
        )
        return [row_to_dict(r) for r in result.scalars().all()]
