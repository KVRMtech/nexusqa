"""
Platform API — Canonical Artifact & Workflow read-only routes.

Exposes canonical artifact state and workflow execution status
from PostgreSQL for the platform UI.  All endpoints are read-only.
All endpoints require JWT authentication and enforce tenant isolation.

P2: Runtime fallback — if a workflow is not yet in the DB (write-through
lag or failure), the platform API falls back to querying the orchestrator
directly for in-flight state.
"""
from __future__ import annotations

import asyncio
import json
import os
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Path, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select, desc, func, asc

from nexus_sdk.db.models import (
    CanonicalArtifactRow, WorkflowInstanceRow, VisualFrameRow,
    VisualSceneRow, AppInstanceRow, EvidenceControlRow, VisualFlowEdgeRow,
    VisualFlowRow, EvidenceStepRow, CursorEventRow,
    # Phase 1.7 — storyboard overlays optionally appended to the
    # visual-evidence-graph response.  Read-only here; populated by
    # the storyboard composer.
    StoryboardPanelRow, AppGroupingRow, SceneCaptionRow,
    AnnotatedFrameCacheRow, SceneActionRow,
    # Phase 2 — URL-keyed page visits + their actions and form
    # snapshots.  Read-only here; populated by the page_visit /
    # page_action / form_snapshot extractors.
    PageVisitRow, PageActionRow,
)


def _compute_visual_e2e_status(
    scene_count: int,
    edge_count: int,
    flow_count: int,
    strong_scene_pct: float,
    strong_action_pct: float,
    avg_completeness: float,
) -> str:
    """Return one of: not_ready | preview_only | ready | strong_ready.

    The thresholds encode a generic quality contract that applies to any upload:

    not_ready:
      - < 2 scenes  OR  0 edges  -> cannot render a meaningful journey at all.

    preview_only:
      - 2+ scenes and 1+ edges present but below 'ready' thresholds.
      - Page can render chronology; semantics may be weak or absent.
      - UI should show explicit degraded badges.

    ready  (backs visual_e2e_ready=True):
      - >= 3 scenes, >= 2 edges, 1+ flows
      - strong/degraded scene coverage >= 40%
      - avg_completeness >= 0.55
      - Minimum quality for launching the Visual E2E page and test-gen.

    strong_ready:
      - >= 4 scenes, >= scene_count-1 edges (chronological continuity)
      - strong/degraded scene coverage >= 70%
      - strong/degraded action coverage >= 50%
      - avg_completeness >= 0.75
      - Suitable for customer export / 'High quality flow' badge.
    """
    if scene_count < 2 or edge_count < 1:
        return "not_ready"
    if scene_count >= 4 and edge_count >= max(scene_count - 1, 2) and flow_count >= 1 \
            and strong_scene_pct >= 0.70 and strong_action_pct >= 0.50 \
            and avg_completeness >= 0.75:
        return "strong_ready"
    if scene_count >= 3 and edge_count >= 2 and flow_count >= 1 \
            and strong_scene_pct >= 0.40 and avg_completeness >= 0.55:
        return "ready"
    return "preview_only"

from ..auth import get_current_user
from ..database import (
    require_db,
    row_to_dict,
    safe_frame_asset_path,
    tenant_scoped_session,
)

# Hard ceiling on how many scenes / controls / steps / events a single
# visual-evidence response may return.  Long demos (3-hour, multi-app)
# easily exceed 5K controls; without a cap the response balloons past
# the 5 MB browser-JSON threshold and the API blocks the event loop
# serialising it.  Clients page via ?page= and ?limit= (same convention
# as ``GET /artifacts/{id}/frames``).
_VISUAL_EVIDENCE_PAGE_DEFAULT = 500
_VISUAL_EVIDENCE_PAGE_MAX = 2000

router = APIRouter(tags=["Artifacts"])
_logger = logging.getLogger(__name__)

# P2: Orchestrator URL for runtime fallback. The docker-compose env
# var is NEXUS_ORCHESTRATOR_URL (namespaced); accept both for backwards
# compatibility with older deployments / tests.
_ORCHESTRATOR_URL = (
    os.environ.get("NEXUS_ORCHESTRATOR_URL")
    or os.environ.get("ORCHESTRATOR_URL")
    or "http://localhost:8100"
)
_SPINE_ENGINE_URL = (
    os.environ.get("NEXUS_SPINE_ENGINE_URL")
    or os.environ.get("SPINE_ENGINE_URL")
    or "http://localhost:8009"
)


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
    session_id: str | None = Query(None, description="Filter by session"),
    status: str | None = Query(None, description="Filter by status (pending, processing, completed, failed)"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: dict = Depends(get_current_user),
):
    tenant_id = user["tenant_id"]
    """List canonical artifacts for a tenant, optionally filtered by session or status."""
    async with tenant_scoped_session(tenant_id) as db:
        q = (
            select(CanonicalArtifactRow)
            .where(CanonicalArtifactRow.tenant_id == tenant_id)  # bound to auth
        )
        if session_id:
            q = q.where(CanonicalArtifactRow.session_id == session_id)
        if status:
            q = q.where(CanonicalArtifactRow.status == status)
        q = q.order_by(desc(CanonicalArtifactRow.created_at)).limit(limit).offset(offset)

        result = await db.execute(q)
        rows = result.scalars().all()
        artifact_ids = [r.artifact_id for r in rows]

        # Bulk-fetch all per-artifact quality metrics in four queries (no N+1).
        scene_counts: dict[str, int] = {}
        strong_scene_counts: dict[str, int] = {}
        edge_counts: dict[str, int] = {}
        strong_edge_counts: dict[str, int] = {}
        flow_counts: dict[str, int] = {}
        if artifact_ids:
            sc_q = await db.execute(
                select(VisualSceneRow.artifact_id, func.count().label("cnt"))
                .where(
                    VisualSceneRow.artifact_id.in_(artifact_ids),
                    VisualSceneRow.tenant_id == tenant_id,
                )
                .group_by(VisualSceneRow.artifact_id)
            )
            scene_counts = {row.artifact_id: row.cnt for row in sc_q.all()}

            ss_q = await db.execute(
                select(VisualSceneRow.artifact_id, func.count().label("cnt"))
                .where(
                    VisualSceneRow.artifact_id.in_(artifact_ids),
                    VisualSceneRow.tenant_id == tenant_id,
                    VisualSceneRow.scene_quality.in_(("strong", "degraded")),
                )
                .group_by(VisualSceneRow.artifact_id)
            )
            strong_scene_counts = {row.artifact_id: row.cnt for row in ss_q.all()}

            ec_q = await db.execute(
                select(VisualFlowEdgeRow.artifact_id, func.count().label("cnt"))
                .where(
                    VisualFlowEdgeRow.artifact_id.in_(artifact_ids),
                    VisualFlowEdgeRow.tenant_id == tenant_id,
                )
                .group_by(VisualFlowEdgeRow.artifact_id)
            )
            edge_counts = {row.artifact_id: row.cnt for row in ec_q.all()}

            se_q = await db.execute(
                select(VisualFlowEdgeRow.artifact_id, func.count().label("cnt"))
                .where(
                    VisualFlowEdgeRow.artifact_id.in_(artifact_ids),
                    VisualFlowEdgeRow.tenant_id == tenant_id,
                    VisualFlowEdgeRow.action_quality.in_(("strong", "degraded")),
                )
                .group_by(VisualFlowEdgeRow.artifact_id)
            )
            strong_edge_counts = {row.artifact_id: row.cnt for row in se_q.all()}

            fl_q = await db.execute(
                select(VisualFlowRow.artifact_id, func.count().label("cnt"))
                .where(
                    VisualFlowRow.artifact_id.in_(artifact_ids),
                    VisualFlowRow.tenant_id == tenant_id,
                    VisualFlowRow.is_noise.is_(False),
                )
                .group_by(VisualFlowRow.artifact_id)
            )
            flow_counts = {row.artifact_id: row.cnt for row in fl_q.all()}

        artifacts = []
        for row in rows:
            d = row_to_dict(row)
            # Exclude large blobs from list view
            d.pop("full_artifact_json", None)
            d.pop("safe_transcript_text", None)
            aid = d["artifact_id"]
            sc = scene_counts.get(aid, 0)
            ec = edge_counts.get(aid, 0)
            fc = flow_counts.get(aid, 0)
            strong_sc = strong_scene_counts.get(aid, 0)
            strong_ec = strong_edge_counts.get(aid, 0)
            strong_scene_pct = strong_sc / sc if sc else 0.0
            strong_action_pct = strong_ec / ec if ec else 0.0
            avg_completeness = float(getattr(row, "semantic_completeness_score", None) or 0.55)
            # visual_e2e_status drives the multi-level readiness contract.
            # visual_e2e_ready remains True for any status >= ready so that
            # existing consumers (UI launch gating, orchestrator checks) are unaffected.
            ve2e_status = _compute_visual_e2e_status(
                scene_count=sc,
                edge_count=ec,
                flow_count=fc,
                strong_scene_pct=strong_scene_pct,
                strong_action_pct=strong_action_pct,
                avg_completeness=avg_completeness,
            )
            d["visual_e2e_status"] = ve2e_status
            d["visual_e2e_ready"] = ve2e_status in ("ready", "strong_ready")
            d["visual_scene_count_persisted"] = sc
            artifacts.append(d)
        return artifacts


@router.get("/api/v1/artifacts/{artifact_id}")
async def get_artifact(
    artifact_id: str = Path(...),
    user: dict = Depends(get_current_user),
):
    """Fetch a single canonical artifact with full detail."""
    tenant_id = user["tenant_id"]
    factory = require_db()
    async with factory() as db:
        row = await db.get(CanonicalArtifactRow, artifact_id)
        if not row or row.tenant_id != tenant_id:
            raise HTTPException(404, f"Artifact {artifact_id} not found")
        return row_to_dict(row)


