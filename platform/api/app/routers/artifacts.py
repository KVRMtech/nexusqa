"""
Platform API — Canonical Artifact & Workflow read-only routes.

Exposes canonical artifact state and workflow execution status
from PostgreSQL for the platform UI.  All endpoints are read-only.

P2: Runtime fallback — if a workflow is not yet in the DB (write-through
lag or failure), the platform API falls back to querying the orchestrator
directly for in-flight state.
"""
from __future__ import annotations

import os
import logging

from fastapi import APIRouter, HTTPException, Query, Path, Request
from sqlalchemy import select, desc, func

from nexus_sdk.db.models import CanonicalArtifactRow, WorkflowInstanceRow

from ..database import require_db, row_to_dict

router = APIRouter(tags=["Artifacts"])
_logger = logging.getLogger(__name__)

# P2: Orchestrator URL for runtime fallback
_ORCHESTRATOR_URL = os.environ.get("ORCHESTRATOR_URL", "http://localhost:8100")
_SPINE_ENGINE_URL = os.environ.get("SPINE_ENGINE_URL", "http://localhost:8009")


def _artifact_list_item(artifact: dict) -> dict:
    item = dict(artifact)
    item.pop("full_artifact_json", None)
    item.pop("safe_transcript_text", None)
    return item


async def _fetch_session_artifact_alias(session_id: str, tenant_id: str, request: Request) -> list[dict]:
    try:
        import httpx

        headers = {}
        auth_header = request.headers.get("authorization")
        if auth_header:
            headers["Authorization"] = auth_header

        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                f"{_SPINE_ENGINE_URL}/api/v1/spine/artifacts/{session_id}",
                headers=headers,
            )
            if resp.status_code != 200:
                return []
            payload = resp.json()
            artifact = payload.get("artifact") if isinstance(payload, dict) else None
            if not artifact or artifact.get("tenant_id") != tenant_id:
                return []
            _logger.info(
                "Session artifact alias resolved from Spine: session=%s artifact=%s",
                session_id,
                artifact.get("artifact_id"),
            )
            return [_artifact_list_item(artifact)]
    except Exception as exc:
        _logger.debug("Session artifact alias fallback failed for %s: %s", session_id, exc)
        return []


# ─── Canonical Artifacts ──────────────────────────────────────

