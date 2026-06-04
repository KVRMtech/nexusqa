"""HTTP endpoints for the Test Factory (Phase 1 — demonstrated functional E2E).

* ``POST /api/v1/test-factory/{artifact_id}/generate``
    Generate demonstrated test cases from the artifact's Pages & Forms
    evidence and persist them.  Idempotent.
* ``GET  /api/v1/test-factory/{artifact_id}/summary``
    Small aggregate payload for the UI (counts only — never the full set).
* ``GET  /api/v1/test-factory/{artifact_id}/test-cases``
    Server-side paginated listing.
* ``GET  /api/v1/test-factory/{artifact_id}/export``
    Stream the generated suite as Excel/CSV/JSON for download or hand-off to a
    test-management tool, in the standard QA column format.

Additive router.  Reads frozen Pages & Forms data; writes only the additive
``factory_test_cases`` table.  Tenant isolation enforced via
``tenant_scoped_session`` (Postgres RLS).
"""

from __future__ import annotations

import io
import logging

from fastapi import APIRouter, Depends, HTTPException, Path as PathParam, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from nexus_sdk.db.models import CanonicalArtifactRow

from ..auth import get_current_user
from ..database import tenant_scoped_session
from ..services.test_factory import service as factory_service
from ..services.test_factory.delivery import (
    EXPORT_MEDIA_TYPES,
    build_csv,
    build_excel,
    build_json,
)

router = APIRouter(tags=["Test Factory"])
_logger = logging.getLogger(__name__)

_BUILDERS = {"excel": build_excel, "csv": build_csv, "json": build_json}
_EXTENSIONS = {"excel": "xlsx", "csv": "csv", "json": "json"}


async def _require_artifact(session, artifact_id: str, tenant_id: str) -> CanonicalArtifactRow:
    art = (
        await session.execute(
            select(CanonicalArtifactRow).where(
                CanonicalArtifactRow.artifact_id == artifact_id,
                CanonicalArtifactRow.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if art is None:
        raise HTTPException(status_code=404, detail="artifact not found")
    return art


@router.post("/api/v1/test-factory/{artifact_id}/generate")
async def generate_test_cases(
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        art = await _require_artifact(session, artifact_id, tenant_id)
        summary = await factory_service.generate_and_store(
            session,
            artifact_id=artifact_id,
            tenant_id=tenant_id,
            session_id=getattr(art, "session_id", "") or "",
        )
    return {"success": True, **summary}


@router.get("/api/v1/test-factory/{artifact_id}/summary")
async def get_summary(
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        return await factory_service.summarize(session, artifact_id=artifact_id)


@router.get("/api/v1/test-factory/{artifact_id}/test-cases")
async def list_test_cases(
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    page: int = Query(1, ge=1, description="1-based page number"),
    limit: int = Query(25, ge=1, le=200, description="Test cases per page"),
    status: str = Query("active", pattern="^(active|reserve)$"),
    user: dict = Depends(get_current_user),
):
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        return await factory_service.list_paginated(
            session, artifact_id=artifact_id, page=page, limit=limit, status=status,
        )


@router.get("/api/v1/test-factory/{artifact_id}/export")
async def export_test_cases(
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    format: str = Query("excel", description="excel | csv | json"),
    user: dict = Depends(get_current_user),
):
    fmt = (format or "excel").lower()
    if fmt not in _BUILDERS:
        raise HTTPException(
            status_code=422,
            detail=f"unsupported format '{format}' (use: {', '.join(_BUILDERS)})",
        )

    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        cases = await factory_service.load_active_production_cases(
            session, artifact_id=artifact_id,
        )

    if not cases:
        raise HTTPException(
            status_code=404,
            detail="no generated test cases for this artifact — run /generate first",
        )

    payload = _BUILDERS[fmt](cases)
    filename = f"nexus-testcases-{artifact_id[:8]}.{_EXTENSIONS[fmt]}"
    return StreamingResponse(
        io.BytesIO(payload),
        media_type=EXPORT_MEDIA_TYPES[fmt],
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
