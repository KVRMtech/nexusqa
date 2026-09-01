"""SQLAlchemy Core repository for the atlas tables.

The repository is tenant-scoped via the RLS session variable. All
write paths use UPSERT semantics so the builder can be invoked
repeatedly for the same backbone node without producing duplicates.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ..db import Database

logger = logging.getLogger(__name__)


# ── Schema projections ─────────────────────────────────────────


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


class AtlasNodeConflict(Exception):
    """Raised when an UPSERT lost a race against a concurrent writer."""


@dataclass(frozen=True)
class UpsertResult:
    atlas_node_id: str
    created: bool


# ── Repository ─────────────────────────────────────────────────


class AtlasRepository:
    def __init__(self, db: Database):
        self._db = db

    # ── Node upsert ─────────────────────────────────────────────

    async def upsert_node(
        self,
        *,
        tenant_id: str,
        product_id: str,
        backbone_node_id: str,
        node_type: str,
        layer: str,
        label: str,
        confidence: float,
        source_session_ids: Iterable[str] = (),
        source_artifact_ids: Iterable[str] = (),
        source_segment_ids: Iterable[str] = (),
        metadata: Optional[dict[str, Any]] = None,
        last_seen_at: Optional[datetime] = None,
    ) -> UpsertResult:
        """Insert-or-merge an atlas node.

        ``source_*_ids`` arrays grow by union — repeated ingests of the
        same backbone node never lose evidence. Confidence is the max
        of any prior value (we don't downgrade nodes on re-write).
        """
        now = _now()
        last_seen = last_seen_at or now
        new_id = uuid.uuid4().hex

        async with self._db.tenant_session(tenant_id) as session:
            stmt = pg_insert(atlas_nodes).values(
                atlas_node_id=new_id,
                tenant_id=tenant_id,
                product_id=product_id,
                backbone_node_id=backbone_node_id,
                node_type=node_type,
                layer=layer,
                label=label[:512],
                source_session_ids=list(_dedup(source_session_ids)),
                source_artifact_ids=list(_dedup(source_artifact_ids)),
                source_segment_ids=list(_dedup(source_segment_ids)),
                confidence=float(confidence),
                metadata_json=metadata or {},
                last_seen_at=last_seen,
                created_at=now,
                updated_at=now,
                version=1,
            )
            # On conflict on the (tenant, product, backbone_node_id) unique
            # constraint, merge arrays + take the higher confidence + keep
            # latest last_seen_at.
            stmt = stmt.on_conflict_do_update(
                constraint="uq_atlas_node_backbone",
                set_={
                    "label": stmt.excluded.label,
                    "node_type": stmt.excluded.node_type,
                    "layer": stmt.excluded.layer,
                    "confidence": sa.case(
                        (
                            stmt.excluded.confidence > atlas_nodes.c.confidence,
                            stmt.excluded.confidence,
                        ),
                        else_=atlas_nodes.c.confidence,
                    ),
                    "source_session_ids": _array_union(
                        atlas_nodes.c.source_session_ids,
                        stmt.excluded.source_session_ids,
                    ),
                    "source_artifact_ids": _array_union(
                        atlas_nodes.c.source_artifact_ids,
                        stmt.excluded.source_artifact_ids,
                    ),
                    "source_segment_ids": _array_union(
                        atlas_nodes.c.source_segment_ids,
                        stmt.excluded.source_segment_ids,
                    ),
                    "last_seen_at": sa.case(
                        (
                            atlas_nodes.c.last_seen_at.is_(None),
                            stmt.excluded.last_seen_at,
                        ),
                        (
                            stmt.excluded.last_seen_at > atlas_nodes.c.last_seen_at,
                            stmt.excluded.last_seen_at,
                        ),
                        else_=atlas_nodes.c.last_seen_at,
                    ),
                    "metadata_json": stmt.excluded.metadata_json,
                    # updated_at + version updated by trigger.
                },
            ).returning(atlas_nodes)
            result = await session.execute(stmt)
            row = result.mappings().first()
        if row is None:
            raise AtlasNodeConflict(
                f"upsert of {backbone_node_id!r} produced no row"
            )
        return UpsertResult(
            atlas_node_id=row["atlas_node_id"],
            created=row["atlas_node_id"] == new_id,
        )

    async def get_node(
        self, *, tenant_id: str, atlas_node_id: str
    ) -> Optional[dict[str, Any]]:
        async with self._db.tenant_session(tenant_id) as session:
            row = (
                await session.execute(
                    sa.select(atlas_nodes).where(
                        atlas_nodes.c.tenant_id == tenant_id,
                        atlas_nodes.c.atlas_node_id == atlas_node_id,
                    )
                )
            ).mappings().first()
        return dict(row) if row else None

    async def get_node_by_backbone(
        self, *, tenant_id: str, product_id: str, backbone_node_id: str
    ) -> Optional[dict[str, Any]]:
        async with self._db.tenant_session(tenant_id) as session:
            row = (
                await session.execute(
                    sa.select(atlas_nodes).where(
                        atlas_nodes.c.tenant_id == tenant_id,
                        atlas_nodes.c.product_id == product_id,
                        atlas_nodes.c.backbone_node_id == backbone_node_id,
                    )
                )
            ).mappings().first()
        return dict(row) if row else None

    async def list_nodes(
        self,
        *,
        tenant_id: str,
        product_id: str,
        layer: Optional[str] = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        async with self._db.tenant_session(tenant_id) as session:
            stmt = sa.select(atlas_nodes).where(
                atlas_nodes.c.tenant_id == tenant_id,
                atlas_nodes.c.product_id == product_id,
            )
            if layer:
                stmt = stmt.where(atlas_nodes.c.layer == layer)
            stmt = stmt.order_by(
                atlas_nodes.c.layer.asc(),
                atlas_nodes.c.label.asc(),
            ).limit(max(1, min(2000, int(limit))))
            rows = (await session.execute(stmt)).mappings().all()
        return [dict(r) for r in rows]

    # ── Edge upsert ─────────────────────────────────────────────

    async def upsert_edge(
        self,
        *,
        tenant_id: str,
        product_id: str,
        from_atlas_node_id: str,
        to_atlas_node_id: str,
        relation_type: str,
        confidence: float,
        status: str = "auto",
        evidence: Optional[dict[str, Any]] = None,
    ) -> str:
        if from_atlas_node_id == to_atlas_node_id:
            raise ValueError("self-edges are not allowed")
        now = _now()
        new_id = uuid.uuid4().hex
        async with self._db.tenant_session(tenant_id) as session:
            stmt = pg_insert(atlas_edges).values(
                edge_id=new_id,
                tenant_id=tenant_id,
                product_id=product_id,
                from_atlas_node_id=from_atlas_node_id,
                to_atlas_node_id=to_atlas_node_id,
                relation_type=relation_type,
                confidence=float(confidence),
                status=status,
                evidence_json=evidence or {},
                created_at=now,
                updated_at=now,
            )
            stmt = stmt.on_conflict_do_update(
                constraint="uq_atlas_edge_triple",
                set_={
                    "confidence": sa.case(
                        (
                            stmt.excluded.confidence > atlas_edges.c.confidence,
                            stmt.excluded.confidence,
                        ),
                        else_=atlas_edges.c.confidence,
                    ),
                    "status": stmt.excluded.status,
                    "evidence_json": stmt.excluded.evidence_json,
                    "updated_at": stmt.excluded.updated_at,
                },
            ).returning(atlas_edges.c.edge_id)
            row = (await session.execute(stmt)).first()
        return row[0] if row else new_id

    async def set_edge_status(
        self,
        *,
        tenant_id: str,
        edge_id: str,
        status: str,
        actor: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        async with self._db.tenant_session(tenant_id) as session:
            await session.execute(
                sa.update(atlas_edges)
                .where(
                    atlas_edges.c.tenant_id == tenant_id,
                    atlas_edges.c.edge_id == edge_id,
                )
                .values(
                    status=status,
                    reviewed_by=actor,
                    reviewed_at=_now(),
                    updated_at=_now(),
                )
            )
            row = (
                await session.execute(
                    sa.select(atlas_edges).where(
                        atlas_edges.c.edge_id == edge_id,
                    )
                )
            ).mappings().first()
        return dict(row) if row else None

    async def list_edges(
        self,
        *,
        tenant_id: str,
        product_id: str,
        status: Optional[str] = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        async with self._db.tenant_session(tenant_id) as session:
            stmt = sa.select(atlas_edges).where(
                atlas_edges.c.tenant_id == tenant_id,
                atlas_edges.c.product_id == product_id,
            )
            if status:
                stmt = stmt.where(atlas_edges.c.status == status)
            stmt = stmt.order_by(atlas_edges.c.updated_at.desc()).limit(
                max(1, min(2000, int(limit)))
            )
            rows = (await session.execute(stmt)).mappings().all()
        return [dict(r) for r in rows]

    # ── Alignment proposals ────────────────────────────────────

    async def upsert_alignment(
        self,
        *,
        tenant_id: str,
        product_id: str,
        from_atlas_node_id: str,
        to_atlas_node_id: str,
        suggested_relation: str,
        similarity: Optional[float],
        evidence: Optional[dict[str, Any]] = None,
        status: str = "pending",
    ) -> str:
        if from_atlas_node_id == to_atlas_node_id:
            raise ValueError("self-alignments are not allowed")
        now = _now()
        new_id = uuid.uuid4().hex
        async with self._db.tenant_session(tenant_id) as session:
            stmt = pg_insert(atlas_alignments).values(
                alignment_id=new_id,
                tenant_id=tenant_id,
                product_id=product_id,
                from_atlas_node_id=from_atlas_node_id,
                to_atlas_node_id=to_atlas_node_id,
                suggested_relation=suggested_relation,
                similarity=similarity,
                evidence_json=evidence or {},
                status=status,
                created_at=now,
            )
            stmt = stmt.on_conflict_do_update(
                constraint="uq_atlas_align_triple",
                set_={
                    "similarity": stmt.excluded.similarity,
                    "evidence_json": stmt.excluded.evidence_json,
                    "status": stmt.excluded.status,
                },
            ).returning(atlas_alignments.c.alignment_id)
            row = (await session.execute(stmt)).first()
        return row[0] if row else new_id

    async def list_alignments(
        self,
        *,
        tenant_id: str,
        product_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        async with self._db.tenant_session(tenant_id) as session:
            stmt = sa.select(atlas_alignments).where(
                atlas_alignments.c.tenant_id == tenant_id,
            )
            if product_id:
                stmt = stmt.where(atlas_alignments.c.product_id == product_id)
            if status:
                stmt = stmt.where(atlas_alignments.c.status == status)
            stmt = stmt.order_by(
                atlas_alignments.c.created_at.desc()
            ).limit(max(1, min(1000, int(limit))))
            rows = (await session.execute(stmt)).mappings().all()
        return [dict(r) for r in rows]

    async def decide_alignment(
        self,
        *,
        tenant_id: str,
        alignment_id: str,
        decision: str,
        actor: str,
        note: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        if decision not in ("approved", "rejected"):
            raise ValueError("decision must be 'approved' or 'rejected'")
        async with self._db.tenant_session(tenant_id) as session:
            await session.execute(
                sa.update(atlas_alignments)
                .where(
                    atlas_alignments.c.tenant_id == tenant_id,
                    atlas_alignments.c.alignment_id == alignment_id,
                )
                .values(
                    status=decision,
                    decided_by=actor,
                    decided_at=_now(),
                    note=note,
                )
            )
            row = (
                await session.execute(
                    sa.select(atlas_alignments).where(
                        atlas_alignments.c.alignment_id == alignment_id,
                    )
                )
            ).mappings().first()
        return dict(row) if row else None

    # ── Layer stats ────────────────────────────────────────────

    async def refresh_layer_stats(
        self, *, tenant_id: str, product_id: str
    ) -> dict[str, dict[str, Any]]:
        """Recompute layer rollups for one product. Idempotent."""
        now = _now()
        out: dict[str, dict[str, Any]] = {}
        async with self._db.tenant_session(tenant_id) as session:
            counts = (
                await session.execute(
                    sa.select(
                        atlas_nodes.c.layer,
                        sa.func.count().label("n"),
                        sa.func.max(atlas_nodes.c.last_seen_at).label("last_at"),
                    )
                    .where(
                        atlas_nodes.c.tenant_id == tenant_id,
                        atlas_nodes.c.product_id == product_id,
                    )
                    .group_by(atlas_nodes.c.layer)
                )
            ).all()
            edge_in = {
                row.layer: int(row.n)
                for row in (
                    await session.execute(
                        sa.select(
                            atlas_nodes.c.layer.label("layer"),
                            sa.func.count().label("n"),
                        )
                        .select_from(
                            atlas_edges.join(
                                atlas_nodes,
                                atlas_nodes.c.atlas_node_id
                                == atlas_edges.c.to_atlas_node_id,
                            )
                        )
                        .where(
                            atlas_edges.c.tenant_id == tenant_id,
                            atlas_edges.c.product_id == product_id,
                            atlas_edges.c.status != "rejected",
                        )
                        .group_by(atlas_nodes.c.layer)
                    )
                ).all()
            }
            edge_out = {
                row.layer: int(row.n)
                for row in (
                    await session.execute(
                        sa.select(
                            atlas_nodes.c.layer.label("layer"),
                            sa.func.count().label("n"),
                        )
                        .select_from(
                            atlas_edges.join(
                                atlas_nodes,
                                atlas_nodes.c.atlas_node_id
                                == atlas_edges.c.from_atlas_node_id,
                            )
                        )
                        .where(
                            atlas_edges.c.tenant_id == tenant_id,
                            atlas_edges.c.product_id == product_id,
                            atlas_edges.c.status != "rejected",
                        )
                        .group_by(atlas_nodes.c.layer)
                    )
                ).all()
            }

            # Coverage: number-of-layers-with-nodes / total layers.
            total_layers = 7  # matches the enum
            present_layers = sum(1 for _ in counts)
            base_coverage = present_layers / total_layers if total_layers else 0.0

            # Upsert one row per observed layer + ensure absent layers
            # are recorded as zero (for the UI to render them).
            for row in counts:
                layer = row.layer
                n = int(row.n)
                # Per-layer coverage: log-scaled by node_count, so a
                # layer with many nodes contributes more weight.
                layer_score = _saturating(n)
                values = {
                    "tenant_id": tenant_id,
                    "product_id": product_id,
                    "layer": layer,
                    "node_count": n,
                    "edge_count_in": int(edge_in.get(layer, 0)),
                    "edge_count_out": int(edge_out.get(layer, 0)),
                    "last_node_at": row.last_at,
                    "coverage_score": min(1.0, base_coverage * 0.5 + layer_score * 0.5),
                    "metadata_json": {"refreshed_at": now.isoformat()},
                    "updated_at": now,
                }
                stmt = pg_insert(atlas_layer_stats).values(**values)
                stmt = stmt.on_conflict_do_update(
                    index_elements=[
                        atlas_layer_stats.c.tenant_id,
                        atlas_layer_stats.c.product_id,
                        atlas_layer_stats.c.layer,
                    ],
                    set_={
                        k: stmt.excluded[k]
                        for k in (
                            "node_count",
                            "edge_count_in",
                            "edge_count_out",
                            "last_node_at",
                            "coverage_score",
                            "metadata_json",
                            "updated_at",
                        )
                    },
                )
                await session.execute(stmt)
                out[layer] = values
        return out

    async def list_layer_stats(
        self, *, tenant_id: str, product_id: str
    ) -> list[dict[str, Any]]:
        async with self._db.tenant_session(tenant_id) as session:
            rows = (
                await session.execute(
                    sa.select(atlas_layer_stats).where(
                        atlas_layer_stats.c.tenant_id == tenant_id,
                        atlas_layer_stats.c.product_id == product_id,
                    )
                )
            ).mappings().all()
        return [dict(r) for r in rows]


# ── Helpers ─────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _dedup(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for v in values:
        if not isinstance(v, str) or not v:
            continue
        if v in seen:
            continue
        seen.add(v)
        out.append(v)
    return out


def _array_union(left, right):
    """SQL expression: dedup-union two text[] arrays.

    Implements ``array(select distinct unnest(left || right))`` so the
    UPSERT path grows the source-id arrays without duplicates.
    """
    union = sa.func.array_cat(left, right)
    return sa.func.array(
        sa.select(sa.func.unnest(union).label("v")).distinct().scalar_subquery()
    )


def _saturating(count: int, *, k: float = 10.0) -> float:
    """1 - exp(-n/k): smoothly saturating in [0, 1)."""
    import math

    if count <= 0:
        return 0.0
    return 1.0 - math.exp(-float(count) / max(0.1, k))
