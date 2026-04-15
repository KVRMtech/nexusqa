"""
Test Case CRUD & Export API — /api/v1/test-cases/*

Full production CRUD for test cases with:
  - Create / List / Get / Update / Delete
  - Nested steps, preconditions, data workbook
  - Multi-format export (Excel, CSV, JSON, HTML)
  - Filtering, pagination, bulk operations

All data persisted in PostgreSQL via async SQLAlchemy.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import structlog
from fastapi import APIRouter, HTTPException, Query, Path as FastAPIPath, Depends, Response
from pydantic import BaseModel, Field

from sqlalchemy import select, func, desc, and_, delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

# SDK imports
_sdk_path = os.path.join(os.path.dirname(__file__), "..", "..", "..", "sdk", "nexus-sdk")
if _sdk_path not in sys.path:
    sys.path.insert(0, _sdk_path)

from nexus_sdk.db.models import (
    TestCaseRow,
    TestCaseStepRow,
    TestCasePreconditionRow,
    DataWorkbookEntryRow,
    ExportJobRow,
)
from nexus_sdk.models import (
    ProductionTestCase,
    ProductionTestStep,
    DataWorkbookEntry,
    Precondition,
)
from nexus_sdk.testcase_id import generate_test_case_id
from nexus_sdk.export import ExportEngine, ExportFormat

logger = structlog.get_logger()

router = APIRouter(prefix="/api/v1/test-cases", tags=["Test Cases"])

# ─── Module-level state (set by init_router) ─────────────────

_session_factory: Optional[async_sessionmaker[AsyncSession]] = None
_export_engine: Optional[ExportEngine] = None
_export_dir: Path = Path(os.getenv("NEXUS_EXPORT_DIR", "/tmp/nexus-exports"))


def init_router(
    session_factory: async_sessionmaker[AsyncSession],
    export_dir: Optional[Path] = None,
) -> None:
    """Initialize the router with a database session factory.

    Called from main.py after DB is set up.
    """
    global _session_factory, _export_engine, _export_dir
    _session_factory = session_factory
    if export_dir:
        _export_dir = export_dir
    _export_engine = ExportEngine(default_output_dir=_export_dir)


def _require_db() -> async_sessionmaker[AsyncSession]:
    if not _session_factory:
        raise HTTPException(503, "Database not connected")
    return _session_factory


def _new_id() -> str:
    return str(uuid.uuid4())


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ─── Request / Response Models ────────────────────────────────

class StepInput(BaseModel):
    step_number: int = Field(..., ge=1)
    action: str = Field(..., min_length=1)
    expected_result: str = Field(default="")
    target_system: str = Field(default="web")
    target_element: str = Field(default="")
    input_data_refs: list[str] = Field(default_factory=list)
    verification: str = Field(default="")
    screenshot_required: bool = Field(default=False)


class PreconditionInput(BaseModel):
    description: str = Field(..., min_length=1)
    is_verified: bool = Field(default=False)


class DataEntryInput(BaseModel):
    field_name: str = Field(..., min_length=1)
    field_value: str = Field(default="")
    field_type: str = Field(default="string")
    is_sensitive: bool = Field(default=False)
    generator_hint: str = Field(default="")


class CreateTestCaseRequest(BaseModel):
    tenant_id: str = Field(..., min_length=1)
    title: str = Field(..., min_length=1, max_length=500)
    description: str = Field(default="")
    test_type: str = Field(default="e2e")
    priority: str = Field(default="medium")
    version: int = Field(default=1)
    target_systems: list[str] = Field(default_factory=list)
    validates_rules: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    suite_id: Optional[str] = Field(default=None)
    source_session_id: Optional[str] = Field(default=None)
    source_speaker_id: Optional[str] = Field(default=None)
    generated_by: str = Field(default="manual")
    steps: list[StepInput] = Field(default_factory=list)
    preconditions: list[PreconditionInput] = Field(default_factory=list)
    data_workbook: list[DataEntryInput] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class UpdateTestCaseRequest(BaseModel):
    title: Optional[str] = Field(default=None, max_length=500)
    description: Optional[str] = None
    test_type: Optional[str] = None
    priority: Optional[str] = None
    status: Optional[str] = None
    version: Optional[int] = None
    target_systems: Optional[list[str]] = None
    validates_rules: Optional[list[str]] = None
    tags: Optional[list[str]] = None
    suite_id: Optional[str] = None
    steps: Optional[list[StepInput]] = None
    preconditions: Optional[list[PreconditionInput]] = None
    data_workbook: Optional[list[DataEntryInput]] = None
    metadata: Optional[dict[str, Any]] = None


class ExportRequest(BaseModel):
    tenant_id: str = Field(..., min_length=1)
    format: str = Field(default="excel", description="excel, csv, json, html")
    test_case_ids: Optional[list[str]] = Field(
        default=None, description="Specific IDs to export; None = all for tenant",
    )
    test_type: Optional[str] = Field(default=None, description="Filter by test type")
    status: Optional[str] = Field(default=None, description="Filter by status")
    title: str = Field(default="Nexus QA — Test Cases")
    include_summary: bool = Field(default=True)


class BulkStatusRequest(BaseModel):
    test_case_ids: list[str] = Field(..., min_items=1)
    status: str = Field(..., description="draft, review, approved, deprecated")
    approved_by: Optional[str] = Field(default=None)


# ─── CRUD  Endpoints ─────────────────────────────────────────

@router.post("", status_code=201)
async def create_test_case(req: CreateTestCaseRequest):
    """
    Create a new test case with steps, preconditions, and data workbook.

    The test_case_id is auto-generated using the standard pattern:
      {PREFIX}-V{version:02d}-{seq:03d}
    """
    factory = _require_db()
    now = _utc_now()

    async with factory() as db:
        # Generate deterministic ID
        tc_id = await generate_test_case_id(
            session=db,
            tenant_id=req.tenant_id,
            test_type=req.test_type,
            version=req.version,
        )

        # Create main row
        tc_row = TestCaseRow(
            test_case_id=tc_id,
            tenant_id=req.tenant_id,
            suite_id=req.suite_id,
            title=req.title,
            description=req.description,
            test_type=req.test_type,
            priority=req.priority,
            status="draft",
            version=req.version,
            target_systems=req.target_systems,
            validates_rules=req.validates_rules,
            tags=req.tags,
            source_session_id=req.source_session_id,
            source_speaker_id=req.source_speaker_id,
            generated_by=req.generated_by,
            metadata_json=req.metadata,
            created_at=now,
            updated_at=now,
        )
        db.add(tc_row)

        # Steps
        for step in req.steps:
            db.add(TestCaseStepRow(
                step_id=_new_id(),
                test_case_id=tc_id,
                step_number=step.step_number,
                action=step.action,
                expected_result=step.expected_result,
                target_system=step.target_system,
                target_element=step.target_element,
                input_data_refs=step.input_data_refs,
                verification=step.verification,
                screenshot_required=step.screenshot_required,
            ))

        # Preconditions
        for i, pre in enumerate(req.preconditions):
            db.add(TestCasePreconditionRow(
                precondition_id=_new_id(),
                test_case_id=tc_id,
                sort_order=i,
                description=pre.description,
                is_verified=pre.is_verified,
            ))

        # Data workbook
        for i, entry in enumerate(req.data_workbook):
            db.add(DataWorkbookEntryRow(
                entry_id=_new_id(),
                test_case_id=tc_id,
                sort_order=i,
                field_name=entry.field_name,
                field_value=entry.field_value,
                field_type=entry.field_type,
                is_sensitive=entry.is_sensitive,
                generator_hint=entry.generator_hint,
            ))

        try:
            await db.commit()
        except IntegrityError as exc:
            await db.rollback()
            detail = str(exc.orig) if exc.orig else str(exc)
            if "tenant_id" in detail or "tenants" in detail:
                raise HTTPException(
                    400,
                    f"Tenant '{req.tenant_id}' does not exist. "
                    "Create the tenant first or use an existing tenant_id.",
                )
            if "suite_id" in detail or "test_suites" in detail:
                raise HTTPException(
                    400,
                    f"Suite '{req.suite_id}' does not exist.",
                )
            logger.error("test_case.create_integrity_error", error=detail)
            raise HTTPException(409, f"Integrity error: {detail}")

        logger.info(
            "test_case.created",
            test_case_id=tc_id,
            tenant=req.tenant_id,
            steps=len(req.steps),
            data_fields=len(req.data_workbook),
        )

        return {
            "test_case_id": tc_id,
            "status": "created",
            "steps": len(req.steps),
            "preconditions": len(req.preconditions),
            "data_workbook_entries": len(req.data_workbook),
        }


@router.get("")
async def list_test_cases(
    tenant_id: str = Query(..., min_length=1),
    test_type: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    priority: Optional[str] = Query(default=None),
    suite_id: Optional[str] = Query(default=None),
    tag: Optional[str] = Query(default=None, description="Filter by tag (contains)"),
    search: Optional[str] = Query(default=None, description="Search title/description"),
    sort_by: str = Query(default="created_at", description="Sort field"),
    sort_order: str = Query(default="desc", description="asc or desc"),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=500),
):
    """
    List test cases with filtering, pagination, and sorting.

    Returns summary view (without steps/data) for performance.
    """
    factory = _require_db()

    async with factory() as db:
        # Base query
        stmt = select(TestCaseRow).where(TestCaseRow.tenant_id == tenant_id)

        # Filters
        if test_type:
            stmt = stmt.where(TestCaseRow.test_type == test_type)
        if status:
            stmt = stmt.where(TestCaseRow.status == status)
        if priority:
            stmt = stmt.where(TestCaseRow.priority == priority)
        if suite_id:
            stmt = stmt.where(TestCaseRow.suite_id == suite_id)
        if search:
            like_term = f"%{search}%"
            stmt = stmt.where(
                TestCaseRow.title.ilike(like_term)
                | TestCaseRow.description.ilike(like_term)
            )

        # Count before pagination
        count_stmt = select(func.count()).select_from(stmt.subquery())
        total = await db.scalar(count_stmt) or 0

        # Sorting
        sort_col = getattr(TestCaseRow, sort_by, TestCaseRow.created_at)
        if sort_order == "asc":
            stmt = stmt.order_by(sort_col.asc())
        else:
            stmt = stmt.order_by(sort_col.desc())

        # Pagination
        stmt = stmt.offset(offset).limit(limit)

        result = await db.execute(stmt)
        rows = result.scalars().all()

        # Build summary response (no steps/data for list)
        items = []
        for row in rows:
            items.append({
                "test_case_id": row.test_case_id,
                "title": row.title,
                "test_type": row.test_type,
                "priority": row.priority,
                "status": row.status,
                "version": row.version,
                "tags": row.tags,
                "target_systems": row.target_systems,
                "generated_by": row.generated_by,
                "suite_id": row.suite_id,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            })

        return {
            "items": items,
            "total": total,
            "offset": offset,
            "limit": limit,
        }


# ─── Statistics Endpoint (must be before /{test_case_id}) ────

@router.get("/stats")
async def test_case_stats(
    tenant_id: str = Query(..., min_length=1),
):
    """Get aggregate test case statistics for a tenant."""
    factory = _require_db()

    async with factory() as db:
        # Total count
        total = await db.scalar(
            select(func.count()).select_from(TestCaseRow)
            .where(TestCaseRow.tenant_id == tenant_id)
        ) or 0

        # Count by status
        status_q = await db.execute(
            select(TestCaseRow.status, func.count())
            .where(TestCaseRow.tenant_id == tenant_id)
            .group_by(TestCaseRow.status)
        )
        by_status = {r[0]: r[1] for r in status_q.all()}

        # Count by type
        type_q = await db.execute(
            select(TestCaseRow.test_type, func.count())
            .where(TestCaseRow.tenant_id == tenant_id)
            .group_by(TestCaseRow.test_type)
        )
        by_type = {r[0]: r[1] for r in type_q.all()}

        # Count by priority
        prio_q = await db.execute(
            select(TestCaseRow.priority, func.count())
            .where(TestCaseRow.tenant_id == tenant_id)
            .group_by(TestCaseRow.priority)
        )
        by_priority = {r[0]: r[1] for r in prio_q.all()}

        # Total steps
        total_steps = await db.scalar(
            select(func.count()).select_from(TestCaseStepRow)
            .join(TestCaseRow)
            .where(TestCaseRow.tenant_id == tenant_id)
        ) or 0

        # Total data fields
        total_data = await db.scalar(
            select(func.count()).select_from(DataWorkbookEntryRow)
            .join(TestCaseRow)
            .where(TestCaseRow.tenant_id == tenant_id)
        ) or 0

        return {
            "total_test_cases": total,
            "total_steps": total_steps,
            "total_data_fields": total_data,
            "by_status": by_status,
            "by_type": by_type,
            "by_priority": by_priority,
        }


@router.get("/{test_case_id}")
async def get_test_case(
    test_case_id: str = FastAPIPath(..., min_length=1),
):
    """
    Get a single test case with all nested data:
    steps, preconditions, data workbook.
    """
    factory = _require_db()

    async with factory() as db:
        stmt = (
            select(TestCaseRow)
            .options(
                selectinload(TestCaseRow.steps),
                selectinload(TestCaseRow.preconditions),
                selectinload(TestCaseRow.data_workbook),
            )
            .where(TestCaseRow.test_case_id == test_case_id)
        )
        result = await db.execute(stmt)
        row = result.scalar_one_or_none()

        if not row:
            raise HTTPException(404, f"Test case {test_case_id} not found")

        return _tc_row_to_full_dict(row)


@router.put("/{test_case_id}")
async def update_test_case(
    req: UpdateTestCaseRequest,
    test_case_id: str = FastAPIPath(..., min_length=1),
):
    """
    Update a test case. Only provided fields are changed.

    If steps/preconditions/data_workbook are provided, they fully replace
    the existing children (delete + re-insert).
    """
    factory = _require_db()
    now = _utc_now()

    async with factory() as db:
        stmt = (
            select(TestCaseRow)
            .options(
                selectinload(TestCaseRow.steps),
                selectinload(TestCaseRow.preconditions),
                selectinload(TestCaseRow.data_workbook),
            )
            .where(TestCaseRow.test_case_id == test_case_id)
        )
        result = await db.execute(stmt)
        row = result.scalar_one_or_none()

        if not row:
            raise HTTPException(404, f"Test case {test_case_id} not found")

        # Update scalar fields
        if req.title is not None:
            row.title = req.title
        if req.description is not None:
            row.description = req.description
        if req.test_type is not None:
            row.test_type = req.test_type
        if req.priority is not None:
            row.priority = req.priority
        if req.status is not None:
            row.status = req.status
        if req.version is not None:
            row.version = req.version
        if req.target_systems is not None:
            row.target_systems = req.target_systems
        if req.validates_rules is not None:
            row.validates_rules = req.validates_rules
        if req.tags is not None:
            row.tags = req.tags
        if req.suite_id is not None:
            row.suite_id = req.suite_id
        if req.metadata is not None:
            row.metadata_json = req.metadata
        row.updated_at = now

        # Replace steps if provided
        if req.steps is not None:
            await db.execute(
                delete(TestCaseStepRow).where(
                    TestCaseStepRow.test_case_id == test_case_id
                )
            )
            for step in req.steps:
                db.add(TestCaseStepRow(
                    step_id=_new_id(),
                    test_case_id=test_case_id,
                    step_number=step.step_number,
                    action=step.action,
                    expected_result=step.expected_result,
                    target_system=step.target_system,
                    target_element=step.target_element,
                    input_data_refs=step.input_data_refs,
                    verification=step.verification,
                    screenshot_required=step.screenshot_required,
                ))

        # Replace preconditions if provided
        if req.preconditions is not None:
            await db.execute(
                delete(TestCasePreconditionRow).where(
                    TestCasePreconditionRow.test_case_id == test_case_id
                )
            )
            for i, pre in enumerate(req.preconditions):
                db.add(TestCasePreconditionRow(
                    precondition_id=_new_id(),
                    test_case_id=test_case_id,
                    sort_order=i,
                    description=pre.description,
                    is_verified=pre.is_verified,
                ))

        # Replace data workbook if provided
        if req.data_workbook is not None:
            await db.execute(
                delete(DataWorkbookEntryRow).where(
                    DataWorkbookEntryRow.test_case_id == test_case_id
                )
            )
            for i, entry in enumerate(req.data_workbook):
                db.add(DataWorkbookEntryRow(
                    entry_id=_new_id(),
                    test_case_id=test_case_id,
                    sort_order=i,
                    field_name=entry.field_name,
                    field_value=entry.field_value,
                    field_type=entry.field_type,
                    is_sensitive=entry.is_sensitive,
                    generator_hint=entry.generator_hint,
                ))

        await db.commit()

        # Re-fetch to return updated view
        result2 = await db.execute(
            select(TestCaseRow)
            .options(
                selectinload(TestCaseRow.steps),
                selectinload(TestCaseRow.preconditions),
                selectinload(TestCaseRow.data_workbook),
            )
            .where(TestCaseRow.test_case_id == test_case_id)
        )
        updated = result2.scalar_one()

        logger.info("test_case.updated", test_case_id=test_case_id)
        return _tc_row_to_full_dict(updated)


@router.delete("/{test_case_id}", status_code=204)
async def delete_test_case(
    test_case_id: str = FastAPIPath(..., min_length=1),
):
    """Delete a test case and all children (cascade)."""
    factory = _require_db()

    async with factory() as db:
        row = await db.get(TestCaseRow, test_case_id)
        if not row:
            raise HTTPException(404, f"Test case {test_case_id} not found")

        await db.delete(row)
        await db.commit()

        logger.info("test_case.deleted", test_case_id=test_case_id)
        return Response(status_code=204)


# ─── Bulk Operations ─────────────────────────────────────────

@router.post("/bulk/status")
async def bulk_update_status(req: BulkStatusRequest):
    """Update status of multiple test cases at once."""
    factory = _require_db()
    now = _utc_now()

    updated_ids: list[str] = []
    not_found_ids: list[str] = []

    async with factory() as db:
        for tc_id in req.test_case_ids:
            row = await db.get(TestCaseRow, tc_id)
            if not row:
                not_found_ids.append(tc_id)
                continue

            row.status = req.status
            row.updated_at = now
            if req.status == "approved" and req.approved_by:
                row.approved_by = req.approved_by
                row.approved_at = now
            updated_ids.append(tc_id)

        await db.commit()

    return {
        "updated": updated_ids,
        "not_found": not_found_ids,
        "updated_count": len(updated_ids),
    }


# ─── Export Endpoints ─────────────────────────────────────────

@router.post("/export")
async def export_test_cases(req: ExportRequest):
    """
    Export test cases to Excel, CSV, JSON, or HTML.

    Returns an export job with download path.
    """
    factory = _require_db()

    if not _export_engine:
        raise HTTPException(503, "Export engine not initialized")

    # Validate format
    try:
        fmt = ExportFormat(req.format.lower())
    except ValueError:
        raise HTTPException(400, f"Invalid format: {req.format}. Use: excel, csv, json, html")

    # Fetch test cases
    async with factory() as db:
        stmt = (
            select(TestCaseRow)
            .options(
                selectinload(TestCaseRow.steps),
                selectinload(TestCaseRow.preconditions),
                selectinload(TestCaseRow.data_workbook),
            )
            .where(TestCaseRow.tenant_id == req.tenant_id)
        )

        if req.test_case_ids:
            stmt = stmt.where(TestCaseRow.test_case_id.in_(req.test_case_ids))
        if req.test_type:
            stmt = stmt.where(TestCaseRow.test_type == req.test_type)
        if req.status:
            stmt = stmt.where(TestCaseRow.status == req.status)

        stmt = stmt.order_by(TestCaseRow.test_case_id)
        result = await db.execute(stmt)
        rows = result.scalars().all()

        if not rows:
            raise HTTPException(404, "No test cases found matching the criteria")

        # Convert ORM rows to Pydantic models
        pydantic_cases = [_row_to_pydantic(r) for r in rows]

        # Run export
        export_result = await _export_engine.export_test_cases(
            pydantic_cases,
            fmt=fmt,
            title=req.title,
            include_summary=req.include_summary,
        )

        if not export_result.success:
            raise HTTPException(500, f"Export failed: {export_result.error}")

        # Record export job in DB
        job_row = ExportJobRow(
            job_id=export_result.job_id,
            tenant_id=req.tenant_id,
            export_type=fmt.value,
            scope="test_case" if not req.test_case_ids else "selection",
            scope_id=",".join(req.test_case_ids) if req.test_case_ids else "",
            file_path=str(export_result.file_path),
            file_size_bytes=export_result.file_size_bytes,
            record_count=export_result.record_count,
            status="completed",
            created_by="api",
            created_at=_utc_now(),
            completed_at=_utc_now(),
        )
        db.add(job_row)
        await db.commit()

        return {
            "job_id": export_result.job_id,
            "format": fmt.value,
            "file_path": str(export_result.file_path),
            "file_size_bytes": export_result.file_size_bytes,
            "record_count": export_result.record_count,
            "step_count": export_result.step_count,
            "duration_ms": round(export_result.duration_ms, 1),
            "status": "completed",
        }


@router.post("/export/download")
async def export_download(req: ExportRequest):
    """
    Export test cases and return the file content directly as a download.

    Returns the file bytes with the appropriate content-type header.
    """
    factory = _require_db()

    if not _export_engine:
        raise HTTPException(503, "Export engine not initialized")

    try:
        fmt = ExportFormat(req.format.lower())
    except ValueError:
        raise HTTPException(400, f"Invalid format: {req.format}. Use: excel, csv, json, html")

    # Fetch test cases
    async with factory() as db:
        stmt = (
            select(TestCaseRow)
            .options(
                selectinload(TestCaseRow.steps),
                selectinload(TestCaseRow.preconditions),
                selectinload(TestCaseRow.data_workbook),
            )
            .where(TestCaseRow.tenant_id == req.tenant_id)
        )

        if req.test_case_ids:
            stmt = stmt.where(TestCaseRow.test_case_id.in_(req.test_case_ids))
        if req.test_type:
            stmt = stmt.where(TestCaseRow.test_type == req.test_type)
        if req.status:
            stmt = stmt.where(TestCaseRow.status == req.status)

        stmt = stmt.order_by(TestCaseRow.test_case_id)
        result = await db.execute(stmt)
        rows = result.scalars().all()

        if not rows:
            raise HTTPException(404, "No test cases found matching the criteria")

        pydantic_cases = [_row_to_pydantic(r) for r in rows]

    # Generate file bytes
    data, content_type = await _export_engine.export_to_bytes(
        pydantic_cases,
        fmt=fmt,
        title=req.title,
        include_summary=req.include_summary,
    )

    ext_map = {
        ExportFormat.EXCEL: "xlsx",
        ExportFormat.CSV: "csv",
        ExportFormat.JSON: "json",
        ExportFormat.HTML: "html",
    }
    filename = f"nexus-testcases.{ext_map[fmt]}"

    return Response(
        content=data,
        media_type=content_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/export/jobs")
async def list_export_jobs(
    tenant_id: str = Query(..., min_length=1),
    limit: int = Query(default=20, ge=1, le=100),
):
    """List recent export jobs for a tenant."""
    factory = _require_db()

    async with factory() as db:
        result = await db.execute(
            select(ExportJobRow)
            .where(ExportJobRow.tenant_id == tenant_id)
            .order_by(desc(ExportJobRow.created_at))
            .limit(limit)
        )
        rows = result.scalars().all()

        return [
            {
                "job_id": r.job_id,
                "export_type": r.export_type,
                "scope": r.scope,
                "file_path": r.file_path,
                "file_size_bytes": r.file_size_bytes,
                "record_count": r.record_count,
                "status": r.status,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "completed_at": r.completed_at.isoformat() if r.completed_at else None,
            }
            for r in rows
        ]


# ─── Helpers ──────────────────────────────────────────────────

def _tc_row_to_full_dict(row: TestCaseRow) -> dict[str, Any]:
    """Convert a TestCaseRow (with loaded relations) to a full dict."""
    return {
        "test_case_id": row.test_case_id,
        "tenant_id": row.tenant_id,
        "suite_id": row.suite_id,
        "title": row.title,
        "description": row.description,
        "test_type": row.test_type,
        "priority": row.priority,
        "status": row.status,
        "version": row.version,
        "target_systems": row.target_systems,
        "validates_rules": row.validates_rules,
        "tags": row.tags,
        "source_session_id": row.source_session_id,
        "source_speaker_id": row.source_speaker_id,
        "generated_by": row.generated_by,
        "approved_by": row.approved_by,
        "approved_at": row.approved_at.isoformat() if row.approved_at else None,
        "metadata": row.metadata_json,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "steps": [
            {
                "step_id": s.step_id,
                "step_number": s.step_number,
                "action": s.action,
                "expected_result": s.expected_result,
                "target_system": s.target_system,
                "target_element": s.target_element,
                "input_data_refs": s.input_data_refs,
                "verification": s.verification,
                "screenshot_required": s.screenshot_required,
            }
            for s in sorted(row.steps, key=lambda x: x.step_number)
        ],
        "preconditions": [
            {
                "precondition_id": p.precondition_id,
                "sort_order": p.sort_order,
                "description": p.description,
                "is_verified": p.is_verified,
            }
            for p in sorted(row.preconditions, key=lambda x: x.sort_order)
        ],
        "data_workbook": [
            {
                "entry_id": d.entry_id,
                "sort_order": d.sort_order,
                "field_name": d.field_name,
                "field_value": d.field_value,
                "field_type": d.field_type,
                "is_sensitive": d.is_sensitive,
                "generator_hint": d.generator_hint,
            }
            for d in sorted(row.data_workbook, key=lambda x: x.sort_order)
        ],
    }


def _row_to_pydantic(row: TestCaseRow) -> ProductionTestCase:
    """Convert a TestCaseRow to a Pydantic ProductionTestCase."""
    return ProductionTestCase(
        test_case_id=row.test_case_id,
        tenant_id=row.tenant_id,
        title=row.title,
        description=row.description or "",
        test_type=row.test_type or "e2e",
        priority=row.priority or "medium",
        status=row.status or "draft",
        version=row.version or 1,
        suite_id=row.suite_id,
        source_session_id=row.source_session_id,
        source_speaker_id=row.source_speaker_id,
        target_systems=row.target_systems or [],
        validates_rules=row.validates_rules or [],
        tags=row.tags or [],
        generated_by=row.generated_by or "system",
        approved_by=row.approved_by,
        approved_at=row.approved_at,
        created_at=row.created_at or _utc_now(),
        updated_at=row.updated_at,
        metadata=row.metadata_json or {},
        preconditions=[
            Precondition(
                description=p.description,
                is_verified=p.is_verified,
            )
            for p in sorted(row.preconditions, key=lambda x: x.sort_order)
        ],
        steps=[
            ProductionTestStep(
                step_number=s.step_number,
                action=s.action,
                expected_result=s.expected_result or "",
                target_system=s.target_system or "web",
                target_element=s.target_element or "",
                input_data_refs=s.input_data_refs or [],
                verification=s.verification or "",
                screenshot_required=s.screenshot_required or False,
            )
            for s in sorted(row.steps, key=lambda x: x.step_number)
        ],
        data_workbook=[
            DataWorkbookEntry(
                field_name=d.field_name,
                field_value=d.field_value or "",
                field_type=d.field_type or "string",
                is_sensitive=d.is_sensitive or False,
                generator_hint=d.generator_hint or "",
            )
            for d in sorted(row.data_workbook, key=lambda x: x.sort_order)
        ],
    )
