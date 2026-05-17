"""Platform API — Product Atlas read + alignment-review endpoints.

Read paths return the projected atlas. Mutations are limited to the
alignment-review queue and per-edge status changes so operators can
approve / reject the cross-modal aligner's proposals without touching
Backbone directly.

Endpoints
---------

    GET    /api/v1/atlas/{product_id}              — full atlas view
    GET    /api/v1/atlas/{product_id}/nodes         — node list (filterable)
    GET    /api/v1/atlas/{product_id}/edges         — edge list (filterable)
    GET    /api/v1/atlas/{product_id}/coverage      — layer-stats rollup
    GET    /api/v1/atlas/alignments                  — alignment queue
    POST   /api/v1/atlas/alignments/{id}/approve    — operator approves
    POST   /api/v1/atlas/alignments/{id}/reject     — operator rejects
    POST   /api/v1/atlas/edges/{id}/confirm         — promote auto → confirmed
    POST   /api/v1/atlas/edges/{id}/reject          — reject an edge
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import get_current_user
from ..database import require_db

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Product Atlas"], prefix="/api/v1/atlas")


# ── Schema projections (mirror migration 023) ──────────────────


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
    sa.Column("last_seen_at", sa.DateTime(timezone=True)),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("version", sa.Integer, nullable=False),
)

atlas_edges = sa.Table(
    "atlas_edges",
    _md,
    sa.Column("edge_id", sa.String(64), primary_key=True),
    sa.Column("tenant_id", sa.String(64), nullable=False),
    sa.Column("product_id", sa.String(64), nullable=False),
    sa.Column("from_atlas_node_id", sa.String(64), nullable=False),
    sa.Column("to_atlas_node_id", sa.String(64), nullable=False),
    sa.Column("relation_type", sa.String(48), nullable=False),
    sa.Column("confidence", sa.Float, nullable=False),
    sa.Column("status", sa.String(16), nullable=False),
    sa.Column("evidence_json", JSONB, nullable=False),
    sa.Column("reviewed_by", sa.String(128)),
    sa.Column("reviewed_at", sa.DateTime(timezone=True)),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)

atlas_alignments = sa.Table(
    "atlas_alignments",
    _md,
    sa.Column("alignment_id", sa.String(64), primary_key=True),
    sa.Column("tenant_id", sa.String(64), nullable=False),
    sa.Column("product_id", sa.String(64), nullable=False),
    sa.Column("from_atlas_node_id", sa.String(64), nullable=False),
    sa.Column("to_atlas_node_id", sa.String(64), nullable=False),
    sa.Column("suggested_relation", sa.String(48), nullable=False),
    sa.Column("similarity", sa.Float),
    sa.Column("evidence_json", JSONB, nullable=False),
    sa.Column("status", sa.String(16), nullable=False),
    sa.Column("decided_by", sa.String(128)),
    sa.Column("decided_at", sa.DateTime(timezone=True)),
    sa.Column("note", sa.String(512)),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)

atlas_layer_stats = sa.Table(
    "atlas_layer_stats",
    _md,
    sa.Column("tenant_id", sa.String(64), primary_key=True),
    sa.Column("product_id", sa.String(64), primary_key=True),
    sa.Column("layer", sa.String(16), primary_key=True),
    sa.Column("node_count", sa.Integer, nullable=False),
    sa.Column("edge_count_in", sa.Integer, nullable=False),
    sa.Column("edge_count_out", sa.Integer, nullable=False),
    sa.Column("last_node_at", sa.DateTime(timezone=True)),
    sa.Column("coverage_score", sa.Float, nullable=False),
    sa.Column("metadata_json", JSONB, nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)


# ── Helpers ─────────────────────────────────────────────────────


_PRIVILEGED = frozenset({"admin", "manager"})


def _require_priv(user: dict) -> None:
    if user.get("role", "viewer") not in _PRIVILEGED:
        raise HTTPException(403, "admin or manager required")


async def _set_tenant(session: AsyncSession, tenant_id: str) -> None:
    await session.execute(
        sa.text("SELECT set_config('nexus.current_tenant_id', :tid, true)"),
        {"tid": tenant_id},
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── DTOs ───────────────────────────────────────────────────────


class AtlasNodeOut(BaseModel):
    atlas_node_id: str
    backbone_node_id: str
    product_id: str
    node_type: str
    layer: str
    label: str
    confidence: float
    source_session_ids: list[str]
    source_artifact_ids: list[str]
    source_segment_ids: list[str]
    metadata: dict[str, Any]
    last_seen_at: Optional[str] = None
    created_at: str
    updated_at: str
    version: int


class AtlasEdgeOut(BaseModel):
    edge_id: str
    product_id: str
    from_atlas_node_id: str
    to_atlas_node_id: str
    relation_type: str
    confidence: float
    status: str
    evidence: dict[str, Any]
    reviewed_by: Optional[str] = None
    reviewed_at: Optional[str] = None
    created_at: str
    updated_at: str


class AtlasAlignmentOut(BaseModel):
    alignment_id: str
    product_id: str
    from_atlas_node_id: str
    to_atlas_node_id: str
    suggested_relation: str
    similarity: Optional[float] = None
    status: str
    evidence: dict[str, Any]
    decided_by: Optional[str] = None
    decided_at: Optional[str] = None
    note: Optional[str] = None
    created_at: str


class LayerStatOut(BaseModel):
    layer: str
    node_count: int
    edge_count_in: int
    edge_count_out: int
    coverage_score: float
    last_node_at: Optional[str] = None


class AtlasOverviewOut(BaseModel):
    product_id: str
    layer_stats: list[LayerStatOut]
    total_nodes: int
    total_edges: int
    pending_alignments: int


class DecideRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: Optional[str] = Field(default=None, max_length=512)


# ── Mappers ─────────────────────────────────────────────────────


def _node_to_out(row) -> AtlasNodeOut:
    return AtlasNodeOut(
        atlas_node_id=row["atlas_node_id"],
        backbone_node_id=row["backbone_node_id"],
        product_id=row["product_id"],
        node_type=row["node_type"],
        layer=row["layer"],
        label=row["label"],
        confidence=float(row["confidence"]),
        source_session_ids=list(row["source_session_ids"] or []),
        source_artifact_ids=list(row["source_artifact_ids"] or []),
        source_segment_ids=list(row["source_segment_ids"] or []),
        metadata=dict(row["metadata_json"] or {}),
        last_seen_at=(
            row["last_seen_at"].isoformat()
            if row["last_seen_at"]
            else None
        ),
        created_at=row["created_at"].isoformat(),
        updated_at=row["updated_at"].isoformat(),
        version=int(row["version"]),
    )


def _edge_to_out(row) -> AtlasEdgeOut:
    return AtlasEdgeOut(
        edge_id=row["edge_id"],
        product_id=row["product_id"],
        from_atlas_node_id=row["from_atlas_node_id"],
        to_atlas_node_id=row["to_atlas_node_id"],
        relation_type=row["relation_type"],
        confidence=float(row["confidence"]),
        status=row["status"],
        evidence=dict(row["evidence_json"] or {}),
        reviewed_by=row["reviewed_by"],
        reviewed_at=(
            row["reviewed_at"].isoformat() if row["reviewed_at"] else None
        ),
        created_at=row["created_at"].isoformat(),
        updated_at=row["updated_at"].isoformat(),
    )


def _alignment_to_out(row) -> AtlasAlignmentOut:
    return AtlasAlignmentOut(
        alignment_id=row["alignment_id"],
        product_id=row["product_id"],
        from_atlas_node_id=row["from_atlas_node_id"],
        to_atlas_node_id=row["to_atlas_node_id"],
        suggested_relation=row["suggested_relation"],
        similarity=(
            float(row["similarity"]) if row["similarity"] is not None else None
        ),
        status=row["status"],
        evidence=dict(row["evidence_json"] or {}),
        decided_by=row["decided_by"],
        decided_at=(
            row["decided_at"].isoformat() if row["decided_at"] else None
        ),
        note=row["note"],
        created_at=row["created_at"].isoformat(),
    )


# ── Endpoints ──────────────────────────────────────────────────


@router.get("/{product_id}", response_model=AtlasOverviewOut)
async def get_atlas_overview(
    product_id: str,
    user: dict = Depends(get_current_user),
) -> AtlasOverviewOut:
    factory = require_db()
    tenant_id = user["tenant_id"]
    async with factory() as session:
        await _set_tenant(session, tenant_id)
        stat_rows = (
            await session.execute(
                sa.select(atlas_layer_stats).where(
                    atlas_layer_stats.c.tenant_id == tenant_id,
                    atlas_layer_stats.c.product_id == product_id,
                )
            )
        ).mappings().all()
        total_nodes = int(
            (
                await session.execute(
                    sa.select(sa.func.count()).where(
                        atlas_nodes.c.tenant_id == tenant_id,
                        atlas_nodes.c.product_id == product_id,
                    )
                )
            ).scalar_one()
        )
        total_edges = int(
            (
                await session.execute(
                    sa.select(sa.func.count()).where(
                        atlas_edges.c.tenant_id == tenant_id,
                        atlas_edges.c.product_id == product_id,
                        atlas_edges.c.status != "rejected",
                    )
                )
            ).scalar_one()
        )
        pending = int(
            (
                await session.execute(
                    sa.select(sa.func.count()).where(
                        atlas_alignments.c.tenant_id == tenant_id,
                        atlas_alignments.c.product_id == product_id,
                        atlas_alignments.c.status == "pending",
                    )
                )
            ).scalar_one()
        )
    return AtlasOverviewOut(
        product_id=product_id,
        layer_stats=[
            LayerStatOut(
                layer=r["layer"],
                node_count=int(r["node_count"]),
                edge_count_in=int(r["edge_count_in"]),
                edge_count_out=int(r["edge_count_out"]),
                coverage_score=float(r["coverage_score"]),
                last_node_at=(
                    r["last_node_at"].isoformat()
                    if r["last_node_at"]
                    else None
                ),
            )
            for r in stat_rows
        ],
        total_nodes=total_nodes,
        total_edges=total_edges,
        pending_alignments=pending,
    )


@router.get("/{product_id}/nodes", response_model=list[AtlasNodeOut])
async def list_atlas_nodes(
    product_id: str,
    layer: Optional[str] = None,
    limit: int = 500,
    user: dict = Depends(get_current_user),
) -> list[AtlasNodeOut]:
    factory = require_db()
    tenant_id = user["tenant_id"]
    limit = max(1, min(2000, int(limit)))
    async with factory() as session:
        await _set_tenant(session, tenant_id)
        stmt = sa.select(atlas_nodes).where(
            atlas_nodes.c.tenant_id == tenant_id,
            atlas_nodes.c.product_id == product_id,
        )
        if layer:
            stmt = stmt.where(atlas_nodes.c.layer == layer)
        stmt = stmt.order_by(
            atlas_nodes.c.layer.asc(), atlas_nodes.c.label.asc()
        ).limit(limit)
        rows = (await session.execute(stmt)).mappings().all()
    return [_node_to_out(r) for r in rows]


@router.get("/{product_id}/edges", response_model=list[AtlasEdgeOut])
async def list_atlas_edges(
    product_id: str,
    status: Optional[str] = None,
    limit: int = 500,
    user: dict = Depends(get_current_user),
) -> list[AtlasEdgeOut]:
    factory = require_db()
    tenant_id = user["tenant_id"]
    limit = max(1, min(2000, int(limit)))
    async with factory() as session:
        await _set_tenant(session, tenant_id)
        stmt = sa.select(atlas_edges).where(
            atlas_edges.c.tenant_id == tenant_id,
            atlas_edges.c.product_id == product_id,
        )
        if status:
            stmt = stmt.where(atlas_edges.c.status == status)
        stmt = stmt.order_by(atlas_edges.c.updated_at.desc()).limit(limit)
        rows = (await session.execute(stmt)).mappings().all()
    return [_edge_to_out(r) for r in rows]


@router.get("/{product_id}/coverage", response_model=list[LayerStatOut])
async def get_coverage(
    product_id: str,
    user: dict = Depends(get_current_user),
) -> list[LayerStatOut]:
    factory = require_db()
    tenant_id = user["tenant_id"]
    async with factory() as session:
        await _set_tenant(session, tenant_id)
        rows = (
            await session.execute(
                sa.select(atlas_layer_stats).where(
                    atlas_layer_stats.c.tenant_id == tenant_id,
                    atlas_layer_stats.c.product_id == product_id,
                )
            )
        ).mappings().all()
    return [
        LayerStatOut(
            layer=r["layer"],
            node_count=int(r["node_count"]),
            edge_count_in=int(r["edge_count_in"]),
            edge_count_out=int(r["edge_count_out"]),
            coverage_score=float(r["coverage_score"]),
            last_node_at=(
                r["last_node_at"].isoformat()
                if r["last_node_at"]
                else None
            ),
        )
        for r in rows
    ]


@router.get("/alignments", response_model=list[AtlasAlignmentOut])
async def list_alignments(
    product_id: Optional[str] = None,
    status: Optional[str] = "pending",
    limit: int = 100,
    user: dict = Depends(get_current_user),
) -> list[AtlasAlignmentOut]:
    factory = require_db()
    tenant_id = user["tenant_id"]
    limit = max(1, min(1000, int(limit)))
    async with factory() as session:
        await _set_tenant(session, tenant_id)
        stmt = sa.select(atlas_alignments).where(
            atlas_alignments.c.tenant_id == tenant_id,
        )
        if product_id:
            stmt = stmt.where(atlas_alignments.c.product_id == product_id)
        if status:
            stmt = stmt.where(atlas_alignments.c.status == status)
        stmt = stmt.order_by(atlas_alignments.c.created_at.desc()).limit(limit)
        rows = (await session.execute(stmt)).mappings().all()
    return [_alignment_to_out(r) for r in rows]


@router.post(
    "/alignments/{alignment_id}/approve",
    response_model=AtlasAlignmentOut,
)
async def approve_alignment(
    alignment_id: str,
    body: DecideRequest,
    user: dict = Depends(get_current_user),
) -> AtlasAlignmentOut:
    _require_priv(user)
    return await _decide_and_apply(
        tenant_id=user["tenant_id"],
        alignment_id=alignment_id,
        decision="approved",
        actor=user["user_id"],
        note=body.note,
    )


@router.post(
    "/alignments/{alignment_id}/reject",
    response_model=AtlasAlignmentOut,
)
async def reject_alignment(
    alignment_id: str,
    body: DecideRequest,
    user: dict = Depends(get_current_user),
) -> AtlasAlignmentOut:
    _require_priv(user)
    return await _decide_and_apply(
        tenant_id=user["tenant_id"],
        alignment_id=alignment_id,
        decision="rejected",
        actor=user["user_id"],
        note=body.note,
    )


@router.post(
    "/edges/{edge_id}/confirm",
    response_model=AtlasEdgeOut,
)
async def confirm_edge(
    edge_id: str,
    user: dict = Depends(get_current_user),
) -> AtlasEdgeOut:
    _require_priv(user)
    return await _set_edge_status(
        tenant_id=user["tenant_id"],
        edge_id=edge_id,
        new_status="confirmed",
        actor=user["user_id"],
    )


@router.post(
    "/edges/{edge_id}/reject",
    response_model=AtlasEdgeOut,
)
async def reject_edge(
    edge_id: str,
    user: dict = Depends(get_current_user),
) -> AtlasEdgeOut:
    _require_priv(user)
    return await _set_edge_status(
        tenant_id=user["tenant_id"],
        edge_id=edge_id,
        new_status="rejected",
        actor=user["user_id"],
    )


# ── Decision plumbing ──────────────────────────────────────────


async def _decide_and_apply(
    *,
    tenant_id: str,
    alignment_id: str,
    decision: str,
    actor: str,
    note: Optional[str],
) -> AtlasAlignmentOut:
    factory = require_db()
    now = _now()
    async with factory() as session:
        await _set_tenant(session, tenant_id)
        existing = (
            await session.execute(
                sa.select(atlas_alignments).where(
                    atlas_alignments.c.tenant_id == tenant_id,
                    atlas_alignments.c.alignment_id == alignment_id,
                )
            )
        ).mappings().first()
        if existing is None:
            raise HTTPException(404, "alignment_not_found")
        if existing["status"] not in ("pending",):
            raise HTTPException(
                409,
                {
                    "code": "alignment_already_decided",
                    "status": existing["status"],
                },
            )

        await session.execute(
            sa.update(atlas_alignments)
            .where(atlas_alignments.c.alignment_id == alignment_id)
            .values(
                status=decision,
                decided_by=actor,
                decided_at=now,
                note=note,
            )
        )

        if decision == "approved":
            edge_stmt = pg_insert(atlas_edges).values(
                edge_id=uuid.uuid4().hex,
                tenant_id=tenant_id,
                product_id=existing["product_id"],
                from_atlas_node_id=existing["from_atlas_node_id"],
                to_atlas_node_id=existing["to_atlas_node_id"],
                relation_type=existing["suggested_relation"],
                confidence=float(existing["similarity"] or 0.5),
                status="confirmed",
                evidence_json={
                    "from_alignment_id": alignment_id,
                    **dict(existing["evidence_json"] or {}),
                },
                reviewed_by=actor,
                reviewed_at=now,
                created_at=now,
                updated_at=now,
            )
            edge_stmt = edge_stmt.on_conflict_do_update(
                constraint="uq_atlas_edge_triple",
                set_={
                    "status": edge_stmt.excluded.status,
                    "confidence": edge_stmt.excluded.confidence,
                    "evidence_json": edge_stmt.excluded.evidence_json,
                    "reviewed_by": edge_stmt.excluded.reviewed_by,
                    "reviewed_at": edge_stmt.excluded.reviewed_at,
                    "updated_at": edge_stmt.excluded.updated_at,
                },
            )
            await session.execute(edge_stmt)

        await session.commit()
        row = (
            await session.execute(
                sa.select(atlas_alignments).where(
                    atlas_alignments.c.alignment_id == alignment_id,
                )
            )
        ).mappings().first()
    if row is None:
        raise HTTPException(500, "alignment_lost_after_decision")
    return _alignment_to_out(row)


async def _set_edge_status(
    *,
    tenant_id: str,
    edge_id: str,
    new_status: str,
    actor: str,
) -> AtlasEdgeOut:
    factory = require_db()
    now = _now()
    async with factory() as session:
        await _set_tenant(session, tenant_id)
        await session.execute(
            sa.update(atlas_edges)
            .where(
                atlas_edges.c.tenant_id == tenant_id,
                atlas_edges.c.edge_id == edge_id,
            )
            .values(
                status=new_status,
                reviewed_by=actor,
                reviewed_at=now,
                updated_at=now,
            )
        )
        await session.commit()
        row = (
            await session.execute(
                sa.select(atlas_edges).where(
                    atlas_edges.c.edge_id == edge_id,
                )
            )
        ).mappings().first()
    if row is None:
        raise HTTPException(404, "edge_not_found")
    return _edge_to_out(row)
