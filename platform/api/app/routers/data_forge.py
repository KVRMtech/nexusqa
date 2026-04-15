"""
Platform API — Data Forge routes (Module 9).
"""
from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import select, desc

from nexus_sdk.db.models import ForgeConfigRow, ForgeResultRow

from ..database import require_db, new_id, utc_now, row_to_dict

router = APIRouter(tags=["Data Forge"])


@router.get("/api/v1/data-forge/configs")
async def list_forge_configs(tenant_id: str = Query("t-1")):
    factory = require_db()
    async with factory() as db:
        result = await db.execute(
            select(ForgeConfigRow)
            .where(ForgeConfigRow.tenant_id == tenant_id)
            .order_by(desc(ForgeConfigRow.created_at))
        )
        return [row_to_dict(r) for r in result.scalars().all()]


@router.get("/api/v1/data-forge/results")
async def list_forge_results(tenant_id: str = Query("t-1")):
    factory = require_db()
    async with factory() as db:
        result = await db.execute(
            select(ForgeResultRow)
            .where(ForgeResultRow.tenant_id == tenant_id)
            .order_by(desc(ForgeResultRow.created_at))
        )
        return [row_to_dict(r) for r in result.scalars().all()]


# ─── POST / Create ────────────────────────────────────────────

class CreateForgeConfigRequest(BaseModel):
    tenant_id: str
    name: str
    schema_definition: dict = {}
    record_count: int = 100
    format: str = "json"


@router.post("/api/v1/data-forge/configs")
async def create_forge_config(req: CreateForgeConfigRequest):
    factory = require_db()
    async with factory() as db:
        row = ForgeConfigRow(
            config_id=new_id(), tenant_id=req.tenant_id, name=req.name,
            schema_definition=req.schema_definition, record_count=req.record_count,
            format=req.format, status="active", created_at=utc_now(),
        )
        db.add(row)
        await db.commit()
        return {"config_id": row.config_id, "status": "created"}
