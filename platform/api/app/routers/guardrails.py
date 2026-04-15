"""
Platform API — Guardrail / AI Confidence routes (Module 6).
"""
from __future__ import annotations

from fastapi import APIRouter, Query
from sqlalchemy import select, desc, and_

from nexus_sdk.db.models import GuardrailPipelineRow, ReviewQueueRow, TrustTrendRow

from ..database import require_db, row_to_dict

router = APIRouter(tags=["Guardrails"])


@router.get("/api/v1/guardrails/pipeline")
async def get_guardrail_pipeline(tenant_id: str = Query("t-1")):
    factory = require_db()
    async with factory() as db:
        result = await db.execute(
            select(GuardrailPipelineRow)
            .where(GuardrailPipelineRow.tenant_id == tenant_id)
            .order_by(GuardrailPipelineRow.step_order)
        )
        return [row_to_dict(r) for r in result.scalars().all()]


@router.get("/api/v1/guardrails/review-queue")
async def get_review_queue(tenant_id: str = Query("t-1")):
    factory = require_db()
    async with factory() as db:
        result = await db.execute(
            select(ReviewQueueRow)
            .where(and_(
                ReviewQueueRow.tenant_id == tenant_id,
                ReviewQueueRow.status == "pending",
            ))
            .order_by(desc(ReviewQueueRow.created_at))
        )
        return [row_to_dict(r) for r in result.scalars().all()]


@router.get("/api/v1/guardrails/trust-trend")
async def get_trust_trend(tenant_id: str = Query("t-1")):
    factory = require_db()
    async with factory() as db:
        result = await db.execute(
            select(TrustTrendRow)
            .where(TrustTrendRow.tenant_id == tenant_id)
            .order_by(TrustTrendRow.period)
        )
        return [row_to_dict(r) for r in result.scalars().all()]
