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
import json
import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Path as PathParam, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select, text

from nexus_sdk.db.models import CanonicalArtifactRow
from nexus_sdk.security.envelope import EnvelopeBlob

from ..auth import get_current_user
from ..database import tenant_scoped_session
from .integrations import integration_installations
from ..services.test_factory import service as factory_service
from ..services.test_factory.options_extractor import (
    extract_field_options_for_artifact,
)
from ..services.test_factory.delivery import (
    EXPORT_MEDIA_TYPES,
    build_csv,
    build_excel,
    build_json,
)
from ..services.test_factory.delivery.connectors import CONNECTORS, build_connector

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


def _bearer(request: Request) -> str:
    raw = request.headers.get("authorization") or ""
    return raw[7:].strip() if raw.lower().startswith("bearer ") else raw.strip()


@router.post("/api/v1/test-factory/{artifact_id}/capture-options")
async def capture_options(
    request: Request,
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    """Capture available field options (vision) into form_snapshot_signals, then
    regenerate so grounded combinations materialise.

    Uses the shared LLM router (from the storyboard composer).  Costs one vision
    call per page; only options actually visible in frames are captured.
    """
    tenant_id = user["tenant_id"]
    composer = getattr(request.app.state, "storyboard_composer", None)
    llm_router = getattr(composer, "_llm_router", None) if composer else None
    if llm_router is None:
        raise HTTPException(status_code=503, detail="LLM router unavailable")
    token = _bearer(request)

    async with tenant_scoped_session(tenant_id) as session:
        art = await _require_artifact(session, artifact_id, tenant_id)
        options = await extract_field_options_for_artifact(
            session, artifact_id=artifact_id, tenant_id=tenant_id,
            router=llm_router, auth_token=token,
        )
        await session.commit()
        # commit reset the transaction-local RLS var — re-arm before regenerate.
        await session.execute(
            text("SELECT set_config('nexus.current_tenant_id', :tid, true)"),
            {"tid": str(tenant_id)},
        )
        regenerated = await factory_service.generate_and_store(
            session, artifact_id=artifact_id, tenant_id=tenant_id,
            session_id=getattr(art, "session_id", "") or "",
        )
    return {"success": True, "options_capture": options, "regenerated": regenerated}


_CATEGORIES = {"negative", "boundary", "error_state"}


@router.post("/api/v1/test-factory/{artifact_id}/generate/{category}")
async def generate_category_endpoint(
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    category: str = PathParam(..., min_length=1, max_length=40),
    user: dict = Depends(get_current_user),
):
    """On-demand generate a non-demonstrated category (negative|boundary|error_state)."""
    if category not in _CATEGORIES:
        raise HTTPException(
            status_code=422,
            detail=f"unknown category '{category}' (use: {', '.join(sorted(_CATEGORIES))})",
        )
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        art = await _require_artifact(session, artifact_id, tenant_id)
        result = await factory_service.generate_category(
            session, artifact_id=artifact_id, tenant_id=tenant_id,
            category=category, session_id=getattr(art, "session_id", "") or "",
        )
    return {"success": True, **result}


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
    type: str | None = Query(None, description="filter by category/test_type"),
    user: dict = Depends(get_current_user),
):
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        return await factory_service.list_paginated(
            session, artifact_id=artifact_id, page=page, limit=limit,
            status=status, test_type=type,
        )


@router.get("/api/v1/test-factory/{artifact_id}/reserve")
async def get_reserve(
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
):
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        reserve = await factory_service.get_reserve(session, artifact_id=artifact_id)
    if reserve is None:
        raise HTTPException(
            status_code=404,
            detail="no combination reserve for this artifact — run /generate first",
        )
    return reserve


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


@router.post("/api/v1/test-factory/{artifact_id}/push/{tool}")
async def push_to_tm_tool(
    request: Request,
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    tool: str = PathParam(..., min_length=1, max_length=40),
    user: dict = Depends(get_current_user),
):
    """Push the active test suite into a test-management tool.

    Credentials are decrypted at call time from the tenant's connected
    ``integration_installations`` row (envelope-encrypted) — never hardcoded.
    """
    tool = tool.lower()
    if tool not in CONNECTORS:
        raise HTTPException(
            status_code=422,
            detail=f"unsupported tool '{tool}' (supported: {', '.join(CONNECTORS)})",
        )
    envelope = getattr(request.app.state, "envelope_service", None)
    if envelope is None:
        raise HTTPException(status_code=503, detail="envelope_service unavailable")

    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        await _require_artifact(session, artifact_id, tenant_id)
        inst = (
            await session.execute(
                select(integration_installations).where(
                    integration_installations.c.tenant_id == tenant_id,
                    integration_installations.c.integration_id == tool,
                    integration_installations.c.status == "connected",
                )
            )
        ).mappings().first()
        if inst is None:
            raise HTTPException(
                status_code=409,
                detail=f"no connected '{tool}' integration for this tenant — install it first",
            )
        cases = await factory_service.load_active_production_cases(
            session, artifact_id=artifact_id,
        )

    if not cases:
        raise HTTPException(
            status_code=404,
            detail="no generated test cases for this artifact — run /generate first",
        )

    blob_bytes = inst.get("encrypted_credentials")
    if not blob_bytes:
        raise HTTPException(
            status_code=409, detail=f"'{tool}' integration has no stored credentials",
        )
    try:
        plaintext = await envelope.decrypt(
            tenant_id,
            EnvelopeBlob.from_bytes(bytes(blob_bytes)),
            expected_aad=tool.encode("utf-8"),
        )
        credentials = json.loads(plaintext.decode("utf-8"))
    except Exception as exc:
        _logger.warning("test_factory.push.credential_decrypt_failed tool=%s err=%s", tool, exc)
        raise HTTPException(status_code=502, detail=f"could not decrypt '{tool}' credentials")

    try:
        connector = build_connector(tool, credentials, dict(inst.get("config") or {}))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    async with httpx.AsyncClient(timeout=60.0) as http:
        result = await connector.push(cases, http)

    return {"success": result.failed == 0, **result.as_dict()}