@router.get("/api/v1/artifacts/{artifact_id}/status")
async def get_artifact_status(
    artifact_id: str = Path(...),
    user: dict = Depends(get_current_user),
):
    """Get processing status for a canonical artifact.

    Phase 1.4: This is the official completion signal endpoint.
    Downstream consumers and UI poll this to determine readiness.
    """
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as db:
        row = await db.get(CanonicalArtifactRow, artifact_id)
        if not row or row.tenant_id != tenant_id:
            raise HTTPException(404, f"Artifact {artifact_id} not found")
        # Check whether visual graph substrate is actually persisted
        # (has_visual_semantics can be true while visual_scenes = 0 if
        # persist_visual_evidence was skipped due to ordering failure)
        # Fetch scene count + quality coverage in a single pass
        scene_rows_res = await db.execute(
            select(VisualSceneRow.scene_quality)
            .where(
                VisualSceneRow.artifact_id == artifact_id,
                VisualSceneRow.tenant_id == tenant_id,
            )
        )
        scene_qualities = [r[0] for r in scene_rows_res.all()]
        persisted_scene_count = len(scene_qualities)
        strong_scene_count = sum(1 for q in scene_qualities if q in ("strong", "degraded"))
        strong_scene_pct = strong_scene_count / persisted_scene_count if persisted_scene_count else 0.0

        # Edge count + action quality coverage
        edge_rows_res = await db.execute(
            select(VisualFlowEdgeRow.action_quality)
            .where(
                VisualFlowEdgeRow.artifact_id == artifact_id,
                VisualFlowEdgeRow.tenant_id == tenant_id,
            )
        )
        edge_qualities = [r[0] for r in edge_rows_res.all()]
        persisted_edge_count = len(edge_qualities)
        strong_action_count = sum(1 for q in edge_qualities if q in ("strong", "degraded"))
        strong_action_pct = strong_action_count / persisted_edge_count if persisted_edge_count else 0.0

        # Flow count
        flow_count_res = await db.execute(
            select(func.count()).where(
                VisualFlowRow.artifact_id == artifact_id,
                VisualFlowRow.tenant_id == tenant_id,
                VisualFlowRow.is_noise.is_(False),
            )
        )
        persisted_flow_count: int = flow_count_res.scalar() or 0

        avg_completeness = float(getattr(row, "semantic_completeness_score", None) or 0.55)

        visual_e2e_status = _compute_visual_e2e_status(
            scene_count=persisted_scene_count,
            edge_count=persisted_edge_count,
            flow_count=persisted_flow_count,
            strong_scene_pct=strong_scene_pct,
            strong_action_pct=strong_action_pct,
            avg_completeness=avg_completeness,
        )

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
            # Four-level readiness contract (replaces the bare boolean).
            # visual_e2e_ready stays True for ready/strong_ready for compatibility.
            "visual_e2e_status": visual_e2e_status,
            "visual_e2e_ready": visual_e2e_status in ("ready", "strong_ready"),
            "visual_scene_count_persisted": persisted_scene_count,
            "visual_edge_count_persisted": persisted_edge_count,
            "visual_flow_count_persisted": persisted_flow_count,
            "scene_state_coverage": round(strong_scene_pct, 3),
            "action_summary_coverage": round(strong_action_pct, 3),
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


@router.get("/api/v1/artifacts/{artifact_id}/progress")
async def stream_artifact_progress(
    artifact_id: str = Path(...),
    eyes_job_id: str | None = Query(
        None,
        description=(
            "Explicit eyes-engine job id.  If omitted, the endpoint tries "
            "to pull it from the artifact's stored ``full_artifact_json`` "
            "(``visual_extraction.job_id``)."
        ),
    ),
    poll_interval: float = Query(
        1.5, ge=0.5, le=10.0,
        description="Seconds between eyes-engine polls.",
    ),
    user: dict = Depends(get_current_user),
    request: Request = None,
):
    """Server-Sent Events stream of eyes-engine progress for one artifact.

    Each event is a single JSON object:

        data: {"progress_percent": 42.0, "current_stage": "chunk_3/12",
               "status": "running", "resumed": 2}

    The stream emits whenever the upstream job snapshot changes and
    terminates when the job reports ``completed`` / ``failed`` /
    ``cancelled`` — or when the client disconnects, or after a hard
    timeout (15 min) so a forgotten browser tab can't leak loop time.

    Tenant scoping: the artifact must belong to the requester.  When
    we have an ``eyes_job_id`` we additionally check the job's
    returned ``tenant_id`` so a leaked job-id cannot stream data from
    another tenant's recording.
    """
    tenant_id = user["tenant_id"]

    async with tenant_scoped_session(tenant_id) as db:
        artifact = await db.get(CanonicalArtifactRow, artifact_id)
        if not artifact or artifact.tenant_id != tenant_id:
            raise HTTPException(404, f"Artifact {artifact_id} not found")
        # If no explicit job id was passed, try the artifact JSON.  This
        # is best-effort — older artifacts wrote different shapes and
        # the SSE endpoint still works without an upstream job (it just
        # streams the current artifact status once and closes).
        if not eyes_job_id:
            full = getattr(artifact, "full_artifact_json", None) or {}
            stages = full if isinstance(full, dict) else {}
            visual = stages.get("visual_extraction") or stages.get("stages", {}).get("visual_extraction")
            if isinstance(visual, dict):
                eyes_job_id = visual.get("job_id") or visual.get("output", {}).get("job_id")
        snapshot_status = artifact.status
        snapshot_completeness = float(
            getattr(artifact, "semantic_completeness_score", None) or 0.0
        )

    config = request.app.state.config
    eyes_base = getattr(config, "eyes_engine_url", "http://localhost:8003")
    auth_header = request.headers.get("authorization") or ""

    async def stream():
        # If we don't have an eyes job to poll, emit a single
        # artifact-level snapshot and close.  Avoids holding the
        # connection open for nothing.
        if not eyes_job_id:
            payload = {
                "artifact_id": artifact_id,
                "status": snapshot_status,
                "progress_percent": None,
                "current_stage": None,
                "semantic_completeness_score": snapshot_completeness,
                "eyes_job_id": None,
                "note": "no eyes job linked to this artifact",
            }
            yield f"data: {json.dumps(payload)}\n\n"
            return

        terminal = {"completed", "failed", "cancelled"}
        last_payload: dict | None = None
        # ~15 min ceiling at the default 1.5s interval; clamp via
        # poll_interval Query validator (max 10s -> ~150 min).
        max_iterations = int(900 / max(poll_interval, 0.5))

        import httpx

        url = f"{eyes_base.rstrip('/')}/api/v1/eyes/jobs/{eyes_job_id}"
        headers = {"Authorization": auth_header} if auth_header else {}

        async with httpx.AsyncClient(timeout=10.0) as client:
            for _ in range(max_iterations):
                if await request.is_disconnected():
                    return
                try:
                    resp = await client.get(url, headers=headers)
                    if resp.status_code == 404:
                        yield f"data: {json.dumps({'error': 'eyes job not found', 'eyes_job_id': eyes_job_id})}\n\n"
                        return
                    if resp.status_code != 200:
                        yield f"data: {json.dumps({'error': f'eyes returned {resp.status_code}'})}\n\n"
                        await asyncio.sleep(poll_interval)
                        continue
                    job = resp.json()
                except Exception as exc:
                    yield f"data: {json.dumps({'error': str(exc)[:160]})}\n\n"
                    await asyncio.sleep(poll_interval)
                    continue

                # Cross-tenant guard — eyes does not enforce tenant
                # isolation on its job-status endpoint today, so we
                # check the returned record before relaying anything.
                job_tenant = (job or {}).get("tenant_id")
                if job_tenant and job_tenant != tenant_id:
                    yield f"data: {json.dumps({'error': 'forbidden — cross-tenant job'})}\n\n"
                    return

                payload = {
                    "artifact_id": artifact_id,
                    "eyes_job_id": eyes_job_id,
                    "status": job.get("status"),
                    "current_stage": job.get("current_stage"),
                    "progress_percent": job.get("progress_percent"),
                }
                # Only emit when something material changed.  Clients
                # parse JSON per-event so we don't want to fire 600
                # identical heartbeats over a 15-min job.
                if payload != last_payload:
                    yield f"data: {json.dumps(payload)}\n\n"
                    last_payload = payload

                if isinstance(payload["status"], str) and payload["status"].lower() in terminal:
                    return

                await asyncio.sleep(poll_interval)

        # Timed out without reaching terminal — emit one final
        # marker so the client can decide whether to reconnect.
        yield f"data: {json.dumps({'timeout': True, 'eyes_job_id': eyes_job_id})}\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx response buffering
        },
    )


@router.get("/api/v1/artifacts/{artifact_id}/transcript")
async def get_artifact_transcript(
    artifact_id: str = Path(...),
    user: dict = Depends(get_current_user),
):
    """Get the PII-safe transcript text for a canonical artifact."""
    tenant_id = user["tenant_id"]
    factory = require_db()
    async with factory() as db:
        row = await db.get(CanonicalArtifactRow, artifact_id)
        if not row or row.tenant_id != tenant_id:
            raise HTTPException(404, f"Artifact {artifact_id} not found")
        return {
            "artifact_id": row.artifact_id,
            "session_id": row.session_id,
            "safe_transcript_text": row.safe_transcript_text or "",
        }


@router.get("/api/v1/artifacts/{artifact_id}/frames")
async def list_artifact_frames(
    artifact_id: str = Path(...),
    page: int = Query(1, ge=1, description="1-based page number"),
    limit: int = Query(50, ge=1, le=200, description="Frames per page"),
    keyframes_only: bool = Query(False, description="Return only keyframes when True"),
    user: dict = Depends(get_current_user),
):
    """Return paginated visual evidence frames for a canonical artifact.

    Frame records are written by the persist_visual_evidence stage of
    the canonical pipeline (Spine engine, POST /api/v1/spine/persist-visual-frames).
    Results are ordered by frame_index ascending so consumers receive
    frames in temporal order.

    Query parameters:
        page           — 1-based page number (default 1)
        limit          — frames per page, max 200 (default 50)
        keyframes_only — when true, return only frames where is_keyframe=True

    Response shape:
        {
            "artifact_id": str,
            "page": int,
            "limit": int,
            "total": int,
            "frames": [VisualFrameRow fields, ...]
        }

    The `frame_asset_path` field in each frame record is the relative
    path used to construct an Eyes image URL via the client's
    ApiClient.getFrameImageUrl() helper.
    """
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as db:
        # Verify artifact ownership before exposing frame data
        artifact = await db.get(CanonicalArtifactRow, artifact_id)
        if not artifact or artifact.tenant_id != tenant_id:
            raise HTTPException(404, f"Artifact {artifact_id} not found")

        offset = (page - 1) * limit
        base_filter = [
            VisualFrameRow.artifact_id == artifact_id,
            VisualFrameRow.tenant_id == tenant_id,
        ]
        if keyframes_only:
            base_filter.append(VisualFrameRow.is_keyframe.is_(True))

        # Total count for pagination
        count_result = await db.execute(
            select(func.count()).select_from(VisualFrameRow).where(*base_filter)
        )
        total = count_result.scalar_one()

        # Paginated frame rows, ordered temporally
        frames_result = await db.execute(
            select(VisualFrameRow)
            .where(*base_filter)
            .order_by(asc(VisualFrameRow.frame_index))
            .offset(offset)
            .limit(limit)
        )
        frames = []
        for row in frames_result.scalars().all():
            frame_dict = row_to_dict(row)
            frame_dict["frame_asset_path"] = safe_frame_asset_path(
                tenant_id, frame_dict.get("frame_asset_path"),
            )
            frames.append(frame_dict)

        return {
            "artifact_id": artifact_id,
            "page": page,
            "limit": limit,
            "total": total,
            "frames": frames,
        }


