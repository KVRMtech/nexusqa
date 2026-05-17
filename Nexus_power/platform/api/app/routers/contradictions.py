"""
Platform API — Contradiction routes (Module 5).
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Path, Depends
from pydantic import BaseModel
from sqlalchemy import select, desc

from nexus_sdk.db.models import ContradictionRow, AuditLogRow

from ..database import require_db, new_id, utc_now, row_to_dict
from ..auth import get_current_user

router = APIRouter(tags=["Contradictions"])


@router.get("/api/v1/contradictions")
async def list_contradictions(user: dict = Depends(get_current_user)):
    tenant_id = user["tenant_id"]
    factory = require_db()
    async with factory() as db:
        result = await db.execute(
            select(ContradictionRow)
            .where(ContradictionRow.tenant_id == tenant_id)
            .order_by(desc(ContradictionRow.created_at))
        )
        return [row_to_dict(r) for r in result.scalars().all()]


class ResolveRequest(BaseModel):
    resolution: str


@router.post("/api/v1/contradictions/{contradiction_id}/resolve")
async def resolve_contradiction(
    contradiction_id: str = Path(...),
    body: ResolveRequest = ...,
    user: dict = Depends(get_current_user),
):
    factory = require_db()
    async with factory() as db:
        row = await db.get(ContradictionRow, contradiction_id)
        if not row:
            raise HTTPException(404, f"Contradiction {contradiction_id} not found")
        row.status = "resolved"
        row.resolution = body.resolution
        row.resolved_at = utc_now()
        audit = AuditLogRow(
            log_id=new_id(),
            tenant_id=row.tenant_id,
            engine="platform-api",
            action="CONTRADICTION_RESOLVED",
            entity_type="contradiction",
            entity_id=contradiction_id,
            details={"resolution": body.resolution[:500]},
        )
        db.add(audit)
        await db.commit()
        await db.refresh(row)
        return row_to_dict(row)


class CreateContradictionRequest(BaseModel):
    rule_a_id: str = ""
    rule_b_id: str = ""
    description: str = ""
    severity: str = "medium"


@router.post("/api/v1/contradictions")
async def create_contradiction(
    req: CreateContradictionRequest,
    user: dict = Depends(get_current_user),
):
    tenant_id = user["tenant_id"]
    factory = require_db()
    async with factory() as db:
        row = ContradictionRow(
            contradiction_id=new_id(), tenant_id=tenant_id,
            rule_a_id=req.rule_a_id, rule_b_id=req.rule_b_id,
            description=req.description, severity=req.severity,
            status="open", created_at=utc_now(),
        )
        db.add(row)
        await db.commit()
        return {"contradiction_id": row.contradiction_id, "status": "created"}
