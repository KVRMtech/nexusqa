"""
Platform API — Test Suite & Run routes (Module 8).
"""
from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import select, desc

from nexus_sdk.db.models import TestSuiteRow, TestRunRow

from ..database import require_db, new_id, utc_now, row_to_dict

router = APIRouter(tags=["Tests"])


@router.get("/api/v1/tests/suites")
async def list_test_suites(tenant_id: str = Query("t-1")):
    factory = require_db()
    async with factory() as db:
        result = await db.execute(
            select(TestSuiteRow)
            .where(TestSuiteRow.tenant_id == tenant_id)
            .order_by(desc(TestSuiteRow.created_at))
        )
        return [row_to_dict(r) for r in result.scalars().all()]


@router.get("/api/v1/tests/runs")
async def list_test_runs(tenant_id: str = Query("t-1")):
    factory = require_db()
    async with factory() as db:
        result = await db.execute(
            select(TestRunRow)
            .where(TestRunRow.tenant_id == tenant_id)
            .order_by(desc(TestRunRow.started_at))
        )
        return [row_to_dict(r) for r in result.scalars().all()]


# ─── POST / Create ────────────────────────────────────────────

class CreateTestSuiteRequest(BaseModel):
    tenant_id: str
    name: str
    description: str = ""
    tags: list[str] = []


@router.post("/api/v1/tests/suites")
async def create_test_suite(req: CreateTestSuiteRequest):
    factory = require_db()
    async with factory() as db:
        row = TestSuiteRow(
            suite_id=new_id(), tenant_id=req.tenant_id, name=req.name,
            description=req.description, tags=req.tags,
            status="active", created_at=utc_now(),
        )
        db.add(row)
        await db.commit()
        return {"suite_id": row.suite_id, "status": "created"}


class CreateTestRunRequest(BaseModel):
    tenant_id: str
    suite_id: str = ""
    environment: str = "staging"
    total_tests: int = 0


@router.post("/api/v1/tests/runs")
async def create_test_run(req: CreateTestRunRequest):
    factory = require_db()
    async with factory() as db:
        row = TestRunRow(
            run_id=new_id(), tenant_id=req.tenant_id, suite_id=req.suite_id,
            environment=req.environment, total_tests=req.total_tests,
            status="pending", started_at=utc_now(),
        )
        db.add(row)
        await db.commit()
        return {"run_id": row.run_id, "status": "created"}