@router.get("/api/v1/artifacts/{artifact_id}/visual-scenes")
async def list_visual_scenes(
    artifact_id: str = Path(...),
    instance_id: str | None = Query(None, description="Filter by app_instance_id"),
    page: int = Query(1, ge=1, description="1-based page number"),
    limit: int = Query(
        _VISUAL_EVIDENCE_PAGE_DEFAULT, ge=1, le=_VISUAL_EVIDENCE_PAGE_MAX,
        description="Scenes per page (max 2000)",
    ),
    user: dict = Depends(get_current_user),
):
    """Return visual scenes for a canonical artifact (Phase 3).

    Scenes are groups of consecutive visually-similar frames, ordered by
    scene_index.  Optionally filtered by ``instance_id`` to show only
    scenes belonging to a specific app instance.

    Response shape:
        {
            "artifact_id": str,
            "page": int,
            "limit": int,
            "total": int,    # total across all pages
            "scenes": [VisualSceneRow fields, ...]
        }
    """
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as db:
        artifact = await db.get(CanonicalArtifactRow, artifact_id)
        if not artifact or artifact.tenant_id != tenant_id:
            raise HTTPException(404, f"Artifact {artifact_id} not found")

        filters = [
            VisualSceneRow.artifact_id == artifact_id,
            VisualSceneRow.tenant_id == tenant_id,
        ]
        if instance_id:
            filters.append(VisualSceneRow.app_instance_id == instance_id)

        count_q = await db.execute(
            select(func.count()).select_from(VisualSceneRow).where(*filters)
        )
        total = count_q.scalar_one()

        offset = (page - 1) * limit
        result = await db.execute(
            select(VisualSceneRow, VisualFrameRow.frame_asset_path)
            .outerjoin(
                VisualFrameRow,
                VisualSceneRow.representative_frame_id == VisualFrameRow.frame_id,
            )
            .where(*filters)
            .order_by(asc(VisualSceneRow.scene_index))
            .offset(offset)
            .limit(limit)
        )
        scenes = []
        for scene_row, frame_asset_path in result.all():
            scene_dict = row_to_dict(scene_row)
            scene_dict["representative_frame_asset_path"] = safe_frame_asset_path(
                tenant_id, frame_asset_path,
            )
            scenes.append(scene_dict)
        return {
            "artifact_id": artifact_id,
            "page": page,
            "limit": limit,
            "total": total,
            "scenes": scenes,
        }


@router.get("/api/v1/artifacts/{artifact_id}/app-instances")
async def list_app_instances(
    artifact_id: str = Path(...),
    page: int = Query(1, ge=1, description="1-based page number"),
    limit: int = Query(
        _VISUAL_EVIDENCE_PAGE_DEFAULT, ge=1, le=_VISUAL_EVIDENCE_PAGE_MAX,
        description="Instances per page (max 2000)",
    ),
    user: dict = Depends(get_current_user),
):
    """Return app instances detected in a canonical artifact (Phase 5).

    App instances are contiguous screen groups that share the same
    application context.  Each instance carries ``segmentation_basis``
    and ``segmentation_confidence`` so consumers know the detection method.

    Response shape:
        {
            "artifact_id": str,
            "page": int,
            "limit": int,
            "total": int,
            "instances": [AppInstanceRow fields, ...]
        }
    """
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as db:
        artifact = await db.get(CanonicalArtifactRow, artifact_id)
        if not artifact or artifact.tenant_id != tenant_id:
            raise HTTPException(404, f"Artifact {artifact_id} not found")

        filters = [
            AppInstanceRow.artifact_id == artifact_id,
            AppInstanceRow.tenant_id == tenant_id,
        ]

        count_q = await db.execute(
            select(func.count()).select_from(AppInstanceRow).where(*filters)
        )
        total = count_q.scalar_one()

        offset = (page - 1) * limit
        result = await db.execute(
            select(AppInstanceRow)
            .where(*filters)
            .order_by(asc(AppInstanceRow.first_scene_index))
            .offset(offset)
            .limit(limit)
        )
        instances = [row_to_dict(r) for r in result.scalars().all()]
        return {
            "artifact_id": artifact_id,
            "page": page,
            "limit": limit,
            "total": total,
            "instances": instances,
        }


@router.get("/api/v1/artifacts/{artifact_id}/evidence-controls")
async def list_evidence_controls(
    artifact_id: str = Path(...),
    scene_id: str | None = Query(default=None, description="Filter by scene_id"),
    automation_ready: bool | None = Query(default=None, description="Filter by automation_ready"),
    page: int = Query(1, ge=1, description="1-based page number"),
    limit: int = Query(
        _VISUAL_EVIDENCE_PAGE_DEFAULT, ge=1, le=_VISUAL_EVIDENCE_PAGE_MAX,
        description="Controls per page (max 2000)",
    ),
    user: dict = Depends(get_current_user),
):
    """Return UI controls extracted from a canonical artifact's scenes (Phase 6).

    Every control carries explicit ``selector_source`` and ``automation_ready``
    fields so callers know the grounding level.  Controls with
    ``automation_ready=false`` have an empty ``playwright_selector`` — no
    guessed selectors are stored.

    Query params:
        scene_id          — restrict to one scene (optional)
        automation_ready  — ``true`` / ``false`` filter (optional)

    Response shape:
        {
            "artifact_id": str,
            "total": int,
            "automation_ready_count": int,
            "controls": [EvidenceControl, ...]
        }
    """
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as db:
        artifact = await db.get(CanonicalArtifactRow, artifact_id)
        if not artifact or artifact.tenant_id != tenant_id:
            raise HTTPException(404, f"Artifact {artifact_id} not found")

        filters = [
            EvidenceControlRow.artifact_id == artifact_id,
            EvidenceControlRow.tenant_id == tenant_id,
        ]
        if scene_id is not None:
            filters.append(EvidenceControlRow.scene_id == scene_id)
        if automation_ready is not None:
            filters.append(EvidenceControlRow.automation_ready == automation_ready)

        count_q = await db.execute(
            select(func.count()).select_from(EvidenceControlRow).where(*filters)
        )
        total = count_q.scalar_one()

        ready_count_q = await db.execute(
            select(func.count()).select_from(EvidenceControlRow).where(
                *filters, EvidenceControlRow.automation_ready.is_(True),
            )
        )
        ready_count = ready_count_q.scalar_one()

        offset = (page - 1) * limit
        result = await db.execute(
            select(EvidenceControlRow)
            .where(*filters)
            .order_by(asc(EvidenceControlRow.scene_id), asc(EvidenceControlRow.control_id))
            .offset(offset)
            .limit(limit)
        )
        controls = [row_to_dict(r) for r in result.scalars().all()]
        return {
            "artifact_id": artifact_id,
            "page": page,
            "limit": limit,
            "total": total,
            "automation_ready_count": ready_count,
            "controls": controls,
        }


@router.get("/api/v1/artifacts/{artifact_id}/evidence-steps")
async def list_evidence_steps(
    artifact_id: str = Path(...),
    scene_id: str | None = Query(default=None, description="Filter by scene_id"),
    min_confidence: float = Query(
        default=0.0, ge=0.0, le=1.0,
        description="Minimum step confidence (0..1).  Useful for hiding "
                    "low-signal rows in the UI without altering the DB.",
    ),
    page: int = Query(1, ge=1, description="1-based page number"),
    limit: int = Query(
        _VISUAL_EVIDENCE_PAGE_DEFAULT, ge=1, le=_VISUAL_EVIDENCE_PAGE_MAX,
        description="Steps per page (max 2000)",
    ),
    user: dict = Depends(get_current_user),
):
    """Return triangulated user-action steps inside a canonical artifact.

    Each step is the output of the multimodal classifier combining OCR
    diff, cursor click, audio intent, and LLaVA delta into one record
    with explicit ``agreement_score`` and ``evidence_signals`` lists.
    The frontend uses this to render the per-scene action timeline with
    confidence badges (Phase D.1).

    Steps are ordered by scene_index then step_index so callers iterating
    the entire artifact get a chronological action sequence.
    """
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as db:
        artifact = await db.get(CanonicalArtifactRow, artifact_id)
        if not artifact or artifact.tenant_id != tenant_id:
            raise HTTPException(404, f"Artifact {artifact_id} not found")

        filters = [
            EvidenceStepRow.artifact_id == artifact_id,
            EvidenceStepRow.tenant_id == tenant_id,
        ]
        if scene_id is not None:
            filters.append(EvidenceStepRow.scene_id == scene_id)
        if min_confidence > 0.0:
            filters.append(EvidenceStepRow.confidence >= min_confidence)

        count_q = await db.execute(
            select(func.count()).select_from(EvidenceStepRow).where(*filters)
        )
        total = count_q.scalar_one()

        # Distribution by action_kind, computed over the full filtered set
        # (not just the page) so the UI's per-kind counts stay correct
        # across pages.
        kinds_q = await db.execute(
            select(EvidenceStepRow.action_kind, func.count())
            .where(*filters)
            .group_by(EvidenceStepRow.action_kind)
        )
        kinds_count: dict[str, int] = {
            (kind or "unknown"): int(cnt) for kind, cnt in kinds_q.all()
        }

        # Order by scene_index → step_index.  We need a join through
        # visual_scenes for scene_index since EvidenceStepRow stores only
        # scene_id (which is opaque).
        offset = (page - 1) * limit
        rows_q = await db.execute(
            select(EvidenceStepRow, VisualSceneRow.scene_index)
            .join(
                VisualSceneRow,
                VisualSceneRow.scene_id == EvidenceStepRow.scene_id,
            )
            .where(*filters)
            .order_by(asc(VisualSceneRow.scene_index), asc(EvidenceStepRow.step_index))
            .offset(offset)
            .limit(limit)
        )
        steps: list[dict] = []
        for step_row, scene_index in rows_q.all():
            data = row_to_dict(step_row)
            data["scene_index"] = int(scene_index or 0)
            steps.append(data)
        return {
            "artifact_id": artifact_id,
            "page": page,
            "limit": limit,
            "total": total,
            "by_action_kind": kinds_count,
            "steps": steps,
        }


