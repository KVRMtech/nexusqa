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

import asyncio

from ..auth import get_current_user
from ..database import tenant_scoped_session
from ..services.storyboard import surface_prefs as _surface_prefs

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
    """Extract the raw JWT from the Authorization header, OR — for
    browser ``<img src=...?token=>`` requests that cannot send an
    Authorization header — from the ``?token=`` query param.

    Forwarded to eyes-engine on service-to-service frame fetches so
    that eyes can enforce the same tenant isolation the user request
    already passed.  Mirrors the query fallback in ``get_current_user``
    / ``jwt_auth_middleware`` so annotated-frame ``<img>`` loads forward
    a valid JWT to eyes (without it eyes rejects the fetch → 404).
    Returns ``""`` when no token is present.
    """
    raw = request.headers.get("authorization") or ""
    if raw.lower().startswith("bearer "):
        return raw[len("bearer "):].strip()
    if raw.strip():
        return raw.strip()
    return (request.query_params.get("token", "") or "").strip()


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
            block=False,  # never block the GET on the slow vision derivation —
                          # derive in the background; client polls `summary.deriving`
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


# ── Surface preferences (cost control) ───────────────────────────────────────
# Toggle Storyboard / 3D Journey / Pages & Forms on/off so a customer only pays
# the vision-LLM cost for what they want to see. Default (no row) = all on.

class SurfacesBody(BaseModel):
    storyboard: bool = Field(True)
    pages_forms: bool = Field(True)
    three_d_journey: bool = Field(True)