@router.get("/api/v1/artifacts")
async def list_artifacts(
    tenant_id: str = Query(..., description="Tenant ID"),
    session_id: str | None = Query(None, description="Filter by session"),
    status: str | None = Query(None, description="Filter by status (pending, processing, completed, failed)"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """List canonical artifacts for a tenant, optionally filtered by session or status."""
    factory = require_db()
    async with factory() as db:
        q = (
            select(CanonicalArtifactRow)
            .where(CanonicalArtifactRow.tenant_id == tenant_id)
        )
        if session_id:
            q = q.where(CanonicalArtifactRow.session_id == session_id)
        if status:
            q = q.where(CanonicalArtifactRow.status == status)
        q = q.order_by(desc(CanonicalArtifactRow.created_at)).limit(limit).offset(offset)

        result = await db.execute(q)
        artifacts = []
        for row in result.scalars().all():
            d = row_to_dict(row)
            # Exclude large blob from list view
            d.pop("full_artifact_json", None)
            d.pop("safe_transcript_text", None)
            artifacts.append(d)
        return artifacts


@router.get("/api/v1/artifacts/{artifact_id}")
async def get_artifact(artifact_id: str = Path(...)):
    """Fetch a single canonical artifact with full detail."""
    factory = require_db()
    async with factory() as db:
        row = await db.get(CanonicalArtifactRow, artifact_id)
        if not row:
            raise HTTPException(404, f"Artifact {artifact_id} not found")
        return row_to_dict(row)


@router.get("/api/v1/artifacts/{artifact_id}/status")
async def get_artifact_status(artifact_id: str = Path(...)):
    """Get processing status for a canonical artifact.

    Phase 1.4: This is the official completion signal endpoint.
    Downstream consumers and UI poll this to determine readiness.
    """
    factory = require_db()
    async with factory() as db:
        row = await db.get(CanonicalArtifactRow, artifact_id)
        if not row:
            raise HTTPException(404, f"Artifact {artifact_id} not found")
        return {
            "artifact_id": row.artifact_id,
            "workflow_id": getattr(row, "workflow_id", None),
            "session_id": row.session_id,
            "tenant_id": row.tenant_id,
            "status": row.status,
            "quality_gate_passed": row.quality_gate_passed,
            "quality_gate_outcome": getattr(row, "quality_gate_outcome", None),
            "brain_quality_score": row.brain_quality_score,
            "has_real_transcript": getattr(row, "has_real_transcript", False),
            "has_visual_semantics": getattr(row, "has_visual_semantics", False),
            "semantic_completeness_score": getattr(row, "semantic_completeness_score", None),
            "review_reasons": (getattr(row, "full_artifact_json", None) or {}).get("review_reasons", []),
            "model_provenance": (getattr(row, "full_artifact_json", None) or {}).get("model_provenance", {}),
            "source_type": getattr(row, "source_type", None),
            "source_filename": getattr(row, "source_filename", None),
            "processing_time_seconds": row.processing_time_seconds,
            "created_at": row.created_at,
            "completed_at": row.completed_at,
            "error": row.error,
        }


@router.get("/api/v1/artifacts/{artifact_id}/transcript")
async def get_artifact_transcript(artifact_id: str = Path(...)):
    """Get the PII-safe transcript text for a canonical artifact."""
    factory = require_db()
    async with factory() as db:
        row = await db.get(CanonicalArtifactRow, artifact_id)
        if not row:
            raise HTTPException(404, f"Artifact {artifact_id} not found")
        return {
            "artifact_id": row.artifact_id,
            "session_id": row.session_id,
            "safe_transcript_text": row.safe_transcript_text or "",
        }


# ─── Session → Artifacts ─────────────────────────────────────

@router.get("/api/v1/sessions/{session_id}/artifacts")
async def list_session_artifacts(
    session_id: str = Path(...),
    tenant_id: str = Query(..., description="Tenant ID"),
    request: Request = None,
):
    """List all canonical artifacts for a given session."""
    factory = require_db()
    async with factory() as db:
        result = await db.execute(
            select(CanonicalArtifactRow)
            .where(
                CanonicalArtifactRow.session_id == session_id,
                CanonicalArtifactRow.tenant_id == tenant_id,
            )
            .order_by(desc(CanonicalArtifactRow.created_at))
        )
        artifacts = [_artifact_list_item(row_to_dict(r)) for r in result.scalars().all()]
        if artifacts:
            return artifacts

    if request is not None:
        aliased = await _fetch_session_artifact_alias(session_id, tenant_id, request)
        if aliased:
            return aliased

    return []


# ─── Session → Workflows ─────────────────────────────────────

@router.get("/api/v1/sessions/{session_id}/workflows")
async def list_session_workflows(
    session_id: str = Path(...),
    tenant_id: str = Query(..., description="Tenant ID"),
    request: Request = None,
):
    """List all workflow executions for a given session.

    Falls back to orchestrator if DB returns empty (write-through
    may not have reached the read model yet).
    """
    factory = require_db()
    async with factory() as db:
        result = await db.execute(
            select(WorkflowInstanceRow)
            .where(
                WorkflowInstanceRow.session_id == session_id,
                WorkflowInstanceRow.tenant_id == tenant_id,
            )
            .order_by(desc(WorkflowInstanceRow.started_at))
        )
        workflows = []
        for row in result.scalars().all():
            d = row_to_dict(row)
            # Exclude large stage detail from list view — available on detail endpoint
            d.pop("input_data", None)
            workflows.append(d)
        if workflows:
            return workflows

    # Fallback: query orchestrator for session workflows not yet in DB
    try:
        import httpx as _httpx
        _headers = {}
        if request:
            _auth = request.headers.get("authorization")
            if _auth:
                _headers["Authorization"] = _auth
        async with _httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                f"{_ORCHESTRATOR_URL}/api/v1/orchestrator/workflows",
                params={"session_id": session_id},
                headers=_headers,
            )
            if resp.status_code == 200:
                data = resp.json()
                if data:
                    _logger.info(
                        "P3 fallback: session %s workflows served from orchestrator",
                        session_id,
                    )
                    return data
    except Exception as exc:
        _logger.debug("P3 session workflows fallback failed: %s", exc)

    return []


@router.get("/api/v1/workflows/{workflow_id}")
async def get_workflow(
    workflow_id: str = Path(...),
    request: Request = None,
):
    """Fetch a single workflow execution with full stage detail.

    P2: Falls back to querying the orchestrator directly if the
    workflow is not yet persisted to the read model (DB).
    """
    factory = require_db()
    async with factory() as db:
        row = await db.get(WorkflowInstanceRow, workflow_id)
        if row:
            return row_to_dict(row)

    # P2: Runtime fallback — query orchestrator for in-flight workflow
    try:
        import httpx
        _headers = {}
        if request:
            _auth = request.headers.get("authorization")
            if _auth:
                _headers["Authorization"] = _auth
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                f"{_ORCHESTRATOR_URL}/api/v1/orchestrator/workflows/{workflow_id}",
                headers=_headers,
            )
            if resp.status_code == 200:
                _logger.info(
                    "P2 fallback: workflow %s served from orchestrator (not yet in DB)",
                    workflow_id,
                )
                return resp.json()
    except Exception as exc:
        _logger.debug("P2 fallback failed for workflow %s: %s", workflow_id, exc)

    raise HTTPException(404, f"Workflow {workflow_id} not found")


@router.get("/api/v1/workflows/{workflow_id}/timeline")
async def get_workflow_timeline(
    workflow_id: str = Path(...),
    request: Request = None,
):
    """Get the execution timeline for a workflow.

    Falls back to orchestrator if workflow not yet in DB.
    """
    factory = require_db()
    async with factory() as db:
        row = await db.get(WorkflowInstanceRow, workflow_id)
        if row:
            return {
                "workflow_id": row.workflow_id,
                "chain_name": row.chain_name,
                "status": row.status,
                "timeline": row.timeline or [],
                "started_at": row.started_at,
                "completed_at": row.completed_at,
            }

    # Fallback: query orchestrator for in-flight workflow timeline
    try:
        import httpx as _httpx
        _headers = {}
        if request:
            _auth = request.headers.get("authorization")
            if _auth:
                _headers["Authorization"] = _auth
        async with _httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                f"{_ORCHESTRATOR_URL}/api/v1/orchestrator/workflows/{workflow_id}",
                headers=_headers,
            )
            if resp.status_code == 200:
                data = resp.json()
                _logger.info(
                    "P3 fallback: timeline for %s served from orchestrator",
                    workflow_id,
                )
                return {
                    "workflow_id": data.get("workflow_id"),
                    "chain_name": data.get("chain_name"),
                    "status": data.get("status"),
                    "timeline": data.get("timeline", []),
                    "started_at": data.get("started_at"),
                    "completed_at": data.get("completed_at"),
                }
    except Exception as exc:
        _logger.debug("P3 timeline fallback failed for %s: %s", workflow_id, exc)

    raise HTTPException(404, f"Workflow {workflow_id} not found")