@router.get("/api/v1/artifacts/{artifact_id}/cursor-events")
async def list_cursor_events(
    artifact_id: str = Path(...),
    frame_id: str | None = Query(default=None, description="Filter by frame_id"),
    is_click: bool | None = Query(
        default=None, description="When true, only click events; "
                                  "when false, only non-click events.",
    ),
    page: int = Query(1, ge=1, description="1-based page number"),
    limit: int = Query(
        _VISUAL_EVIDENCE_PAGE_DEFAULT, ge=1, le=_VISUAL_EVIDENCE_PAGE_MAX,
        description="Events per page (max 2000)",
    ),
    user: dict = Depends(get_current_user),
):
    """Return per-frame cursor positions and click annotations.

    Used by the frontend step-replay overlay (Phase D.2): clicking a
    step on the bottom panel seeks the video to the step's
    ``after_frame_id`` and draws a cursor marker at the matching
    cursor_event coordinates.
    """
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as db:
        artifact = await db.get(CanonicalArtifactRow, artifact_id)
        if not artifact or artifact.tenant_id != tenant_id:
            raise HTTPException(404, f"Artifact {artifact_id} not found")

        filters = [
            CursorEventRow.artifact_id == artifact_id,
            CursorEventRow.tenant_id == tenant_id,
        ]
        if frame_id is not None:
            filters.append(CursorEventRow.frame_id == frame_id)
        if is_click is not None:
            filters.append(CursorEventRow.is_click == is_click)

        count_q = await db.execute(
            select(func.count()).select_from(CursorEventRow).where(*filters)
        )
        total = count_q.scalar_one()

        click_count_q = await db.execute(
            select(func.count()).select_from(CursorEventRow).where(
                *filters, CursorEventRow.is_click.is_(True),
            )
        )
        click_count = click_count_q.scalar_one()

        offset = (page - 1) * limit
        result = await db.execute(
            select(CursorEventRow)
            .where(*filters)
            .order_by(asc(CursorEventRow.frame_index))
            .offset(offset)
            .limit(limit)
        )
        events = [row_to_dict(r) for r in result.scalars().all()]
        return {
            "artifact_id": artifact_id,
            "page": page,
            "limit": limit,
            "total": total,
            "click_count": click_count,
            "events": events,
        }


def _canonicalize_scene_urls(
    scenes: list[dict],
    *,
    app_groupings: list[dict],
    scene_to_app_grouping: dict[str, str],
) -> None:
    """Mutate each scene's ``detected_url`` to use the canonical hostname.

    OCR routinely misreads URL bar text (``usaa.com`` → ``Msdd.com``).
    The app_deduper has already identified canonical domains for each
    grouping; we use them here to clean the URL hostname while
    preserving the OCR'd path/query/fragment (which is usually correct
    because OCR does better on lowercase + slashes than on logos /
    address-bar fonts).

    Strategy
    --------
    1. Determine the artifact's DOMINANT canonical_domain — the
       canonical_domain of the app_grouping with the highest
       ``total_scene_count``.  When all groupings are small or none
       have canonical_domain, we leave URLs alone (single-app heuristic
       can't safely fire on multi-app journeys without more signal).
    2. For each scene, find the grouping it belongs to via
       ``scene_to_app_grouping``.
    3. If that grouping's canonical_domain matches the dominant, use
       its hostname directly.
    4. If the grouping's scene_count is much smaller than the dominant
       AND its canonical_domain looks like an OCR ghost (different
       from the dominant on a single-app artifact), substitute the
       dominant hostname.  Preserves path/query/fragment.
    5. When no rewrite is possible (no groupings, multi-app, or path
       parsing fails), leave the URL alone.

    Side effect: each scene_dict gets a new key
    ``canonical_url`` (the cleaned form) alongside the original
    ``detected_url`` (kept for audit).  Frontend should prefer
    ``canonical_url`` when present.
    """
    if not scenes or not app_groupings:
        return

    # 1) Find dominant grouping's canonical_domain.
    #
    # Real internet domains are case-insensitive but always RENDER
    # lowercase in browser URL bars (~99% of pages).  OCR misreads of
    # logos / address-bar fonts often produce mixed-case strings ("Msdd",
    # "Wivw") which masquerade as canonical_domain on an OCR-ghost
    # grouping.  When picking the artifact's dominant domain, prefer
    # the highest-scene-count grouping whose canonical_domain is
    # ALL-LOWERCASE.  Only fall back to mixed-case candidates when no
    # clean candidate exists.
    def _is_clean_domain(s: str) -> bool:
        """True when ``s`` looks like a real lowercase domain."""
        if not s:
            return False
        # Must have at least one dot (no bare hostnames) and be all-lower
        # in the part before the slash.
        if "." not in s:
            return False
        if s != s.lower():
            return False
        # Reject obvious noise (very short subdomains, no TLD).
        labels = s.split(".")
        if any(len(lbl) < 2 for lbl in labels):
            return False
        return True

    dominant_domain = ""
    dominant_count = 0
    # First pass — only consider clean (lowercase) candidates.
    for g in app_groupings:
        cd = (g.get("canonical_domain") or "").strip()
        if not _is_clean_domain(cd):
            continue
        count = int(g.get("total_scene_count") or 0)
        if count > dominant_count:
            dominant_count = count
            dominant_domain = cd.lower()
    # Second pass — fall back to any candidate (even mixed-case) when
    # no clean one exists.  This avoids breaking artifacts that have
    # legitimately mixed-case canonical_domains (rare but possible).
    if not dominant_domain:
        for g in app_groupings:
            cd = (g.get("canonical_domain") or "").strip().lower()
            if not cd or "." not in cd:
                continue
            count = int(g.get("total_scene_count") or 0)
            if count > dominant_count:
                dominant_count = count
                dominant_domain = cd
    if not dominant_domain:
        return

    # Find the dominant grouping ITSELF (its full row) so we can use its
    # canonical_name / display_label when rewriting ghost groupings.
    dominant_grouping_row: dict | None = None
    for g in app_groupings:
        if (g.get("canonical_domain") or "").strip().lower() == dominant_domain:
            dominant_grouping_row = g
            break

    # ── Rewrite app_groupings that look like OCR ghosts of the dominant.
    #
    # A grouping is rewritten ONLY when:
    #   1. It is web_ui app_type (don't touch desktop/mainframe/email apps)
    #   2. It has a non-empty canonical_domain (don't touch native UIs
    #      that legitimately have no URL — "Web Application — New Tab",
    #      "Desktop Application — ...", etc.)
    #   3. Its canonical_domain differs from the dominant
    #   4. EITHER its canonical_domain is dirty (uppercase / no dot)
    #      OR its canonical_name has uppercase first-letter while the
    #      dominant's name is all-lowercase (typical OCR misread
    #      signature — real domains render lowercase)
    #
    # For each ghost grouping we substitute its name/domain/label with
    # the dominant's values.  The grouping_id stays the same so
    # scene_to_app_grouping mappings continue to work; only the
    # human-visible labels change.  This makes the "Msdd.com" tab in
    # the flow rail disappear because the ghost grouping now reports
    # canonical_name="usaa.com".
    def _name_looks_like_ocr_ghost(name: str) -> bool:
        """True when the canonical_name has mixed-case letters before
        the .com / .net / .io / etc, which is the dominant OCR-misread
        signature for browser-bar domain text."""
        if not name:
            return False
        # Strip after first dot: "Msdd.com" → "Msdd", "usaa.com" → "usaa"
        head = name.split(".", 1)[0]
        if not head:
            return False
        # Real domains render lowercase in URL bars.  Any uppercase
        # letter in the head portion is a strong OCR-misread signal.
        return any(ch.isupper() for ch in head)

    for g in app_groupings:
        cd = (g.get("canonical_domain") or "").strip()
        cn = (g.get("canonical_name") or "").strip()
        app_type = (g.get("app_type") or "").lower()

        # Rule 1: never touch non-web-ui app types.
        if app_type != "web_ui":
            continue

        # Rule 2: never touch native/empty-domain groupings.  These are
        # things like "Web Application — New Tab" or desktop apps
        # misclassified as web_ui — they legitimately have no domain.
        if not cd:
            continue

        # Rule 3: never touch the dominant itself.
        if cd.lower() == dominant_domain:
            continue

        # Rule 4: rewrite iff the grouping looks like an OCR ghost.
        is_ghost = (not _is_clean_domain(cd)) or _name_looks_like_ocr_ghost(cn)
        if not is_ghost:
            continue  # legit secondary app (multi-app session) — leave alone

        if dominant_grouping_row is not None:
            g["canonical_name"] = dominant_grouping_row.get("canonical_name") or dominant_domain
            g["canonical_domain"] = dominant_grouping_row.get("canonical_domain") or dominant_domain
            g["display_label"] = dominant_grouping_row.get("display_label") or dominant_domain
        else:
            g["canonical_name"] = dominant_domain
            g["canonical_domain"] = dominant_domain
            g["display_label"] = dominant_domain

    # 2) Build grouping_id → (canonical_domain, scene_count) lookup.
    grouping_lookup: dict[str, tuple[str, int]] = {}
    for g in app_groupings:
        gid = g.get("grouping_id")
        if not gid:
            continue
        grouping_lookup[str(gid)] = (
            (g.get("canonical_domain") or "").strip().lower(),
            int(g.get("total_scene_count") or 0),
        )

    # 3) Multi-app heuristic — if any non-dominant grouping has a
    # comparable scene_count (>=50% of dominant) AND that grouping's
    # domain is also lowercase-clean (not an OCR ghost), treat the
    # journey as multi-app and don't cross-substitute hostnames.
    # Mixed-case "competing" groupings are treated as ghosts and the
    # dominant lowercase domain wins.
    multi_app = any(
        cnt >= max(1, int(dominant_count * 0.5))
        and dom != dominant_domain
        and _is_clean_domain(dom)
        for dom, cnt in grouping_lookup.values()
        if dom  # ignore empty-domain (desktop apps)
    )

    # 4) Per-scene rewrite.
    from urllib.parse import urlparse

    for scene in scenes:
        raw_url = (scene.get("detected_url") or "").strip()
        if not raw_url:
            continue

        scene_id = str(scene.get("scene_id") or "")
        grouping_id = scene_to_app_grouping.get(scene_id) or ""
        grouping_domain, grouping_count = grouping_lookup.get(grouping_id, ("", 0))

        # Choose the hostname to use.
        # - Single-app journey: always use the dominant.
        # - Multi-app: use the scene's own grouping domain when it has
        #   enough scenes (real app); fall back to dominant when grouping
        #   looks like a ghost (low scene_count).
        ghost_threshold = max(1, int(dominant_count * 0.25))
        if multi_app and grouping_domain and grouping_count >= ghost_threshold:
            target_hostname = grouping_domain
        else:
            target_hostname = dominant_domain

        # Parse the OCR'd URL.  When it's not a well-formed URL,
        # synthesize a clean one from scratch using the dominant.
        try:
            parsed = urlparse(raw_url if "://" in raw_url else "https://" + raw_url)
            path = parsed.path or ""
            query = ("?" + parsed.query) if parsed.query else ""
            fragment = ("#" + parsed.fragment) if parsed.fragment else ""
            scheme = parsed.scheme or "https"
            current_hostname = (parsed.hostname or "").strip().lower()
        except Exception:  # pragma: no cover - urlparse rarely raises
            path, query, fragment, scheme, current_hostname = "", "", "", "https", ""

        # No-op when the OCR hostname already matches the target.
        if current_hostname == target_hostname:
            scene["canonical_url"] = raw_url
            continue

        canonical = f"{scheme}://{target_hostname}{path}{query}{fragment}"
        scene["canonical_url"] = canonical