@router.get(
    "/api/v1/artifacts/{artifact_id}/surfaces",
    summary="Effective surface toggles for an artifact (override → tenant default → all-on)",
)
async def get_artifact_surfaces(
    artifact_id: str = Path(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        return await _surface_prefs.get_effective(
            session, tenant_id=tenant_id, artifact_id=artifact_id,
        )


@router.put(
    "/api/v1/artifacts/{artifact_id}/surfaces",
    summary="Set the per-artifact surface override; derives newly-enabled surfaces in the background",
)
async def set_artifact_surfaces(
    request: Request,
    body: SurfacesBody,
    artifact_id: str = Path(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    tenant_id = user["tenant_id"]
    token = _bearer_token(request)
    composer = _composer_from_request(request)
    desired = body.model_dump()
    async with tenant_scoped_session(tenant_id) as session:
        before = await _surface_prefs.resolve_surfaces(
            session, tenant_id=tenant_id, artifact_id=artifact_id,
        )
        saved = await _surface_prefs.set_artifact_override(
            session, tenant_id=tenant_id, artifact_id=artifact_id, surfaces=desired,
        )
    # "Turn on → then it calls": if a surface flipped OFF→ON, derive it now
    # (background, off the request path — the client polls summary.deriving).
    newly_on = [s for s in _surface_prefs.SURFACES if saved.get(s) and not before.get(s)]
    if newly_on:
        asyncio.create_task(
            composer._derive_in_background(artifact_id, tenant_id, token, saved)
        )
    return {"surfaces": saved, "deriving": bool(newly_on), "newly_enabled": newly_on}


@router.get(
    "/api/v1/tenant/surfaces",
    summary="The org-wide default surface toggles",
)
async def get_tenant_surfaces(
    user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        eff = await _surface_prefs.get_effective(
            session, tenant_id=tenant_id, artifact_id=_surface_prefs._TENANT_DEFAULT_KEY,
        )
    return {"surfaces": eff["tenant_default"] or {s: True for s in _surface_prefs.SURFACES},
            "is_set": eff["tenant_default"] is not None}


@router.put(
    "/api/v1/tenant/surfaces",
    summary="Set the org-wide default surface toggles (applies to artifacts without an override)",
)
async def set_tenant_surfaces(
    body: SurfacesBody,
    user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        saved = await _surface_prefs.set_tenant_default(
            session, tenant_id=tenant_id, surfaces=body.model_dump(),
        )
    return {"surfaces": saved}


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


# ── Ground-truth capture ingestion (Road B — the Tier-0 overlay) ─────────────


def _redact_value(s: str) -> str:
    """Defense-in-depth PII redaction for one client-supplied ground-truth VALUE.

    Runs the SDK's in-perimeter detector over the captured value and replaces
    every hit with its redaction placeholder — no external-LLM egress. Fails
    OPEN: the recorder already redacts at source, so if the server-side
    detector is unavailable the source-redacted value stands (never
    raw-by-bypass, never a 500 on the ingest path). Shared by BOTH ground-truth
    ingest routes so the stored-value guarantee is identical regardless of
    which route a recorder posts to (QE-Central extension E1 hoisted this from
    the /ground-truth route's closure — behavior unchanged)."""
    if not s:
        return s
    try:
        from nexus_sdk.evidence.pii_detector import detect_pii, redact
        hits = detect_pii(s, None)
        return redact(s, hits) if hits else s
    except Exception:
        # The recorder already redacted at source; if the server detector is
        # unavailable, the source-redacted value stands (never raw-by-bypass).
        return s


class GroundTruthEventIn(BaseModel):
    """One instrumented capture event posted by a recorder / per-modality adapter."""

    timestamp_ms: int = Field(0, ge=0)
    kind: str = Field("navigate", max_length=40)
    url: str = Field("", max_length=2000)
    url_host: str = Field("", max_length=500)
    url_path: str = Field("", max_length=2000)
    url_query: str = Field("", max_length=2000)
    target_label: str = Field("", max_length=500)
    value: str = Field("", max_length=1000)
    target_kind: str = Field("", max_length=40)
    modality: str = Field("web_cdp", max_length=40)


class GroundTruthBody(BaseModel):
    session_id: str = Field("", max_length=64)
    recorder_version: str = Field("v1", max_length=50)
    events: list[GroundTruthEventIn] = Field(default_factory=list, max_length=5000)


@router.post("/api/v1/artifacts/{artifact_id}/ground-truth")
async def ingest_ground_truth(
    body: GroundTruthBody,
    artifact_id: str = Path(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
) -> dict:
    """Ingest an instrumented recorder's ground-truth event sidecar (Road B).

    Defense-in-depth PII redaction: every captured VALUE is re-redacted here with
    the SDK's domain detector — fully IN-PERIMETER, no external-LLM egress (the
    recorder also redacts at source). Idempotently replaces this artifact's prior
    events; ``page_visit_extractor`` consumes them as the Tier-0 overlay on the
    next derivation (``PageVisitSource.GROUND_TRUTH``, confidence 1.0). Generic
    across capture modalities — one shape for web CDP / UIA / HLLAPI / Appium.

    Until ``scripts/apply_ground_truth_events.sql`` is applied the insert fails and
    the video path is unaffected (fail-open at the consumer)."""
    import os
    # #1 fix (never-green-wash + RBAC): this is a CLIENT-WRITABLE source that
    # compiles to PROVEN / confidence-1.0 page-visits and a hard toHaveURL, so it
    # MUST NOT be reachable by a low-privilege user — and instrumented Road-B
    # capture is NOT the product path (video-only), so it stays OFF by default.
    # Enable per-deployment AND only for an admin/manager.
    if os.getenv("NEXUS_GROUND_TRUTH_INGEST_ENABLED", "").strip().lower() not in ("1", "true", "yes", "on"):
        raise HTTPException(status_code=403, detail="Ground-truth ingest is disabled (instrumented capture is not enabled for this deployment).")
    if (user.get("role") or "viewer").lower() not in ("admin", "manager"):
        raise HTTPException(status_code=403, detail="Ground-truth ingest requires an admin or manager role.")
    import uuid as _uuid

    from sqlalchemy import delete

    from nexus_sdk.db.models import GroundTruthEventRow

    tenant_id = user["tenant_id"]

    written = 0
    async with tenant_scoped_session(tenant_id) as session:
        await session.execute(
            delete(GroundTruthEventRow).where(
                GroundTruthEventRow.artifact_id == artifact_id,
                GroundTruthEventRow.tenant_id == tenant_id,
            )
        )
        for i, e in enumerate(body.events):
            session.add(GroundTruthEventRow(
                event_id=str(_uuid.uuid4()),
                artifact_id=artifact_id,
                tenant_id=tenant_id,
                session_id=body.session_id or "",
                sequence_index=i,
                timestamp_ms=int(e.timestamp_ms or 0),
                kind=(e.kind or "navigate")[:40],
                url=e.url or "",
                url_host=e.url_host or "",
                url_path=e.url_path or "",
                url_query=e.url_query or "",
                target_label=e.target_label or "",
                value=_redact_value(e.value or ""),
                target_kind=(e.target_kind or "")[:40],
                modality=(e.modality or "web_cdp")[:40],
                recorder_version=(body.recorder_version or "v1")[:50],
                signals={},
            ))
            written += 1
        await session.commit()
    return {"success": True, "artifact_id": artifact_id, "ingested": written}


@router.get("/api/v1/artifacts/{artifact_id}/ground-truth")
async def list_ground_truth(
    artifact_id: str = Path(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
) -> dict:
    """List the stored (already PII-redacted) ground-truth events for an artifact
    — for verification / debugging. Values are redacted at ingest; raw PII is
    never stored or returned."""
    if (user.get("role") or "viewer").lower() not in ("admin", "manager"):
        raise HTTPException(status_code=403, detail="Ground-truth read requires an admin or manager role.")
    from sqlalchemy import select

    from nexus_sdk.db.models import GroundTruthEventRow

    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        rows = (await session.execute(
            select(GroundTruthEventRow)
            .where(
                GroundTruthEventRow.artifact_id == artifact_id,
                GroundTruthEventRow.tenant_id == tenant_id,
            )
            .order_by(GroundTruthEventRow.sequence_index.asc())
        )).scalars().all()
    return {
        "artifact_id": artifact_id,
        "count": len(rows),
        "events": [
            {
                "sequence_index": r.sequence_index,
                "timestamp_ms": r.timestamp_ms,
                "kind": r.kind,
                "url": r.url,
                "url_path": r.url_path,
                "target_label": r.target_label,
                "value": r.value,
                "target_kind": r.target_kind,
                "modality": r.modality,
            }
            for r in rows
        ],
    }


# ── Phase C: Pages & Forms honest quality scorecard ─────────────────────────

_TRUSTED_VISIT_SOURCES = {"ground_truth", "url_regex", "url_scene", "llm_inferred"}


@router.get("/api/v1/artifacts/{artifact_id}/pages-forms/scorecard")
async def pages_forms_scorecard(
    artifact_id: str = Path(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
) -> dict:
    """Per-artifact Pages & Forms quality self-report, computed ONLY from
    persisted extraction rows (no model calls). Honest by construction: the
    same provenance tiers the pipeline records are what get graded, and every
    deduction names its reason. A weak extraction gets a weak grade -- the
    product never green-washes its own output."""
    from sqlalchemy import select as _sel
    from nexus_sdk.db.models import PageActionRow, PageVisitRow

    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        visits = (await session.execute(
            _sel(PageVisitRow).where(
                PageVisitRow.artifact_id == artifact_id,
                PageVisitRow.tenant_id == tenant_id,
            ).order_by(PageVisitRow.sequence_index.asc())
        )).scalars().all()
        actions = (await session.execute(
            _sel(PageActionRow).where(
                PageActionRow.artifact_id == artifact_id,
                PageActionRow.tenant_id == tenant_id,
            )
        )).scalars().all()

    if not visits:
        return {
            "artifact_id": artifact_id,
            "scorecard_version": "v1",
            "visits": 0,
            "grade": "N/A",
            "reasons": ["no page visits derived for this artifact"],
        }

    src_mix: dict = {}
    hosts: set = set()
    trusted = 0
    forms_nonempty = 0
    conf_sum = 0.0
    for v in visits:
        src = str(v.source or "").strip().lower()
        src_mix[src] = src_mix.get(src, 0) + 1
        if v.canonical_host:
            hosts.add(str(v.canonical_host).strip().lower())
        if src in _TRUSTED_VISIT_SOURCES:
            trusted += 1
        try:
            if v.form_snapshot and len(dict(v.form_snapshot)) > 0:
                forms_nonempty += 1
        except (TypeError, ValueError):
            pass
        conf_sum += float(v.extraction_confidence or 0.0)

    total = len(visits)
    acts_total = len(actions)
    ready = sum(1 for a in actions if bool(a.automation_ready))
    xver = sum(1 for a in actions if "cross-verified" in str(a.reasoning or ""))
    dups = sum(1 for a in actions if "[duplicate:" in str(a.reasoning or ""))
    trusted_pct = round(trusted / total, 3)
    ready_pct = round(ready / acts_total, 3) if acts_total else 0.0
    avg_conf = round(conf_sum / total, 3)
    host_consistent = len(hosts) <= 1

    reasons: list = []
    score = 0
    if host_consistent:
        score += 1
    else:
        reasons.append(
            f"page hosts are inconsistent ({sorted(hosts)}) -- OCR host noise "
            "or a genuine multi-app recording; review the visual flow")
    if trusted_pct >= 0.7:
        score += 1
    else:
        reasons.append(
            f"only {int(trusted_pct * 100)}% of pages have a trusted-tier URL "
            "(instrumented / bar-read / vision-read) -- consider re-recording at "
            "higher resolution or enabling the ground-truth recorder")
    if acts_total and ready_pct >= 0.6:
        score += 1
    elif acts_total:
        reasons.append(
            f"only {int(ready_pct * 100)}% of extracted actions are "
            "automation-ready -- values were not independently corroborated")
    else:
        reasons.append("no user actions were extracted")
    if avg_conf >= 0.75:
        score += 1
    else:
        reasons.append(f"average page-identity confidence is {avg_conf}")

    grade = {4: "A", 3: "B", 2: "C", 1: "D"}.get(score, "F")
    return {
        "artifact_id": artifact_id,
        "scorecard_version": "v1",
        "grade": grade,
        "reasons": reasons or ["all quality gates passed"],
        "visits": total,
        "trusted_visits": trusted,
        "trusted_pct": trusted_pct,
        "host_consistent": host_consistent,
        "hosts": sorted(hosts),
        "source_mix": src_mix,
        "avg_visit_confidence": avg_conf,
        "forms_nonempty": forms_nonempty,
        "actions_total": acts_total,
        "actions_ready": ready,
        "actions_ready_pct": ready_pct,
        "actions_cross_verified": xver,
        "actions_duplicates_demoted": dups,
        "provenance_note": (
            "trusted tiers = ground_truth (instrumented), url_regex (address-bar "
            "OCR), url_scene, llm_inferred (vision address-bar read). Everything "
            "else is pixel-inferred and honestly sub-trusted."),
    }


# ── Phase D: ground-truth Tier-0 event ingest (platform half) ────────────────

_GT_ALLOWED_KINDS = {
    "navigate", "navigation", "click", "type", "select", "check",
    "submit", "open", "close", "scroll",
}
_GT_MAX_BATCH = 1000


# NOTE: distinct from the lenient ``GroundTruthEventIn`` above, which serves the
# sibling /ground-truth route. Both classes were once literally named
# ``GroundTruthEventIn``; because this module runs under
# ``from __future__ import annotations``, Pydantic resolves the string
# annotation ``list[GroundTruthEventIn]`` LAZILY against the module namespace —
# so by resolution time BOTH bodies pointed at this stricter second class. The
# legacy /ground-truth route therefore 422'd every payload missing
# ``sequence_index`` and silently dropped url_host/url_path/url_query, which its
# own model declares. Keeping the names distinct is what makes each route bind
# the model it was written against.
class GroundTruthRecorderEventIn(BaseModel):
    sequence_index: int = Field(..., ge=0, le=1_000_000)
    timestamp_ms: int = Field(..., ge=0)
    kind: str = Field(..., min_length=1, max_length=40)
    url: str = Field("", max_length=2000)
    target_label: str = Field("", max_length=500)
    value: str = Field("", max_length=1000)
    target_kind: str = Field("", max_length=40)
    modality: str = Field("dom", max_length=40)
    recorder_version: str = Field("", max_length=50)
    signals: dict = Field(default_factory=dict)


class GroundTruthBatchIn(BaseModel):
    events: list[GroundTruthRecorderEventIn] = Field(..., min_length=1, max_length=_GT_MAX_BATCH)


@router.post("/api/v1/artifacts/{artifact_id}/ground-truth/events")
async def ingest_ground_truth_events(
    body: GroundTruthBatchIn,
    artifact_id: str = Path(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
) -> dict:
    """Ingest instrumented recorder events for an artifact (Tier-0 ground
    truth). Idempotent: event_id is a deterministic UUID5 of
    (artifact, sequence, kind, timestamp), so replays upsert instead of
    duplicating. The page-visit extractor's ground-truth overlay makes these
    AUTHORITATIVE over every pixel tier on the next derivation -- this is the
    100%-identity path for tenants who install the recorder."""
    import os

    # QE-Central extension E1 (privilege + PII parity with the sibling
    # /ground-truth route): this too is a CLIENT-WRITABLE source that becomes
    # AUTHORITATIVE page identity (confidence-1.0 overlay), so it carries the
    # SAME deployment env gate + admin/manager role gate the sibling has.
    # Default (flag unset) => 403 => byte-identical behavior for every
    # existing deployment — the video product path never calls this route.
    if os.getenv("NEXUS_GROUND_TRUTH_INGEST_ENABLED", "").strip().lower() not in ("1", "true", "yes", "on"):
        raise HTTPException(status_code=403, detail="Ground-truth ingest is disabled (instrumented capture is not enabled for this deployment).")
    if (user.get("role") or "viewer").lower() not in ("admin", "manager"):
        raise HTTPException(status_code=403, detail="Ground-truth ingest requires an admin or manager role.")
    import uuid as _uuid
    from datetime import datetime, timezone
    from urllib.parse import urlparse
    from sqlalchemy import select as _sel
    from nexus_sdk.db.models import CanonicalArtifactRow, GroundTruthEventRow

    tenant_id = user["tenant_id"]
    bad = [e.kind for e in body.events
           if e.kind.strip().lower() not in _GT_ALLOWED_KINDS]
    if bad:
        raise HTTPException(
            status_code=422,
            detail=f"unsupported event kind(s): {sorted(set(bad))[:5]} -- "
                   f"allowed: {sorted(_GT_ALLOWED_KINDS)}")

    async with tenant_scoped_session(tenant_id) as session:
        art = (await session.execute(
            _sel(CanonicalArtifactRow.artifact_id).where(
                CanonicalArtifactRow.artifact_id == artifact_id,
                CanonicalArtifactRow.tenant_id == tenant_id,
            )
        )).scalar()
        if not art:
            raise HTTPException(status_code=404, detail="artifact not found")
        session_id = (await session.execute(
            _sel(CanonicalArtifactRow.session_id).where(
                CanonicalArtifactRow.artifact_id == artifact_id,
                CanonicalArtifactRow.tenant_id == tenant_id,
            )
        )).scalar() or ""

        ingested = 0
        for e in body.events:
            parsed = urlparse(e.url if "://" in e.url else ("https://" + e.url if e.url else ""))
            row = GroundTruthEventRow(
                event_id=str(_uuid.uuid5(
                    _uuid.NAMESPACE_URL,
                    f"nexus-gt:{artifact_id}:{e.sequence_index}:{e.kind}:{e.timestamp_ms}",
                )),
                artifact_id=artifact_id,
                tenant_id=tenant_id,
                session_id=str(session_id),
                sequence_index=e.sequence_index,
                timestamp_ms=e.timestamp_ms,
                kind=e.kind.strip().lower(),
                url=e.url,
                url_host=(parsed.hostname or "").lower(),
                url_path=parsed.path or "",
                url_query=parsed.query or "",
                target_label=e.target_label,
                value=_redact_value(e.value or ""),
                target_kind=e.target_kind,
                modality=e.modality,
                recorder_version=e.recorder_version,
                signals=dict(e.signals or {}),
                created_at=datetime.now(timezone.utc),
            )
            await session.merge(row)
            ingested += 1
        await session.commit()

        total = len((await session.execute(
            _sel(GroundTruthEventRow.event_id).where(
                GroundTruthEventRow.artifact_id == artifact_id,
                GroundTruthEventRow.tenant_id == tenant_id,
            )
        )).scalars().all())

    return {
        "artifact_id": artifact_id,
        "ingested": ingested,
        "total_events_for_artifact": total,
        "note": ("events become AUTHORITATIVE page identity on the next "
                 "storyboard derivation (regenerate)"),
    }


@router.get("/api/v1/artifacts/{artifact_id}/ground-truth/events")
async def list_ground_truth_events(
    artifact_id: str = Path(..., min_length=1, max_length=64),
    user: dict = Depends(get_current_user),
) -> dict:
    from sqlalchemy import select as _sel
    from nexus_sdk.db.models import GroundTruthEventRow

    tenant_id = user["tenant_id"]
    async with tenant_scoped_session(tenant_id) as session:
        rows = (await session.execute(
            _sel(GroundTruthEventRow).where(
                GroundTruthEventRow.artifact_id == artifact_id,
                GroundTruthEventRow.tenant_id == tenant_id,
            ).order_by(GroundTruthEventRow.sequence_index.asc()).limit(200)
        )).scalars().all()
    return {
        "artifact_id": artifact_id,
        "count": len(rows),
        "events": [
            {
                "sequence_index": r.sequence_index,
                "timestamp_ms": r.timestamp_ms,
                "kind": r.kind,
                "url": r.url,
                "target_label": r.target_label,
                "value": r.value,
                "modality": r.modality,
            }
            for r in rows
        ],
    }
