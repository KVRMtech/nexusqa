"""
Platform API — Test Suite & Run routes (Module 8).
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select, desc

from nexus_sdk.db.models import TestSuiteRow, TestRunRow

from ..database import require_db, new_id, utc_now, row_to_dict
from ..auth import get_current_user
from ..config import PlatformAPIConfig
from ..services.test_exporters import (
    REGISTRY as EXPORTER_REGISTRY,
    prepare_export_context,
)

router = APIRouter(tags=["Tests"])
_logger = logging.getLogger(__name__)
_config = PlatformAPIConfig()


@router.get("/api/v1/tests/suites")
async def list_test_suites(user: dict = Depends(get_current_user)):
    tenant_id = user["tenant_id"]
    factory = require_db()
    async with factory() as db:
        result = await db.execute(
            select(TestSuiteRow)
            .where(TestSuiteRow.tenant_id == tenant_id)
            .order_by(desc(TestSuiteRow.created_at))
        )
        return [row_to_dict(r) for r in result.scalars().all()]


@router.get("/api/v1/tests/runs")
async def list_test_runs(user: dict = Depends(get_current_user)):
    tenant_id = user["tenant_id"]
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
    name: str
    description: str = ""
    tags: list[str] = []


@router.post("/api/v1/tests/suites")
async def create_test_suite(
    req: CreateTestSuiteRequest,
    user: dict = Depends(get_current_user),
):
    tenant_id = user["tenant_id"]
    factory = require_db()
    async with factory() as db:
        row = TestSuiteRow(
            suite_id=new_id(), tenant_id=tenant_id, name=req.name,
            description=req.description, tags=req.tags,
            status="active", created_at=utc_now(),
        )
        db.add(row)
        await db.commit()
        return {"suite_id": row.suite_id, "status": "created"}


class CreateTestRunRequest(BaseModel):
    suite_id: str = ""
    environment: str = "staging"
    total_tests: int = 0


@router.post("/api/v1/tests/runs")
async def create_test_run(
    req: CreateTestRunRequest,
    user: dict = Depends(get_current_user),
):
    tenant_id = user["tenant_id"]
    factory = require_db()
    async with factory() as db:
        row = TestRunRow(
            run_id=new_id(), tenant_id=tenant_id, suite_id=req.suite_id,
            environment=req.environment, total_tests=req.total_tests,
            status="pending", started_at=utc_now(),
        )
        db.add(row)
        await db.commit()
        return {"run_id": row.run_id, "status": "created"}


# ─── Test Export (P5 — multi-format) ─────────────────────────


@router.get("/api/v1/tests/export-formats")
async def list_export_formats(user: dict = Depends(get_current_user)):
    """List all supported export formats — used by the frontend dropdown."""
    _ = user  # auth required, no per-tenant logic
    return {
        "formats": [
            {
                "id": exporter_cls.format_id,
                "label": exporter_cls.label,
                "file_extension": exporter_cls.file_extension,
            }
            for exporter_cls in EXPORTER_REGISTRY.values()
        ]
    }


async def _run_export(
    *, artifact_id: str, tenant_id: str, format_id: str, enforce_gates: bool,
) -> StreamingResponse:
    """Shared export pipeline used by all format-specific endpoints."""
    exporter_cls = EXPORTER_REGISTRY.get(format_id)
    if exporter_cls is None:
        raise HTTPException(422, detail={
            "error": "unknown_format",
            "message": f"Unknown export format {format_id!r}. Available: {sorted(EXPORTER_REGISTRY)}",
        })

    ctx = await prepare_export_context(
        artifact_id=artifact_id,
        tenant_id=tenant_id,
        enforce_automation_gates=enforce_gates,
    )
    bundle = exporter_cls(ctx).render()

    import io as _io
    return StreamingResponse(
        _io.BytesIO(bundle.payload),
        media_type=bundle.content_type,
        headers={"Content-Disposition": f"attachment; filename={bundle.filename}"},
    )


@router.post("/api/v1/tests/export")
async def export_tests(
    artifact_id: str = Query(..., min_length=1),
    format: str = Query("playwright", description="Export format id (playwright | cypress | gherkin | json)"),
    enforce_gates: bool = Query(
        True,
        description=(
            "When True (default), every actionable step must pass the 5-gate "
            "automation readiness check. Set False for descriptive formats "
            "(gherkin / json) where unproven steps are still useful."
        ),
    ),
    user: dict = Depends(get_current_user),
):
    """Generic multi-format test export.

    Delegates to the exporter registered for ``format``. Every format
    shares the same preparation pipeline: graph fetch → Heart test cases →
    P3 lifecycle gate (approved/automated/live/stable only) → optional
    5-gate automation readiness → app_instance grouping.
    """
    return await _run_export(
        artifact_id=artifact_id,
        tenant_id=user["tenant_id"],
        format_id=format,
        enforce_gates=enforce_gates,
    )


@router.post("/api/v1/tests/export-playwright")
async def export_playwright(
    artifact_id: str,
    user: dict = Depends(get_current_user),
):
    """Back-compat shim — same as ``/export?format=playwright``."""
    return await _run_export(
        artifact_id=artifact_id,
        tenant_id=user["tenant_id"],
        format_id="playwright",
        enforce_gates=True,
    )