def _empty_storyboard_overlay() -> dict:
    """Default-empty overlay payload returned when the flag is off.

    Kept as a separate helper so the route function stays focused on
    its main 7-step query plan; the empty-shape contract lives here.

    Phase 2 keys (page_visits, page_actions, form_snapshots) are
    included so clients can read these unconditionally without
    null-checks.
    """
    return {
        "app_groupings": [],
        "noise_scene_ids": [],
        "scene_captions": {},
        "annotated_frame_urls": {},
        "scene_to_app_grouping": {},
        "scene_actions": {},
        # Phase 2 — URL-keyed surfaces.
        "page_visits": [],
        "page_actions": {},
        "form_snapshots": {},
    }


async def _load_storyboard_overlay(
    db,
    *,
    artifact_id: str,
    tenant_id: str,
    window_scene_ids: list[str],
) -> dict:
    """Read storyboard-derived rows and shape them for the 3D Journey.

    All four queries are tenant-scoped and run in parallel against the
    same RLS session.  Outputs are keyed by scene_id so the frontend
    can join them onto its existing scene loop without restructuring.

    When the storyboard composer has not yet derived overlays for an
    artifact (e.g. first /storyboard call hasn't fired), the returned
    payload is empty — the 3D Journey then falls back to its current
    heuristic-only behavior automatically.
    """
    # 1) App groupings — dedup'd apps (1 row per real app, not per
    #    OCR-corrupted boundary).  Sorted so the response is stable
    #    across calls.
    groupings_q = await db.execute(
        select(AppGroupingRow)
        .where(
            AppGroupingRow.artifact_id == artifact_id,
            AppGroupingRow.tenant_id == tenant_id,
        )
        .order_by(asc(AppGroupingRow.first_scene_index))
    )
    app_groupings: list[dict] = []
    # member_instance_id → grouping_id reverse lookup so callers can
    # map raw app_instance ids onto their canonical grouping.
    instance_to_grouping: dict[str, str] = {}
    for row in groupings_q.scalars().all():
        groupings_dict = row_to_dict(row)
        app_groupings.append(groupings_dict)
        for instance_id in (row.member_instance_ids or []):
            instance_to_grouping[str(instance_id)] = row.grouping_id

    # 2) Noise scene ids — drawn from storyboard_panels.  We surface
    #    the underlying scene_ids that landed inside any panel flagged
    #    is_noise so the 3D Journey can filter them.  A noise panel
    #    can span multiple scenes when scene_grouper collapsed them;
    #    we expand back to scene-level here for the journey rail.
    noise_panels_q = await db.execute(
        select(StoryboardPanelRow)
        .where(
            StoryboardPanelRow.artifact_id == artifact_id,
            StoryboardPanelRow.tenant_id == tenant_id,
            StoryboardPanelRow.is_noise.is_(True),
        )
    )
    noise_scene_ids: set[str] = set()
    # Also fold the panel→app_grouping back-link into scene-level
    # mapping so the journey rail can label each card with its dedup'd
    # app even when scenes weren't grouped 1:1.
    scene_to_app_grouping: dict[str, str] = {}
    panels_q = await db.execute(
        select(StoryboardPanelRow)
        .where(
            StoryboardPanelRow.artifact_id == artifact_id,
            StoryboardPanelRow.tenant_id == tenant_id,
        )
    )
    panels: list[StoryboardPanelRow] = list(panels_q.scalars().all())
    for panel in panels:
        if panel.is_noise:
            # Best-effort: mark first_scene_id and any scene_id
            # encoded in grouper_signals.scene_id_members.
            noise_scene_ids.add(str(panel.first_scene_id))
            noise_scene_ids.add(str(panel.last_scene_id))
            members = (panel.grouper_signals or {}).get("scene_id_members")
            if isinstance(members, list):
                for sid in members:
                    if sid:
                        noise_scene_ids.add(str(sid))
        if panel.app_grouping_id:
            scene_to_app_grouping[str(panel.first_scene_id)] = panel.app_grouping_id
            members = (panel.grouper_signals or {}).get("scene_id_members") or []
            for sid in members:
                if sid:
                    scene_to_app_grouping[str(sid)] = panel.app_grouping_id

    # 3) LLM short captions per scene.  Only the rows matching the
    #    current generator_version are surfaced — older versions stay
    #    in the table for audit but are not returned to clients.
    #    Scoped to the scenes in the current window when window is
    #    populated, otherwise return everything.
    caption_filter = [
        SceneCaptionRow.artifact_id == artifact_id,
        SceneCaptionRow.tenant_id == tenant_id,
    ]
    if window_scene_ids:
        caption_filter.append(SceneCaptionRow.scene_id.in_(window_scene_ids))
    captions_q = await db.execute(
        select(
            SceneCaptionRow.scene_id,
            SceneCaptionRow.short_caption,
            SceneCaptionRow.narrative_caption,
            SceneCaptionRow.caption_quality,
            SceneCaptionRow.generator_model,
        ).where(*caption_filter)
    )
    scene_captions: dict[str, dict] = {}
    for scene_id, short_cap, narr, quality, model in captions_q.all():
        if not scene_id:
            continue
        scene_captions[str(scene_id)] = {
            "short": short_cap or "",
            "narrative": narr or "",
            "quality": quality or "weak",
            "model": model or "",
        }

    # 4) Annotated frame URLs.
    #
    # The annotated cache is keyed by the STORYBOARD PANEL's
    # representative_frame_id (set by scene_grouper when it picked
    # the best frame across all collapsed scenes), NOT the
    # VisualSceneRow's representative_frame_id (the pipeline's
    # per-scene pick).  When scene_grouper collapses multiple scenes
    # into one panel, every member scene inherits the panel's
    # annotated URL.  Without this fan-out the 3D Journey would show
    # blank thumbnails for any scene that wasn't the panel's chosen
    # representative — even though the panel has an annotated PNG.
    annotated_q = await db.execute(
        select(AnnotatedFrameCacheRow.frame_id)
        .where(
            AnnotatedFrameCacheRow.artifact_id == artifact_id,
            AnnotatedFrameCacheRow.tenant_id == tenant_id,
        )
    )
    annotated_frame_ids = {str(fid) for (fid,) in annotated_q.all()}
    annotated_frame_urls: dict[str, str] = {}
    for panel in panels:
        if not panel.representative_frame_id:
            continue
        if str(panel.representative_frame_id) not in annotated_frame_ids:
            continue
        url = (
            f"/api/v1/artifacts/{artifact_id}"
            f"/frames/{panel.representative_frame_id}/annotated.png"
        )
        # Fan out: every scene that landed in this panel inherits the
        # same annotated URL.  Includes first_scene_id, last_scene_id,
        # and any scene_id_members recorded by the grouper.
        annotated_frame_urls[str(panel.first_scene_id)] = url
        annotated_frame_urls[str(panel.last_scene_id)] = url
        members = (panel.grouper_signals or {}).get("scene_id_members") or []
        for sid in members:
            if sid:
                annotated_frame_urls[str(sid)] = url

    # 5) Scene actions — the multimodal-LLM-extracted semantic actions
    #    (verb / target / value / confidence / reasoning) per scene.
    #    Keyed by scene_id like scene_captions so the frontend can join
    #    them onto its existing scene loop.  Drives the 3D Journey
    #    bottom detail panel ("user selected TX from State dropdown")
    #    and the test-export pipeline.
    #
    # Phase 1.6 — each value is now a LIST.  Long form-fill scenes
    # contribute multiple rows (one per detected action) ordered by
    # subaction_index.  Short scenes return a list of length 1 (or
    # empty when no row was extracted).  Older clients read
    # ``scene_actions[scene_id][0]`` for backwards-compatible single-
    # action rendering.
    action_filter = [
        SceneActionRow.artifact_id == artifact_id,
        SceneActionRow.tenant_id == tenant_id,
    ]
    if window_scene_ids:
        action_filter.append(SceneActionRow.scene_id.in_(window_scene_ids))
    actions_q = await db.execute(
        select(
            SceneActionRow.scene_id,
            SceneActionRow.subaction_index,
            SceneActionRow.verb,
            SceneActionRow.target_label,
            SceneActionRow.target_kind,
            SceneActionRow.value,
            SceneActionRow.confidence,
            SceneActionRow.automation_ready,
            SceneActionRow.reasoning,
            SceneActionRow.evidence_signals,
            SceneActionRow.extractor_model,
        )
        .where(*action_filter)
        .order_by(
            SceneActionRow.scene_id.asc(),
            SceneActionRow.subaction_index.asc(),
        )
    )
    scene_actions: dict[str, list[dict]] = {}
    for sid, sub_idx, verb, target_label, target_kind, value, conf, auto_ready, reasoning, ev, model in actions_q.all():
        if not sid:
            continue
        scene_actions.setdefault(str(sid), []).append({
            "subaction_index": int(sub_idx or 0),
            "verb": verb or "none",
            "target_label": target_label or "",
            "target_kind": target_kind or "other",
            "value": value,  # may be null
            "confidence": float(conf or 0.0),
            "automation_ready": bool(auto_ready),
            "reasoning": reasoning or "",
            "evidence_signals": ev or {},
            "extractor_model": model or "",
        })

    # 6) Page visits — Phase 2.0 URL-keyed log.  Independent of scenes;
    #    surfaces every distinct URL the user visited (even when
    #    scene_grouper merged 9 form sub-pages into one scene because
    #    their layout was identical).  Sorted by sequence_index so the
    #    payload matches the recording's chronology.  Form snapshot
    #    (label → value dict) is folded into each visit row so test
    #    exporters can iterate once.
    #
    # CRITICAL: filter to the CURRENT (max) extractor_version.
    # page_visits keeps prior versions for audit (the extractor's
    # stale-row cleanup only prunes within a version), so without this
    # filter we return every version interleaved — e.g. v1 (11 rows) +
    # v2 (14 rows) = 25 rows with duplicate sequence_index values, which
    # the UI renders as duplicate/interleaved pages.
    current_page_visit_version = (
        select(func.max(PageVisitRow.extractor_version))
        .where(
            PageVisitRow.artifact_id == artifact_id,
            PageVisitRow.tenant_id == tenant_id,
        )
        .scalar_subquery()
    )
    page_visits_q = await db.execute(
        select(
            PageVisitRow.page_visit_id,
            PageVisitRow.sequence_index,
            PageVisitRow.location,
            PageVisitRow.url_host,
            PageVisitRow.url_path,
            PageVisitRow.url_query,
            PageVisitRow.canonical_host,
            PageVisitRow.first_seen_ms,
            PageVisitRow.last_seen_ms,
            PageVisitRow.duration_ms,
            PageVisitRow.frame_count,
            PageVisitRow.source,
            PageVisitRow.extraction_confidence,
            PageVisitRow.primary_scene_id,
            PageVisitRow.form_snapshot,
            PageVisitRow.form_snapshot_signals,
            PageVisitRow.extractor_version,
            PageVisitRow.form_snapshot_extractor_version,
        )
        .where(
            PageVisitRow.artifact_id == artifact_id,
            PageVisitRow.tenant_id == tenant_id,
            PageVisitRow.extractor_version == current_page_visit_version,
        )
        .order_by(PageVisitRow.sequence_index.asc())
    )
    page_visits: list[dict] = []
    form_snapshots: dict[str, dict] = {}
    for (
        visit_id, seq_idx, location, url_host, url_path, url_query,
        canonical_host, first_seen, last_seen, duration, frame_count,
        source, extraction_conf, primary_scene_id,
        form_snapshot, snapshot_signals, extractor_v, snapshot_extractor_v,
    ) in page_visits_q.all():
        snapshot = form_snapshot or {}
        snapshot_meta = snapshot_signals or {}
        page_visits.append({
            "page_visit_id": str(visit_id),
            "sequence_index": int(seq_idx or 0),
            "location": location or "",
            "url_host": url_host or "",
            "url_path": url_path or "",
            "url_query": url_query or "",
            "canonical_host": canonical_host or "",
            "first_seen_ms": int(first_seen or 0),
            "last_seen_ms": int(last_seen or 0),
            "duration_ms": int(duration or 0),
            "frame_count": int(frame_count or 0),
            "source": source or "url_regex",
            "extraction_confidence": float(extraction_conf or 0.0),
            "primary_scene_id": (
                str(primary_scene_id) if primary_scene_id else None
            ),
            "form_snapshot": snapshot,
            "form_snapshot_signals": snapshot_meta,
            "extractor_version": extractor_v or "",
            "form_snapshot_extractor_version": snapshot_extractor_v or "",
        })
        if snapshot:
            form_snapshots[str(visit_id)] = {
                "fields": snapshot,
                "field_kinds": snapshot_meta.get("field_kinds") or {},
                "visible_field_count": snapshot_meta.get(
                    "visible_field_count", len(snapshot),
                ),
                "overall_confidence": float(
                    snapshot_meta.get("overall_confidence", 0.0) or 0.0,
                ),
                "page_intent": snapshot_meta.get("page_intent", "") or "",
                "source_frame_asset_path": snapshot_meta.get(
                    "source_frame_asset_path", "",
                ) or "",
                "extractor_model": snapshot_meta.get("extractor_model", "") or "",
                "extractor_version": snapshot_extractor_v or "",
            }

    # 7) Page actions — Phase 2.1.  One LIST per page_visit_id, ordered
    #    by subaction_index so multi-action pages return chronological
    #    sequences.  Same shape as scene_actions so test exporters can
    #    iterate uniformly.
    #
    # CRITICAL: filter to the CURRENT (max) extractor_version — same
    # reason as page_visits above.  page_actions accumulated v1..v5
    # (101 rows) during the deploy iteration; without this filter the
    # UI stacks 4-5 versions of actions on every page.  The current
    # action version's rows are keyed to the current visit version's
    # page_visit_ids (the extractor filters visits to max version), so
    # the two filters stay consistent.
    current_page_action_version = (
        select(func.max(PageActionRow.extractor_version))
        .where(
            PageActionRow.artifact_id == artifact_id,
            PageActionRow.tenant_id == tenant_id,
        )
        .scalar_subquery()
    )
    page_actions_q = await db.execute(
        select(
            PageActionRow.page_visit_id,
            PageActionRow.subaction_index,
            PageActionRow.verb,
            PageActionRow.target_label,
            PageActionRow.target_kind,
            PageActionRow.value,
            PageActionRow.confidence,
            PageActionRow.automation_ready,
            PageActionRow.reasoning,
            PageActionRow.evidence_signals,
            PageActionRow.extractor_model,
        )
        .where(
            PageActionRow.artifact_id == artifact_id,
            PageActionRow.tenant_id == tenant_id,
            PageActionRow.extractor_version == current_page_action_version,
        )
        .order_by(
            PageActionRow.page_visit_id.asc(),
            PageActionRow.subaction_index.asc(),
        )
    )
    page_actions: dict[str, list[dict]] = {}
    for (
        visit_id, sub_idx, verb, target_label, target_kind, value,
        conf, auto_ready, reasoning, ev, model,
    ) in page_actions_q.all():
        if not visit_id:
            continue
        page_actions.setdefault(str(visit_id), []).append({
            "subaction_index": int(sub_idx or 0),
            "verb": verb or "none",
            "target_label": target_label or "",
            "target_kind": target_kind or "other",
            "value": value,
            "confidence": float(conf or 0.0),
            "automation_ready": bool(auto_ready),
            "reasoning": reasoning or "",
            "evidence_signals": ev or {},
            "extractor_model": model or "",
        })

    return {
        "app_groupings": app_groupings,
        "noise_scene_ids": sorted(noise_scene_ids),
        "scene_captions": scene_captions,
        "annotated_frame_urls": annotated_frame_urls,
        "scene_to_app_grouping": scene_to_app_grouping,
        "scene_actions": scene_actions,
        # Phase 2 surfaces — URL-keyed.
        "page_visits": page_visits,
        "page_actions": page_actions,
        "form_snapshots": form_snapshots,
    }


