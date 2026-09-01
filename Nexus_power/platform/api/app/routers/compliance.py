"""
Platform API — Compliance routes (Module 10).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select

from nexus_sdk.db.models import JurisdictionRow

from ..database import require_db, new_id, utc_now, row_to_dict
from ..auth import get_current_user

router = APIRouter(tags=["Compliance"])


@router.get("/api/v1/compliance/jurisdictions")
async def list_jurisdictions(user: dict = Depends(get_current_user)):
    tenant_id = user["tenant_id"]
    factory = require_db()
    async with factory() as db:
        result = await db.execute(
            select(JurisdictionRow)
            .where(JurisdictionRow.tenant_id == tenant_id)
            .order_by(JurisdictionRow.name)
        )
        return [row_to_dict(r) for r in result.scalars().all()]


class CreateJurisdictionRequest(BaseModel):
    name: str
    code: str = ""
    region: str = ""


@router.post("/api/v1/compliance/jurisdictions")
async def create_jurisdiction(
    req: CreateJurisdictionRequest,
    user: dict = Depends(get_current_user),
):
    tenant_id = user["tenant_id"]
    factory = require_db()
    async with factory() as db:
        row = JurisdictionRow(
            jurisdiction_id=new_id(), tenant_id=tenant_id, name=req.name,
            code=req.code, region=req.region,
            status="active", created_at=utc_now(),
        )
        db.add(row)
        await db.commit()
        return {"jurisdiction_id": row.jurisdiction_id, "status": "created"}
