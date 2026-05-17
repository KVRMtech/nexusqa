"""
Platform API — SME Profile routes (Module 3).
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Path, Depends
from sqlalchemy import select, desc

from nexus_sdk.db.models import SMEProfileRow

from ..database import require_db, row_to_dict
from ..auth import get_current_user

router = APIRouter(tags=["SME Profiles"])


@router.get("/api/v1/sme/profiles")
async def list_sme_profiles(user: dict = Depends(get_current_user)):
    tenant_id = user["tenant_id"]
    factory = require_db()
    async with factory() as db:
        result = await db.execute(
            select(SMEProfileRow)
            .where(SMEProfileRow.tenant_id == tenant_id)
            .order_by(desc(SMEProfileRow.created_at))
        )
        return [row_to_dict(r) for r in result.scalars().all()]


@router.get("/api/v1/sme/profiles/{speaker_id}")
async def get_sme_profile(
    speaker_id: str = Path(...),
    user: dict = Depends(get_current_user),
):
    factory = require_db()
    async with factory() as db:
        row = await db.get(SMEProfileRow, speaker_id)
        if not row:
            raise HTTPException(404, f"SME profile {speaker_id} not found")
        return row_to_dict(row)