@router.get("/api/v1/artifacts/{artifact_id}/visual-evidence-graph")
async def get_visual_evidence_graph(
    artifact_id: str = Path(...),
    scene_offset: int = Query(
        0, ge=0,
        description="0-based offset into the scene timeline.  Use with "
                    "``scene_limit`` to page through long demos.",
    ),
    scene_limit: int = Query(
        _VISUAL_EVIDENCE_PAGE_DEFAULT, ge=1, le=_VISUAL_EVIDENCE_PAGE_MAX,
        description="Maximum scenes returned in this window (max 2000).  "
                    "Controls and edges are scoped to the scenes in this "
                    "window so payload size stays bounded for long demos.",
    ),
    include_storyboard_overlays: bool = Query(
        False,
        description="Phase 1.7 — when True, augment the response with "
                    "storyboard-derived overlays: dedup'd app groupings, "
                    "noise scene flags, LLM short captions, and "
                    "annotated PNG URLs.  Used by VisualFlowDiagramPage "
                    "to render clean app names + filtered noise + "
                    "annotated thumbnails.  Off by default for "
                    "backwards-compat — older clients ignore the new "
                    "fields entirely.",
    ),
    user: dict = Depends(get_current_user),
):
    """Return a windowed slice of the visual evidence graph for a canonical artifact (Phase 7).

    Single endpoint serving both the SessionReplayPage and VisualFlowDiagramPage.
    All four evidence layers are included in one response to avoid waterfall
    requests, but the response is bounded by ``scene_limit`` so a 3-hour
    multi-app demo doesn't serialise 50K rows in one shot.

    For backwards compatibility the default window (500 scenes) covers
    every demo we've shipped to date in a single call; clients that need
    deeper pagination pass ``scene_offset`` + ``scene_limit``.

    Response shape:
        {
            "artifact_id": str,
            "scene_window": {"offset": int, "limit": int, "total": int},
            "flows": [...],                                  # Phase 5b — full set
            "app_instances": [...],                          # Phase 5 — full set
            "scenes": [...],                                 # Phase 3 — page slice
            "controls_by_scene": { "scene_id": [...] },     # Phase 6 — scoped to page slice
            "edges": [...],                                  # Phase 7 — scoped to page slice
            "summary": {...}                                 # computed over the full artifact
        }
    """
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as db:
        artifact = await db.get(CanonicalArtifactRow, artifact_id)
        if not artifact or artifact.tenant_id != tenant_id:
            raise HTTPException(404, f"Artifact {artifact_id} not found")

        # ── Scene window ────────────────────────────────────────
        scene_filter = [
            VisualSceneRow.artifact_id == artifact_id,
            VisualSceneRow.tenant_id == tenant_id,
        ]
        scene_count_q = await db.execute(
            select(func.count()).select_from(VisualSceneRow).where(*scene_filter)
        )
        scene_total = scene_count_q.scalar_one()

        scenes_q = await db.execute(
            select(VisualSceneRow, VisualFrameRow.frame_asset_path)
            .outerjoin(
                VisualFrameRow,
                VisualSceneRow.representative_frame_id == VisualFrameRow.frame_id,
            )
            .where(*scene_filter)
            .order_by(asc(VisualSceneRow.scene_index))
            .offset(scene_offset)
            .limit(scene_limit)
        )
        scenes = []
        window_scene_ids: list[str] = []
        for scene_row, frame_asset_path in scenes_q.all():
            scene_dict = row_to_dict(scene_row)
            scene_dict["representative_frame_asset_path"] = safe_frame_asset_path(
                tenant_id, frame_asset_path,
            )
            scenes.append(scene_dict)
            window_scene_ids.append(scene_dict["scene_id"])

        # ── App instances (small set — always full) ─────────────
        instances_q = await db.execute(
            select(AppInstanceRow)
            .where(
                AppInstanceRow.artifact_id == artifact_id,
                AppInstanceRow.tenant_id == tenant_id,
            )
            .order_by(asc(AppInstanceRow.first_scene_index))
        )
        app_instances = [row_to_dict(r) for r in instances_q.scalars().all()]

        # ── Controls scoped to the windowed scenes ──────────────
        if window_scene_ids:
            controls_q = await db.execute(
                select(EvidenceControlRow)
                .where(
                    EvidenceControlRow.artifact_id == artifact_id,
                    EvidenceControlRow.tenant_id == tenant_id,
                    EvidenceControlRow.scene_id.in_(window_scene_ids),
                )
            )
            controls_flat = [row_to_dict(r) for r in controls_q.scalars().all()]
        else:
            controls_flat = []

        controls_by_scene: dict[str, list] = {}
        for ctrl in controls_flat:
            sid = ctrl.get("scene_id", "")
            controls_by_scene.setdefault(sid, []).append(ctrl)

        # ── Edges scoped to the windowed scenes ─────────────────
        # An edge is included when EITHER endpoint is in the window — that
        # way a window boundary doesn't visually orphan a scene that
        # actually has an incoming/outgoing edge from the next window.
        if window_scene_ids:
            edges_q = await db.execute(
                select(VisualFlowEdgeRow)
                .where(
                    VisualFlowEdgeRow.artifact_id == artifact_id,
                    VisualFlowEdgeRow.tenant_id == tenant_id,
                    (
                        VisualFlowEdgeRow.from_scene_id.in_(window_scene_ids)
                        | VisualFlowEdgeRow.to_scene_id.in_(window_scene_ids)
                    ),
                )
                .order_by(asc(VisualFlowEdgeRow.from_scene_id))
            )
            edges = [row_to_dict(r) for r in edges_q.scalars().all()]
        else:
            edges = []

        # ── Flows (small set — always full) ─────────────────────
        flows_q = await db.execute(
            select(VisualFlowRow)
            .where(
                VisualFlowRow.artifact_id == artifact_id,
                VisualFlowRow.tenant_id == tenant_id,
            )
            .order_by(asc(VisualFlowRow.flow_index))
        )
        flows = [row_to_dict(r) for r in flows_q.scalars().all()]

        # ── Whole-artifact summary metrics ──────────────────────
        # Strong-coverage percentages and avg completeness are computed
        # over the entire artifact (not just the window) so the badge
        # values don't shift as the user pages through.
        edge_total_q = await db.execute(
            select(func.count()).select_from(VisualFlowEdgeRow).where(
                VisualFlowEdgeRow.artifact_id == artifact_id,
                VisualFlowEdgeRow.tenant_id == tenant_id,
            )
        )
        edge_total = edge_total_q.scalar_one()

        action_confirmed_q = await db.execute(
            select(func.count()).select_from(VisualFlowEdgeRow).where(
                VisualFlowEdgeRow.artifact_id == artifact_id,
                VisualFlowEdgeRow.tenant_id == tenant_id,
                VisualFlowEdgeRow.edge_type == "action_confirmed_transition",
            )
        )
        action_confirmed_count = action_confirmed_q.scalar_one()

        automation_ready_q = await db.execute(
            select(func.count()).select_from(EvidenceControlRow).where(
                EvidenceControlRow.artifact_id == artifact_id,
                EvidenceControlRow.tenant_id == tenant_id,
                EvidenceControlRow.automation_ready.is_(True),
            )
        )
        automation_ready_count = automation_ready_q.scalar_one()

        avg_completeness_q = await db.execute(
            select(func.coalesce(func.avg(VisualSceneRow.completeness_confidence), 0.0))
            .where(*scene_filter)
        )
        avg_conf = float(avg_completeness_q.scalar_one() or 0.0)

        strong_scene_q = await db.execute(
            select(func.count()).select_from(VisualSceneRow).where(
                *scene_filter,
                VisualSceneRow.scene_quality.in_(("strong", "degraded")),
            )
        )
        strong_scene_count = strong_scene_q.scalar_one()

        strong_action_q = await db.execute(
            select(func.count()).select_from(VisualFlowEdgeRow).where(
                VisualFlowEdgeRow.artifact_id == artifact_id,
                VisualFlowEdgeRow.tenant_id == tenant_id,
                VisualFlowEdgeRow.action_quality.in_(("strong", "degraded")),
            )
        )
        strong_action_count = strong_action_q.scalar_one()

        strong_scene_pct = (strong_scene_count / scene_total) if scene_total else 0.0
        strong_action_pct = (strong_action_count / edge_total) if edge_total else 0.0
        avg_completeness = float(
            getattr(artifact, "semantic_completeness_score", None) or avg_conf or 0.55
        )
        non_noise_flow_count = sum(1 for f in flows if not f.get("is_noise"))
        graph_visual_e2e_status = _compute_visual_e2e_status(
            scene_count=scene_total,
            edge_count=edge_total,
            flow_count=non_noise_flow_count,
            strong_scene_pct=strong_scene_pct,
            strong_action_pct=strong_action_pct,
            avg_completeness=avg_completeness,
        )

        # ── Phase 1.7 storyboard overlays (opt-in) ──────────────────
        # Fold in the storyboard composer's derived outputs so the 3D
        # Journey tab can render with the same data the Storyboard tab
        # uses (dedup'd app names, noise filtering, LLM captions,
        # annotated PNG URLs).  Skipped entirely when the flag is off
        # so existing callers see the original payload shape.
        storyboard_overlay = _empty_storyboard_overlay()
        if include_storyboard_overlays:
            storyboard_overlay = await _load_storyboard_overlay(
                db,
                artifact_id=artifact_id,
                tenant_id=tenant_id,
                window_scene_ids=window_scene_ids,
            )

        # Phase 1.5 — URL canonicalization.
        #
        # OCR routinely misreads URL bar text (e.g. ``usaa.com`` →
        # ``Msdd.com``), producing scene.detected_url values that don't
        # match the customer's actual application.  The app_deduper has
        # already identified the dominant canonical_domain across the
        # artifact's groupings (weighted by scene_count), so we use
        # that as ground truth for the hostname when the per-scene OCR
        # disagrees.  Path/query/fragment from the OCR'd URL are
        # preserved.
        _canonicalize_scene_urls(
            scenes,
            app_groupings=storyboard_overlay.get("app_groupings") or [],
            scene_to_app_grouping=storyboard_overlay.get("scene_to_app_grouping") or {},
        )

        return {
            "artifact_id": artifact_id,
            "scene_window": {
                "offset": scene_offset,
                "limit": scene_limit,
                "returned": len(scenes),
                "total": scene_total,
            },
            "flows": flows,
            "app_instances": app_instances,
            "scenes": scenes,
            "controls_by_scene": controls_by_scene,
            "edges": edges,
            "visual_e2e_status": graph_visual_e2e_status,
            "summary": {
                "total_flows": len(flows),
                "total_scenes": scene_total,
                "total_edges": edge_total,
                "action_confirmed_edges": action_confirmed_count,
                "automation_ready_controls": automation_ready_count,
                "avg_completeness_confidence": round(avg_conf, 4),
                "scene_state_coverage": round(strong_scene_pct, 3),
                "action_summary_coverage": round(strong_action_pct, 3),
            },
            # Phase 1.7 — empty objects when the flag is off so the
            # response shape stays stable (clients can always read
            # these keys without null-checks; counts are 0).
            "app_groupings": storyboard_overlay["app_groupings"],
            "noise_scene_ids": storyboard_overlay["noise_scene_ids"],
            "scene_captions": storyboard_overlay["scene_captions"],
            "annotated_frame_urls": storyboard_overlay["annotated_frame_urls"],
            "scene_to_app_grouping": storyboard_overlay["scene_to_app_grouping"],
            "scene_actions": storyboard_overlay["scene_actions"],
            # Phase 2 — URL-keyed page visits + their actions + form
            # snapshots.  Always present (possibly empty) so the
            # frontend + test exporters can iterate without null checks.
            "page_visits": storyboard_overlay["page_visits"],
            "page_actions": storyboard_overlay["page_actions"],
            "form_snapshots": storyboard_overlay["form_snapshots"],
        }


