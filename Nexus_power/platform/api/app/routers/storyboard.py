"""HTTP endpoints for the picture-first storyboard.

Three endpoints land here:

* ``GET /api/v1/artifacts/{artifact_id}/storyboard``
    Returns the composed storyboard for one artifact.  Triggers lazy
    derivation of any missing or stale Phase 1 outputs (scene
    grouping, app dedup, captions, annotated frames).
* ``GET /api/v1/artifacts/{artifact_id}/frames/{frame_id}/annotated.png``
    Streams an annotated PNG/JPEG for a single representative frame.
    Falls back to the raw frame on render failure so the client
    always gets a usable image.
* ``POST /api/v1/artifacts/{artifact_id}/storyboard/regenerate``
    Forces a full re-derivation regardless of version state.  Used
    by operators after a version bump.

All endpoints require JWT authentication, enforce tenant isolation
via the ``tenant_scoped_session`` helper (which sets the
``nexus.current_tenant_id`` Postgres RLS variable), and never expose
LLM API keys or upstream provider errors to the client.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from ..auth import get_current_user
from ..database import tenant_scoped_session

router = APIRouter(tags=["Storyboard"])
_logger = logging.getLogger(__name__)


# ── Pydantic response shapes ─────────────────────────────────────────────────


class AppGroupingResponse(BaseModel):
    grouping_id: str
    display_label: str
    canonical_name: str
    canonical_domain: str
    app_type: str
    scene_count: int
    first_scene_index: int
    last_scene_index: int
    confidence: float
    dedup_basis: str


class PanelResponse(BaseModel):
    panel_id: str
    panel_index: int
    scene_count: int
    in_scene_action_count: int
    start_ms: int
    end_ms: int
    duration_ms: int
    caption_short: str
    caption_long: str
    caption_quality: str
    panel_quality: str
    completeness_confidence: float
    is_noise: bool
    noise_reason: str
    representative_frame_id: str | None
    representative_frame_path: str
    annotated_frame_url: str | None = Field(
        default=None,
        description="URL to GET the annotated PNG. Null when no representative frame.",
    )
    annotated_frame_available: bool
    app: AppGroupingResponse | None


class DerivationResponse(BaseModel):
    scene_grouper_version: str
    app_deduper_version: str
    caption_rewriter_version: str
    frame_annotator_version: str
    ran_scene_grouper: bool
    ran_app_deduper: bool
    ran_caption_rewriter: bool
    ran_frame_annotator: bool
    derivation_elapsed_ms: int
    storyboard_total_ms: int


class StoryboardPayload(BaseModel):
    artifact_id: str
    visual_e2e_status: str | None
    panels: list[PanelResponse]
    apps: list[AppGroupingResponse]
    summary: dict[str, Any]
    derivation: DerivationResponse


# ── Helpers ──────────────────────────────────────────────────────────────────


def _composer_from_request(request: Request):
    composer = getattr(request.app.state, "storyboard_composer", None)
    if composer is None:
        raise HTTPException(
            status_code=503,
            detail="storyboard subsystem not initialised — restart platform-api",
        )
    return composer


def _bearer_token(request: Request) -> str:
    """Extract the raw JWT from the Authorization header.

    Forwarded to eyes-engine on service-to-service frame fetches so
    that eyes can enforce the same tenant isolation the user request
    already passed.  Returns ``""`` when no token is present.
    """
    raw = request.headers.get("authorization") or ""
    if raw.lower().startswith("bearer "):
        return raw[len("bearer "):].strip()
    return raw.strip()


def _frame_annotator_from_request(request: Request):
    annotator = getattr(request.app.state, "frame_annotator", None)
    if annotator is None:
        raise HTTPException(
            status_code=503,
            detail="frame annotator not initialised — restart platform-api",
        )
    if not annotator.pil_available:
        raise HTTPException(
            status_code=503,
            detail="Pillow not installed in platform-api image",
        )
    return annotator


def _panel_to_response(panel, artifact_id: str) -> PanelResponse:
    app = panel.app
    app_response = (
        AppGroupingResponse(
            grouping_id=app.grouping_id,
            display_label=app.display_label,
            canonical_name=app.canonical_name,
            canonical_domain=app.canonical_domain,
            app_type=app.app_type,
            scene_count=app.scene_count,
            first_scene_index=app.first_scene_index,
            last_scene_index=app.last_scene_index,
            confidence=app.confidence,
            dedup_basis=app.dedup_basis,
        )
        if app
        else None
    )

    annotated_url: str | None = None
    if panel.representative_frame_id:
        annotated_url = (
            f"/api/v1/artifacts/{artifact_id}/frames/"
            f"{panel.representative_frame_id}/annotated.png"
        )

    return PanelResponse(
        panel_id=panel.panel_id,
        panel_index=panel.panel_index,
        scene_count=panel.scene_count,
        in_scene_action_count=panel.in_scene_action_count,
        start_ms=panel.start_ms,
        end_ms=panel.end_ms,
        duration_ms=panel.duration_ms,
        caption_short=panel.caption_short,
        caption_long=panel.caption_long,
        caption_quality=panel.caption_quality,
        panel_quality=panel.panel_quality,
        completeness_confidence=panel.completeness_confidence,
        is_noise=panel.is_noise,
        noise_reason=panel.noise_reason,
        representative_frame_id=panel.representative_frame_id,
        representative_frame_path=panel.representative_frame_path,
        annotated_frame_url=annotated_url,
        annotated_frame_available=panel.annotated_frame_available,
        app=app_response,
    )


# ── Routes ───────────────────────────────────────────────────────────────────


@router.get(
    "/api/v1/artifacts/{artifact_id}/storyboard",
    response_model=StoryboardPayload,
    summary="Get the picture-first storyboard for an artifact",
)
async def get_storyboard(
    request: Request,
    artifact_id: str = Path(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
) -> StoryboardPayload:
    composer = _composer_from_request(request)
    tenant_id = user["tenant_id"]
    token = _bearer_token(request)
    async with tenant_scoped_session(tenant_id) as session:
        response = await composer.get_storyboard(
            session,
            artifact_id=artifact_id,
            tenant_id=tenant_id,
            force_regenerate=False,
            auth_token=token,
        )
    if response is None:
        raise HTTPException(status_code=404, detail="artifact not found")

    return StoryboardPayload(
        artifact_id=response.artifact_id,
        visual_e2e_status=response.visual_e2e_status,
        panels=[_panel_to_response(p, artifact_id) for p in response.panels],
        apps=[
            AppGroupingResponse(
                grouping_id=a.grouping_id,
                display_label=a.display_label,
                canonical_name=a.canonical_name,
                canonical_domain=a.canonical_domain,
                app_type=a.app_type,
                scene_count=a.scene_count,
                first_scene_index=a.first_scene_index,
                last_scene_index=a.last_scene_index,
                confidence=a.confidence,
                dedup_basis=a.dedup_basis,
            )
            for a in response.apps
        ],
        summary=dict(response.summary),
        derivation=DerivationResponse(
            scene_grouper_version=response.derivation.scene_grouper_version,
            app_deduper_version=response.derivation.app_deduper_version,
            caption_rewriter_version=response.derivation.caption_rewriter_version,
            frame_annotator_version=response.derivation.frame_annotator_version,
            ran_scene_grouper=response.derivation.ran_scene_grouper,
            ran_app_deduper=response.derivation.ran_app_deduper,
            ran_caption_rewriter=response.derivation.ran_caption_rewriter,
            ran_frame_annotator=response.derivation.ran_frame_annotator,
            derivation_elapsed_ms=response.derivation.derivation_elapsed_ms,
            storyboard_total_ms=response.derivation.storyboard_total_ms,
        ),
    )


@router.post(
    "/api/v1/artifacts/{artifact_id}/storyboard/regenerate",
    response_model=StoryboardPayload,
    summary="Force re-derivation of the storyboard regardless of cached version",
)
async def regenerate_storyboard(
    request: Request,
    artifact_id: str = Path(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
) -> StoryboardPayload:
    composer = _composer_from_request(request)
    tenant_id = user["tenant_id"]
    token = _bearer_token(request)
    async with tenant_scoped_session(tenant_id) as session:
        response = await composer.get_storyboard(
            session,
            artifact_id=artifact_id,
            tenant_id=tenant_id,
            force_regenerate=True,
            auth_token=token,
        )
    if response is None:
        raise HTTPException(status_code=404, detail="artifact not found")

    return StoryboardPayload(
        artifact_id=response.artifact_id,
        visual_e2e_status=response.visual_e2e_status,
        panels=[_panel_to_response(p, artifact_id) for p in response.panels],
        apps=[
            AppGroupingResponse(
                grouping_id=a.grouping_id,
                display_label=a.display_label,
                canonical_name=a.canonical_name,
                canonical_domain=a.canonical_domain,
                app_type=a.app_type,
                scene_count=a.scene_count,
                first_scene_index=a.first_scene_index,
                last_scene_index=a.last_scene_index,
                confidence=a.confidence,
                dedup_basis=a.dedup_basis,
            )
            for a in response.apps
        ],
        summary=dict(response.summary),
        derivation=DerivationResponse(
            scene_grouper_version=response.derivation.scene_grouper_version,
            app_deduper_version=response.derivation.app_deduper_version,
            caption_rewriter_version=response.derivation.caption_rewriter_version,
            frame_annotator_version=response.derivation.frame_annotator_version,
            ran_scene_grouper=response.derivation.ran_scene_grouper,
            ran_app_deduper=response.derivation.ran_app_deduper,
            ran_caption_rewriter=response.derivation.ran_caption_rewriter,
            ran_frame_annotator=response.derivation.ran_frame_annotator,
            derivation_elapsed_ms=response.derivation.derivation_elapsed_ms,
            storyboard_total_ms=response.derivation.storyboard_total_ms,
        ),
    )


@router.get(
    "/api/v1/artifacts/{artifact_id}/frames/{frame_id}/annotated.png",
    summary="Stream the annotated PNG/JPEG for one frame",
)
async def get_annotated_frame(
    request: Request,
    artifact_id: str = Path(..., min_length=1, max_length=64),
    frame_id: str = Path(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
) -> Response:
    annotator = _frame_annotator_from_request(request)
    tenant_id = user["tenant_id"]
    token = _bearer_token(request)
    async with tenant_scoped_session(tenant_id) as session:
        annotated = await annotator.annotate_frame(
            session,
            frame_id=frame_id,
            tenant_id=tenant_id,
            auth_token=token,
        )
    if annotated is None:
        raise HTTPException(status_code=404, detail="frame not found")

    headers = {
        "Cache-Control": "private, max-age=3600",
        "X-Annotated-Cached": "true" if annotated.cached else "false",
        "X-Annotation-Width": str(annotated.width),
        "X-Annotation-Height": str(annotated.height),
    }
    return Response(
        content=annotated.asset_bytes,
        media_type=annotated.content_type,
        headers=headers,
    )
