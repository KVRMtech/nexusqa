"""Platform API — Action layer endpoints.

Exposes:

  POST /api/v1/actions/sandbox/run          — invoke a Legs scenario
  GET  /api/v1/actions/sandbox/{id}         — fetch invocation status

  POST /api/v1/actions/tours                — generate + save a synthesized tour
  GET  /api/v1/actions/tours                — list tours
  GET  /api/v1/actions/tours/{id}           — fetch a tour
  POST /api/v1/actions/tours/{id}/publish   — publish (status='published')
  POST /api/v1/actions/tours/{id}/archive   — archive

  POST /api/v1/actions/impact               — compute + save an impact analysis
  GET  /api/v1/actions/impact               — list past analyses
  GET  /api/v1/actions/impact/{id}          — fetch one

  GET  /api/v1/actions/invocations          — audit list (filterable by kind)

The router is the only writer for ``synthesized_tours`` and
``impact_analyses``; the sandbox path defers to ``SandboxRunner`` which
in turn writes the invocation row.

Wiring contract: ``request.app.state.sandbox_runner`` must be set by
the platform-API startup when the Legs URL is configured. If not, the
sandbox endpoints return 503; tour + impact endpoints work without it.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from ..actions import (
    ActionRepository,
    ImpactAnalyzer,
    ImpactAnalyzerConfig,
    SandboxQuotaExceeded,
    SandboxRequest,
    SandboxRunner,
    TourComposer,
    TourComposerConfig,
)
from ..auth import get_current_user
from ..database import require_db, get_session_factory

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Actions"], prefix="/api/v1/actions")


_md = sa.MetaData()


atlas_nodes = sa.Table(
    "atlas_nodes",
    _md,
    sa.Column("atlas_node_id", sa.String(64), primary_key=True),
    sa.Column("tenant_id", sa.String(64), nullable=False),
    sa.Column("product_id", sa.String(64), nullable=False),
    sa.Column("backbone_node_id", sa.String(64), nullable=False),
    sa.Column("node_type", sa.String(64), nullable=False),
    sa.Column("layer", sa.String(16), nullable=False),
    sa.Column("label", sa.String(512), nullable=False),
    sa.Column("source_session_ids", ARRAY(sa.String(64)), nullable=False),
    sa.Column("source_artifact_ids", ARRAY(sa.String(64)), nullable=False),
    sa.Column("source_segment_ids", ARRAY(sa.String(64)), nullable=False),
    sa.Column("confidence", sa.Float, nullable=False),
    sa.Column("metadata_json", JSONB, nullable=False),
)


# ── Helpers ────────────────────────────────────────────────────


_PRIVILEGED = frozenset({"admin", "manager", "api"})


def _require_priv(user: dict) -> None:
    if user.get("role", "viewer") not in _PRIVILEGED:
        raise HTTPException(403, "admin, manager, or api role required")


async def _set_tenant(session: AsyncSession, tenant_id: str) -> None:
    await session.execute(
        sa.text("SELECT set_config('nexus.current_tenant_id', :tid, true)"),
        {"tid": tenant_id},
    )


def _action_repo() -> ActionRepository:
    sf = get_session_factory()
    if sf is None:
        raise HTTPException(503, "database not connected")
    return ActionRepository(sf)


def _sandbox_runner(request: Request) -> SandboxRunner:
    runner = getattr(request.app.state, "sandbox_runner", None)
    if runner is None:
        raise HTTPException(503, "sandbox_runner_not_configured")
    return runner


# ── DTOs ───────────────────────────────────────────────────────


class SandboxRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scenario_id: str = Field(min_length=1, max_length=128)
    params: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: int = Field(default=60, ge=5, le=600)
    label: Optional[str] = Field(default=None, max_length=256)
    idempotency_key: Optional[str] = Field(default=None, max_length=128)
    trigger_dispatch_id: Optional[str] = Field(default=None, max_length=64)


class SandboxRunResponse(BaseModel):
    invocation_id: str
    status: str
    legs_run_id: Optional[str] = None
    output: dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None
    latency_ms: int


class ComposeTourRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_id: str = Field(min_length=1, max_length=64)
    title: Optional[str] = Field(default=None, max_length=256)
    persona: Optional[str] = Field(default=None, max_length=64)
    target_minutes: Optional[int] = Field(default=None, ge=1, le=240)
    publish: bool = False


class TourSegmentOut(BaseModel):
    atlas_node_id: str
    label: str
    layer: str
    segment_ids: list[str]
    speaker_id: Optional[str] = None
    estimated_seconds: int
    ordinal: int


class TourOut(BaseModel):
    tour_id: str
    product_id: str
    title: str
    persona: Optional[str] = None
    target_minutes: Optional[int] = None
    playlist: list[TourSegmentOut]
    coverage: dict[str, Any]
    atlas_node_ids: list[str]
    status: str
    created_by: Optional[str] = None
    created_at: str
    updated_at: str


class ImpactAnalyzeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_id: str = Field(min_length=1, max_length=64)
    root_atlas_node_id: str = Field(min_length=1, max_length=64)
    change_description: Optional[str] = Field(default=None, max_length=4000)
    max_depth: Optional[int] = Field(default=None, ge=1, le=5)
    include_rejected: bool = False
    save: bool = True
    expires_in_hours: Optional[int] = Field(default=24, ge=1, le=720)


class ImpactReportOut(BaseModel):
    analysis_id: Optional[str] = None
    product_id: str
    root_atlas_node_id: str
    root_label: str
    root_layer: str
    downstream: list[dict[str, Any]]
    upstream: list[dict[str, Any]]
    layer_summary: dict[str, dict[str, Any]]
    estimated_blast_radius: int
    truncated: bool
    created_at: Optional[str] = None


class InvocationOut(BaseModel):
    invocation_id: str
    kind: str
    status: str
    request: dict[str, Any]
    result: dict[str, Any]
    error: Optional[str] = None
    latency_ms: Optional[int] = None
    trigger_dispatch_id: Optional[str] = None
    trigger_user_id: Optional[str] = None
    created_at: str
    started_at: Optional[str] = None
    completed_at: Optional[str] = None


# ── Mappers ────────────────────────────────────────────────────


def _tour_to_out(row) -> TourOut:
    playlist_raw = row.get("playlist") or []
    if not isinstance(playlist_raw, list):
        playlist_raw = []
    return TourOut(
        tour_id=row["tour_id"],
        product_id=row["product_id"],
        title=row["title"],
        persona=row.get("persona"),
        target_minutes=row.get("target_minutes"),
        playlist=[
            TourSegmentOut(
                atlas_node_id=str(seg.get("atlas_node_id") or ""),
                label=str(seg.get("label") or ""),
                layer=str(seg.get("layer") or ""),
                segment_ids=list(seg.get("segment_ids") or []),
                speaker_id=seg.get("speaker_id"),
                estimated_seconds=int(seg.get("estimated_seconds") or 30),
                ordinal=int(seg.get("ordinal") or 0),
            )
            for seg in playlist_raw
            if isinstance(seg, dict)
        ],
        coverage=dict(row.get("coverage") or {}),
        atlas_node_ids=list(row.get("atlas_node_ids") or []),
        status=row["status"],
        created_by=row.get("created_by"),
        created_at=row["created_at"].isoformat(),
        updated_at=row["updated_at"].isoformat(),
    )


def _invocation_to_out(row) -> InvocationOut:
    return InvocationOut(
        invocation_id=row["invocation_id"],
        kind=row["kind"],
        status=row["status"],
        request=dict(row.get("request") or {}),
        result=dict(row.get("result") or {}),
        error=row.get("error"),
        latency_ms=row.get("latency_ms"),
        trigger_dispatch_id=row.get("trigger_dispatch_id"),
        trigger_user_id=row.get("trigger_user_id"),
        created_at=row["created_at"].isoformat(),
        started_at=(
            row["started_at"].isoformat() if row.get("started_at") else None
        ),
        completed_at=(
            row["completed_at"].isoformat()
            if row.get("completed_at")
            else None
        ),
    )


def _impact_row_to_out(row) -> ImpactReportOut:
    return ImpactReportOut(
        analysis_id=row.get("analysis_id"),
        product_id=row["product_id"],
        root_atlas_node_id=row["root_atlas_node_id"],
        root_label=str(
            (row.get("layer_summary") or {}).get("__root_label", "")
        ),
        root_layer=str(
            (row.get("layer_summary") or {}).get("__root_layer", "")
        ),
        downstream=list(row.get("downstream") or []),
        upstream=list(row.get("upstream") or []),
        layer_summary={
            k: v
            for k, v in (row.get("layer_summary") or {}).items()
            if not k.startswith("__")
        },
        estimated_blast_radius=int(row.get("estimated_blast_radius") or 0),
        truncated=bool((row.get("layer_summary") or {}).get("__truncated", False)),
        created_at=(
            row["created_at"].isoformat() if row.get("created_at") else None
        ),
    )


# ── Sandbox endpoints ──────────────────────────────────────────


@router.post("/sandbox/run", response_model=SandboxRunResponse)
async def run_sandbox(
    body: SandboxRunRequest,
    request: Request,
    user: dict = Depends(get_current_user),
) -> SandboxRunResponse:
    _require_priv(user)
    runner = _sandbox_runner(request)
    sandbox_req = SandboxRequest(
        scenario_id=body.scenario_id,
        params=body.params,
        timeout_seconds=body.timeout_seconds,
        label=body.label,
        idempotency_key=body.idempotency_key,
    )
    try:
        result = await runner.run(
            tenant_id=user["tenant_id"],
            request=sandbox_req,
            trigger_dispatch_id=body.trigger_dispatch_id,
            trigger_user_id=user.get("user_id"),
        )
    except SandboxQuotaExceeded as exc:
        raise HTTPException(
            status_code=429,
            detail={
                "code": "quota_exceeded",
                "used": exc.used,
                "limit": exc.limit,
            },
        )
    return SandboxRunResponse(
        invocation_id=result.invocation_id,
        status=result.status,
        legs_run_id=result.legs_run_id,
        output=result.output,
        error=result.error,
        latency_ms=result.latency_ms,
    )


@router.get("/sandbox/{invocation_id}", response_model=InvocationOut)
async def get_sandbox_invocation(
    invocation_id: str,
    user: dict = Depends(get_current_user),
) -> InvocationOut:
    row = await _action_repo().get_invocation(
        tenant_id=user["tenant_id"], invocation_id=invocation_id
    )
    if row is None or row["kind"] != "sandbox_run":
        raise HTTPException(404, "invocation_not_found")
    return _invocation_to_out(row)


# ── Tour endpoints ─────────────────────────────────────────────


@router.post("/tours", response_model=TourOut, status_code=201)
async def compose_tour(
    body: ComposeTourRequest,
    user: dict = Depends(get_current_user),
) -> TourOut:
    _require_priv(user)
    factory = require_db()
    tenant_id = user["tenant_id"]
    async with factory() as session:
        await _set_tenant(session, tenant_id)
        rows = (
            await session.execute(
                sa.select(atlas_nodes).where(
                    atlas_nodes.c.tenant_id == tenant_id,
                    atlas_nodes.c.product_id == body.product_id,
                )
            )
        ).mappings().all()
    if not rows:
        raise HTTPException(
            404,
            {"code": "no_atlas_nodes_for_product", "product_id": body.product_id},
        )
    composer = TourComposer(TourComposerConfig())
    composed = composer.compose(
        nodes=[dict(r) for r in rows],
        persona=body.persona,
        target_minutes=body.target_minutes,
    )
    if not composed.playlist:
        raise HTTPException(
            422, "atlas has nodes but tour composer produced an empty playlist"
        )
    title = body.title or _default_tour_title(
        product_id=body.product_id, persona=body.persona
    )
    saved = await _action_repo().save_tour(
        tenant_id=tenant_id,
        product_id=body.product_id,
        title=title,
        persona=body.persona,
        target_minutes=body.target_minutes,
        playlist=[seg.model_dump() for seg in composed.playlist],
        coverage=composed.coverage,
        atlas_node_ids=list(composed.atlas_node_ids),
        status="published" if body.publish else "draft",
        created_by=user.get("user_id"),
    )
    return _tour_to_out(saved)


@router.get("/tours", response_model=list[TourOut])
async def list_tours(
    product_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
    user: dict = Depends(get_current_user),
) -> list[TourOut]:
    rows = await _action_repo().list_tours(
        tenant_id=user["tenant_id"],
        product_id=product_id,
        status=status,
        limit=limit,
    )
    return [_tour_to_out(r) for r in rows]


@router.get("/tours/{tour_id}", response_model=TourOut)
async def get_tour(
    tour_id: str,
    user: dict = Depends(get_current_user),
) -> TourOut:
    row = await _action_repo().get_tour(
        tenant_id=user["tenant_id"], tour_id=tour_id
    )
    if row is None:
        raise HTTPException(404, "tour_not_found")
    return _tour_to_out(row)


@router.post("/tours/{tour_id}/publish", response_model=TourOut)
async def publish_tour(
    tour_id: str,
    user: dict = Depends(get_current_user),
) -> TourOut:
    _require_priv(user)
    row = await _action_repo().update_tour_status(
        tenant_id=user["tenant_id"], tour_id=tour_id, status="published"
    )
    if row is None:
        raise HTTPException(404, "tour_not_found")
    return _tour_to_out(row)


@router.post("/tours/{tour_id}/archive", response_model=TourOut)
async def archive_tour(
    tour_id: str,
    user: dict = Depends(get_current_user),
) -> TourOut:
    _require_priv(user)
    row = await _action_repo().update_tour_status(
        tenant_id=user["tenant_id"], tour_id=tour_id, status="archived"
    )
    if row is None:
        raise HTTPException(404, "tour_not_found")
    return _tour_to_out(row)


# ── Impact endpoints ───────────────────────────────────────────


@router.post("/impact", response_model=ImpactReportOut)
async def analyze_impact(
    body: ImpactAnalyzeRequest,
    user: dict = Depends(get_current_user),
) -> ImpactReportOut:
    _require_priv(user)
    sf = get_session_factory()
    if sf is None:
        raise HTTPException(503, "database not connected")
    cfg = ImpactAnalyzerConfig(
        max_depth=body.max_depth or 3,
        include_rejected=body.include_rejected,
    )
    analyzer = ImpactAnalyzer(sf, cfg)
    report = await analyzer.analyze(
        tenant_id=user["tenant_id"],
        product_id=body.product_id,
        root_atlas_node_id=body.root_atlas_node_id,
    )
    if report is None:
        raise HTTPException(404, "atlas_node_not_found")

    # Build the persistence-friendly layer_summary with embedded
    # root metadata + truncation flag (kept under '__'-prefixed
    # keys so the public mapper can strip them).
    layer_summary_serialised = {
        layer: summary.to_dict() for layer, summary in report.layer_summary.items()
    }
    layer_summary_serialised["__root_label"] = report.root_label
    layer_summary_serialised["__root_layer"] = report.root_layer
    layer_summary_serialised["__truncated"] = report.truncated

    saved_row: Optional[dict[str, Any]] = None
    if body.save:
        expires_at = (
            datetime.now(timezone.utc)
            + timedelta(hours=body.expires_in_hours or 24)
            if body.expires_in_hours
            else None
        )
        saved_row = await _action_repo().save_impact_analysis(
            tenant_id=user["tenant_id"],
            product_id=body.product_id,
            root_atlas_node_id=body.root_atlas_node_id,
            change_description=body.change_description,
            downstream=[n.to_dict() for n in report.downstream],
            upstream=[n.to_dict() for n in report.upstream],
            layer_summary=layer_summary_serialised,
            estimated_blast_radius=report.blast_radius,
            requested_by=user.get("user_id"),
            expires_at=expires_at,
        )

    return ImpactReportOut(
        analysis_id=saved_row["analysis_id"] if saved_row else None,
        product_id=body.product_id,
        root_atlas_node_id=body.root_atlas_node_id,
        root_label=report.root_label,
        root_layer=report.root_layer,
        downstream=[n.to_dict() for n in report.downstream],
        upstream=[n.to_dict() for n in report.upstream],
        layer_summary={
            layer: summary.to_dict()
            for layer, summary in report.layer_summary.items()
        },
        estimated_blast_radius=report.blast_radius,
        truncated=report.truncated,
        created_at=(
            saved_row["created_at"].isoformat()
            if saved_row and saved_row.get("created_at")
            else None
        ),
    )


@router.get("/impact", response_model=list[ImpactReportOut])
async def list_impact_analyses(
    product_id: Optional[str] = None,
    limit: int = 50,
    user: dict = Depends(get_current_user),
) -> list[ImpactReportOut]:
    rows = await _action_repo().list_impact_analyses(
        tenant_id=user["tenant_id"], product_id=product_id, limit=limit
    )
    return [_impact_row_to_out(r) for r in rows]


@router.get("/impact/{analysis_id}", response_model=ImpactReportOut)
async def get_impact_analysis(
    analysis_id: str,
    user: dict = Depends(get_current_user),
) -> ImpactReportOut:
    row = await _action_repo().get_impact_analysis(
        tenant_id=user["tenant_id"], analysis_id=analysis_id
    )
    if row is None:
        raise HTTPException(404, "analysis_not_found")
    return _impact_row_to_out(row)


# ── Invocations audit ──────────────────────────────────────────


@router.get("/invocations", response_model=list[InvocationOut])
async def list_invocations(
    kind: Optional[str] = None,
    limit: int = 100,
    user: dict = Depends(get_current_user),
) -> list[InvocationOut]:
    rows = await _action_repo().list_invocations(
        tenant_id=user["tenant_id"], kind=kind, limit=limit
    )
    return [_invocation_to_out(r) for r in rows]


# ── Helpers ────────────────────────────────────────────────────


def _default_tour_title(*, product_id: str, persona: Optional[str]) -> str:
    persona_part = f" · {persona}" if persona else ""
    return f"{product_id}{persona_part} — guided tour"