@router.get("/api/v1/artifacts/{artifact_id}/flows/{flow_id}")
async def get_flow_detail(
    artifact_id: str = Path(...),
    flow_id: str = Path(...),
    user: dict = Depends(get_current_user),
):
    """Return scenes, edges, and controls scoped to a single flow."""
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as db:
        flow_row = await db.get(VisualFlowRow, flow_id)
        if not flow_row or flow_row.tenant_id != tenant_id or flow_row.artifact_id != artifact_id:
            raise HTTPException(404, f"Flow {flow_id} not found")

        flow = row_to_dict(flow_row)

        scenes_q = await db.execute(
            select(VisualSceneRow, VisualFrameRow.frame_asset_path)
            .outerjoin(
                VisualFrameRow,
                VisualSceneRow.representative_frame_id == VisualFrameRow.frame_id,
            )
            .where(
                VisualSceneRow.artifact_id == artifact_id,
                VisualSceneRow.tenant_id == tenant_id,
                VisualSceneRow.flow_id == flow_id,
            )
            .order_by(asc(VisualSceneRow.scene_index))
        )
        scenes = []
        for scene_row, frame_asset_path in scenes_q.all():
            scene_dict = row_to_dict(scene_row)
            scene_dict["representative_frame_asset_path"] = safe_frame_asset_path(
                tenant_id, frame_asset_path,
            )
            scenes.append(scene_dict)

        scene_ids = {s["scene_id"] for s in scenes}

        edges_q = await db.execute(
            select(VisualFlowEdgeRow)
            .where(
                VisualFlowEdgeRow.artifact_id == artifact_id,
                VisualFlowEdgeRow.tenant_id == tenant_id,
                VisualFlowEdgeRow.flow_id == flow_id,
            )
            .order_by(asc(VisualFlowEdgeRow.from_scene_id))
        )
        edges = [row_to_dict(r) for r in edges_q.scalars().all()]

        # Defensive: drop any edge whose endpoints are not both in this flow's
        # scene set (guards against legacy cross-flow edges in the DB).
        edges = [e for e in edges if e.get("from_scene_id") in scene_ids and e.get("to_scene_id") in scene_ids]

        if scene_ids:
            controls_q = await db.execute(
                select(EvidenceControlRow)
                .where(
                    EvidenceControlRow.artifact_id == artifact_id,
                    EvidenceControlRow.tenant_id == tenant_id,
                    EvidenceControlRow.scene_id.in_(scene_ids),
                )
            )
            controls_flat = [row_to_dict(r) for r in controls_q.scalars().all()]
        else:
            controls_flat = []
        controls_by_scene: dict[str, list] = {}
        for ctrl in controls_flat:
            sid = ctrl.get("scene_id", "")
            controls_by_scene.setdefault(sid, []).append(ctrl)

        return {
            "flow": flow,
            "scenes": scenes,
            "edges": edges,
            "controls_by_scene": controls_by_scene,
        }


# ─── Session → Artifacts ─────────────────────────────────────

@router.get("/api/v1/sessions/{session_id}/artifacts")
async def list_session_artifacts(
    session_id: str = Path(...),
    request: Request = None,
    user: dict = Depends(get_current_user),
):
    tenant_id = user["tenant_id"]
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
    request: Request = None,
    user: dict = Depends(get_current_user),
):
    tenant_id = user["tenant_id"]
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
    user: dict = Depends(get_current_user),
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

    _headers: dict[str, str] = {}
    if request:
        _auth = request.headers.get("authorization")
        if _auth:
            _headers["Authorization"] = _auth

    # P2 fallback A — legacy chain orchestrator (workflow_instances).
    try:
        import httpx
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                f"{_ORCHESTRATOR_URL}/api/v1/orchestrator/workflows/{workflow_id}",
                headers=_headers,
            )
            if resp.status_code == 200:
                _logger.info(
                    "P2 fallback (legacy): workflow %s served from orchestrator",
                    workflow_id,
                )
                return resp.json()
    except Exception as exc:
        _logger.debug("P2 legacy fallback failed for workflow %s: %s", workflow_id, exc)

    # Phase A fallback B — new Phase 12 plane (workflow_state). Every
    # canonical upload now dispatches there, so this is the primary
    # source of truth for in-flight workflow status. The new plane's
    # /api/v1/canonical-workflows/{id} returns a different shape than
    # the legacy chain; we normalize it here so the UI sees the same
    # keys regardless of which plane handled the request.
    try:
        import httpx
        async with httpx.AsyncClient(timeout=5.0) as client:
            state_resp = await client.get(
                f"{_ORCHESTRATOR_URL}/api/v1/canonical-workflows/{workflow_id}",
                headers=_headers,
            )
            # Pass through 403/404 distinctly. The previous code
            # collapsed both into 404, so users hitting a cross-tenant
            # workflow saw "Workflow not found" and assumed the ID was
            # wrong. The UI's catch block now branches on status to
            # show the right message.
            if state_resp.status_code == 403:
                raise HTTPException(
                    403,
                    f"Workflow {workflow_id}: access denied "
                    "(belongs to another tenant)",
                )
            if state_resp.status_code != 200:
                raise HTTPException(404, f"Workflow {workflow_id} not found")
            state = state_resp.json()

            # DAG state is authoritative for stage status. step_history
            # provides duration_ms but its status column can lag or be
            # contaminated when parallel steps complete close in time.
            # Build stages from the plan order (so all steps appear in
            # the correct sequence), overlay status from DAG sets, then
            # enrich with timing from history when available.
            dag_completed = set(state.get("dag_completed_steps") or [])
            dag_in_flight = set(state.get("dag_in_flight_steps") or [])
            plan_steps = state.get("plan_steps") or []
            current_step = state.get("current_step") or ""

            stages: dict[str, dict] = {}
            for ps in plan_steps:
                full_name = ps.get("name", "")
                short = full_name.split(".", 1)[-1] if "." in full_name else full_name
                if full_name in dag_completed:
                    status = "completed"
                elif full_name in dag_in_flight or full_name == current_step:
                    status = "running"
                else:
                    status = "pending"
                stages[short] = {
                    "stage_id": short,
                    "step_name": full_name,
                    "engine": ps.get("engine", ""),
                    "status": status,
                    "started_at": None,
                    "completed_at": None,
                    "duration_ms": None,
                    "error": None,
                }

            # Overlay timing data from step_history (best-effort —
            # history's status field is unreliable in parallel DAGs but
            # its duration_ms is fine when the row was correctly closed).
            try:
                hist_resp = await client.get(
                    f"{_ORCHESTRATOR_URL}/api/v1/canonical-workflows/{workflow_id}/history",
                    headers=_headers,
                )
                if hist_resp.status_code == 200:
                    # Keep the LATEST attempt per step_name (history is
                    # ordered oldest-first, so a simple overwrite wins).
                    for h in (hist_resp.json().get("steps") or []):
                        name = h.get("step_name", "")
                        short = name.split(".", 1)[-1] if "." in name else name
                        if short not in stages:
                            continue
                        stages[short]["started_at"] = h.get("started_at")
                        stages[short]["completed_at"] = h.get("completed_at")
                        stages[short]["duration_ms"] = h.get("duration_ms")
                        if h.get("error"):
                            stages[short]["error"] = h.get("error")
                        stages[short]["attempt"] = h.get("attempt", 0)
            except Exception as hist_exc:
                _logger.debug(
                    "Phase A fallback: step history fetch failed for wf=%s err=%s",
                    workflow_id, hist_exc,
                )

            # Map plane status → UI status vocabulary the existing
            # SessionCommandPage logic understands.
            plane_status = state.get("status", "")
            status_map = {
                "pending": "running",
                "running": "running",
                "completed": "completed",
                "failed": "failed",
                "cancelled": "cancelled",
                "quarantined": "failed",  # dead-letter is a failure from the UI's POV
            }
            ui_status = status_map.get(plane_status, plane_status)

            # Progress = % of plan steps the DAG marked completed.
            completed_steps = sum(
                1 for s in stages.values() if s.get("status") in ("completed", "skipped")
            )
            total_steps = max(len(stages), 1)
            progress_pct = min(100, max(0, int(round(
                100.0 * completed_steps / total_steps
            ))))

            _logger.info(
                "Phase A fallback: workflow %s served from new plane (status=%s)",
                workflow_id, plane_status,
            )
            return {
                "workflow_id": state.get("workflow_id"),
                "chain_id": "canonical_pipeline",
                "chain_name": "Canonical Pipeline (Phase 12 DAG)",
                "status": ui_status,
                "current_stage": state.get("current_step") or "",
                "progress_percent": progress_pct,
                "stages": stages,
                "error": state.get("error") or None,
                "created_at": state.get("created_at"),
                "completed_at": state.get("completed_at"),
                "deadline_at": state.get("deadline_at"),
                "kind": state.get("kind"),
                "attempt": state.get("attempt", 0),
                "step_index": state.get("step_index", 0),
            }
    except HTTPException:
        raise
    except Exception as exc:
        _logger.debug(
            "Phase A new-plane fallback failed for workflow %s: %s",
            workflow_id, exc,
        )

    raise HTTPException(404, f"Workflow {workflow_id} not found")


# D6g: map Phase-12 DAG step names → the UI's 7 canonical-stage IDs
# (CanonicalResultPage.CANONICAL_STAGES). The UI's getTimelineStage()
# does substring matching against `event`, so emitting events keyed
# off the UI stage id makes the Processing Timeline render correctly.
_DAG_STEP_TO_UI_STAGE: dict[str, str] = {
    "spine.probe_media": "media_probe",
    "ears.preprocess": "audio_transcription",
    "ears.diarize": "audio_transcription",
    "ears.transcribe_segments": "audio_transcription",
    "ears.align": "audio_transcription",
    "eyes.extract_frames": "visual_extraction",
    "eyes.detect_scenes": "visual_extraction",
    "eyes.ocr_frames": "visual_extraction",
    "eyes.analyze_scenes": "visual_extraction",
    "eyes.analyze_transitions": "visual_extraction",
    "eyes.build_evidence": "visual_extraction",
    "shield.redact_audio": "pii_redaction",
    "shield.redact_video": "pii_redaction",
    "shield.redact_text": "pii_redaction",
    "spine.build_visual_graph": "visual_graph_assembly",
    "spine.persist_minimal_artifact": "artifact_persistence",
    "spine.update_artifact_enriched": "artifact_persistence",
    "spine.persist_visual_evidence": "artifact_persistence",
    "spine.persist_action_evidence": "artifact_persistence",
    "backbone.canonicalize_multimodal": "canonical_quality_gate",
}


def _build_timeline_from_step_history(rows: list) -> list[dict]:
    """Aggregate raw workflow_step_history rows into the UI-shaped
    timeline event stream. Each UI stage gets one `*.start` event
    (earliest start across its sub-steps) and one `*.complete` event
    (latest complete). Degraded/failed sub-steps roll up to the
    parent's status. Without this aggregation, the UI's stage panel
    is blank — its substring matcher only knows the 7 canonical
    stage ids, not the 20+ raw DAG step names.
    """
    if not rows:
        return []

    by_stage: dict[str, dict] = {}
    for row in rows:
        ui_stage = _DAG_STEP_TO_UI_STAGE.get(row.step_name)
        if not ui_stage:
            continue
        bucket = by_stage.setdefault(
            ui_stage,
            {"start": None, "end": None, "status": "completed", "failed": False, "degraded": False},
        )
        if row.started_at and (bucket["start"] is None or row.started_at < bucket["start"]):
            bucket["start"] = row.started_at
        if row.completed_at and (bucket["end"] is None or row.completed_at > bucket["end"]):
            bucket["end"] = row.completed_at
        # The final-attempt status wins for a step; sub-steps with
        # status=="failed" propagate as degraded at the UI-stage level
        # (the workflow as a whole only fails if no fallback recovered).
        if row.status == "failed":
            bucket["degraded"] = True
        elif row.status not in ("completed", "skipped"):
            bucket["failed"] = bucket["failed"] or False

    timeline: list[dict] = []
    for ui_stage, info in by_stage.items():
        if info["start"]:
            timeline.append({
                "timestamp": info["start"].isoformat(),
                "event": f"{ui_stage}.start",
                "detail": f"stage: {ui_stage} started",
            })
        if info["end"]:
            label = "degraded" if info["degraded"] else (
                "failed" if info["failed"] else "complete"
            )
            timeline.append({
                "timestamp": info["end"].isoformat(),
                "event": f"{ui_stage}.{label}",
                "detail": f"stage: {ui_stage} {label}",
            })
    timeline.sort(key=lambda e: e["timestamp"])
    return timeline


@router.get("/api/v1/workflows/{workflow_id}/timeline")
async def get_workflow_timeline(
    workflow_id: str = Path(...),
    request: Request = None,
    user: dict = Depends(get_current_user),
):
    """Get the execution timeline for a workflow.

    Lookup order:
      1. Legacy `workflow_instances` row (carries pre-baked timeline JSON)
      2. Phase-12 plane (`workflow_state` + `workflow_step_history`)
      3. Orchestrator in-flight fallback

    D6g (2026-05-18): added step #2 — without it, the UI's Processing
    Timeline section was blank for every Phase-12 workflow because the
    legacy table lookup missed and the orchestrator endpoint also
    doesn't know Phase-12 plane workflows.
    """
    factory = require_db()
    async with factory() as db:
        # 1. Legacy table
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

        # 2. Phase-12 plane: assemble from workflow_state + step_history
        try:
            from nexus_sdk.workflows.db_models import (
                WorkflowStateRow as _WfStateRow,
                WorkflowStepHistoryRow as _WfStepHistRow,
            )
            from sqlalchemy import select
            wf_state = await db.get(_WfStateRow, workflow_id)
            if wf_state:
                step_rows = (
                    await db.execute(
                        select(_WfStepHistRow)
                        .where(_WfStepHistRow.workflow_id == workflow_id)
                        .order_by(_WfStepHistRow.started_at.asc())
                    )
                ).scalars().all()
                _timeline = _build_timeline_from_step_history(list(step_rows))
                return {
                    "workflow_id": wf_state.workflow_id,
                    "chain_name": wf_state.kind,
                    "status": wf_state.status,
                    "timeline": _timeline,
                    "started_at": wf_state.created_at,
                    "completed_at": wf_state.completed_at,
                }
        except Exception as exc:
            _logger.warning(
                "timeline.phase12_lookup_failed wf=%s err=%s",
                workflow_id, exc,
            )

    # 3. Orchestrator in-flight fallback
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
