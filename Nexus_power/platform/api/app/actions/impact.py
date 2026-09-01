"""Impact analyzer — walks ``atlas_edges`` outward from a root node.

The analyzer answers "if I change this, what is touched?" by doing a
bounded-depth BFS over the atlas graph. ``confirmed`` and ``auto``
edges contribute; ``rejected`` edges are excluded; ``pending_review``
edges contribute with a tag so operators can see them in the result.

We bound depth and breadth to keep the worst case predictable:

    * depth                 — default 3, max 5
    * max_nodes_per_layer   — default 50
    * max_total_nodes       — default 500 (hard cap; truncates with a flag)

Output:
    ImpactReport {
        root,
        downstream:   list[ImpactNode],   # follow from→to edges
        upstream:     list[ImpactNode],   # follow to→from edges
        layer_summary: dict[layer, LayerSummary],
        blast_radius:  int,               # unique nodes touched (excluding root)
        truncated:     bool,
    }
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger(__name__)


_md = sa.MetaData()


atlas_nodes = sa.Table(
    "atlas_nodes",
    _md,
    sa.Column("atlas_node_id", sa.String(64), primary_key=True),
    sa.Column("tenant_id", sa.String(64), nullable=False),
    sa.Column("product_id", sa.String(64), nullable=False),
    sa.Column("node_type", sa.String(64), nullable=False),
    sa.Column("layer", sa.String(16), nullable=False),
    sa.Column("label", sa.String(512), nullable=False),
    sa.Column("confidence", sa.Float, nullable=False),
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
)


@dataclass(frozen=True)
class ImpactNode:
    atlas_node_id: str
    label: str
    layer: str
    node_type: str
    distance: int
    relation_chain: tuple[str, ...]
    edge_status_chain: tuple[str, ...]
    confidence: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "atlas_node_id": self.atlas_node_id,
            "label": self.label,
            "layer": self.layer,
            "node_type": self.node_type,
            "distance": self.distance,
            "relation_chain": list(self.relation_chain),
            "edge_status_chain": list(self.edge_status_chain),
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class LayerSummary:
    layer: str
    node_count: int
    has_pending_review: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "layer": self.layer,
            "node_count": self.node_count,
            "has_pending_review": self.has_pending_review,
        }


@dataclass(frozen=True)
class ImpactReport:
    root_atlas_node_id: str
    root_label: str
    root_layer: str
    downstream: tuple[ImpactNode, ...]
    upstream: tuple[ImpactNode, ...]
    layer_summary: dict[str, LayerSummary]
    blast_radius: int
    truncated: bool


@dataclass(frozen=True)
class ImpactAnalyzerConfig:
    max_depth: int = 3
    max_nodes_per_layer: int = 50
    max_total_nodes: int = 500
    include_rejected: bool = False


class ImpactAnalyzer:
    """Pure BFS over ``atlas_edges`` + ``atlas_nodes``."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        config: Optional[ImpactAnalyzerConfig] = None,
    ):
        self._sf = session_factory
        cfg = config or ImpactAnalyzerConfig()
        # Hard-cap the operator-supplied values defensively.
        self._max_depth = max(1, min(5, int(cfg.max_depth)))
        self._max_per_layer = max(1, min(500, int(cfg.max_nodes_per_layer)))
        self._max_total = max(1, min(5000, int(cfg.max_total_nodes)))
        self._include_rejected = bool(cfg.include_rejected)

    async def analyze(
        self,
        *,
        tenant_id: str,
        product_id: str,
        root_atlas_node_id: str,
    ) -> Optional[ImpactReport]:
        async with self._sf() as session:
            await _set_tenant(session, tenant_id)
            # Single pass over the product's atlas: load every node
            # once, then look up the root in-memory. Avoids a duplicate
            # SELECT against atlas_nodes for the root row and keeps the
            # analyzer easy to fake in tests.
            node_rows = (
                await session.execute(
                    sa.select(atlas_nodes).where(
                        atlas_nodes.c.tenant_id == tenant_id,
                        atlas_nodes.c.product_id == product_id,
                    )
                )
            ).mappings().all()
            edges = (
                await session.execute(
                    sa.select(atlas_edges).where(
                        atlas_edges.c.tenant_id == tenant_id,
                        atlas_edges.c.product_id == product_id,
                    )
                )
            ).mappings().all()

        node_index = {row["atlas_node_id"]: dict(row) for row in node_rows}
        root_row = node_index.get(root_atlas_node_id)
        if root_row is None:
            return None
        adjacency_out: dict[str, list[dict[str, Any]]] = {}
        adjacency_in: dict[str, list[dict[str, Any]]] = {}
        for edge in edges:
            if not self._edge_active(edge["status"]):
                continue
            e = dict(edge)
            adjacency_out.setdefault(e["from_atlas_node_id"], []).append(e)
            adjacency_in.setdefault(e["to_atlas_node_id"], []).append(e)

        downstream, truncated_down = self._bfs(
            start=root_atlas_node_id,
            adjacency=adjacency_out,
            node_index=node_index,
            direction="downstream",
        )
        upstream, truncated_up = self._bfs(
            start=root_atlas_node_id,
            adjacency=adjacency_in,
            node_index=node_index,
            direction="upstream",
        )

        layer_summary = self._summarise(downstream + upstream)
        blast_radius = len(set(n.atlas_node_id for n in (downstream + upstream)))
        return ImpactReport(
            root_atlas_node_id=root_atlas_node_id,
            root_label=root_row["label"],
            root_layer=root_row["layer"],
            downstream=tuple(downstream),
            upstream=tuple(upstream),
            layer_summary=layer_summary,
            blast_radius=blast_radius,
            truncated=truncated_down or truncated_up,
        )

    # ── Internals ──────────────────────────────────────────────

    def _edge_active(self, status: str) -> bool:
        if status == "rejected":
            return self._include_rejected
        return status in ("auto", "confirmed", "pending_review")

    def _bfs(
        self,
        *,
        start: str,
        adjacency: dict[str, list[dict[str, Any]]],
        node_index: dict[str, dict[str, Any]],
        direction: str,
    ) -> tuple[list[ImpactNode], bool]:
        visited: set[str] = {start}
        per_layer_count: dict[str, int] = {}
        truncated = False
        out: list[ImpactNode] = []
        queue: deque[tuple[str, int, tuple[str, ...], tuple[str, ...]]] = deque()
        queue.append((start, 0, (), ()))

        while queue:
            node_id, depth, relations, statuses = queue.popleft()
            if depth >= self._max_depth:
                continue
            next_edges = adjacency.get(node_id, [])
            for edge in next_edges:
                neighbour = (
                    edge["to_atlas_node_id"]
                    if direction == "downstream"
                    else edge["from_atlas_node_id"]
                )
                if neighbour in visited:
                    continue
                node_row = node_index.get(neighbour)
                if node_row is None:
                    continue
                layer = node_row["layer"]
                layer_count = per_layer_count.get(layer, 0)
                if layer_count >= self._max_per_layer:
                    truncated = True
                    continue
                if len(out) >= self._max_total:
                    truncated = True
                    return out, truncated

                visited.add(neighbour)
                per_layer_count[layer] = layer_count + 1
                new_relations = relations + (edge["relation_type"],)
                new_statuses = statuses + (edge["status"],)
                out.append(
                    ImpactNode(
                        atlas_node_id=neighbour,
                        label=node_row["label"],
                        layer=layer,
                        node_type=node_row["node_type"],
                        distance=depth + 1,
                        relation_chain=new_relations,
                        edge_status_chain=new_statuses,
                        confidence=float(node_row["confidence"]),
                    )
                )
                queue.append(
                    (neighbour, depth + 1, new_relations, new_statuses)
                )
        return out, truncated

    @staticmethod
    def _summarise(nodes: Iterable[ImpactNode]) -> dict[str, LayerSummary]:
        out: dict[str, dict[str, Any]] = {}
        for n in nodes:
            slot = out.setdefault(
                n.layer, {"count": 0, "pending": False}
            )
            slot["count"] += 1
            if any(s == "pending_review" for s in n.edge_status_chain):
                slot["pending"] = True
        return {
            layer: LayerSummary(
                layer=layer,
                node_count=int(slot["count"]),
                has_pending_review=bool(slot["pending"]),
            )
            for layer, slot in out.items()
        }


async def _set_tenant(session: AsyncSession, tenant_id: str) -> None:
    await session.execute(
        sa.text("SELECT set_config('nexus.current_tenant_id', :tid, true)"),
        {"tid": tenant_id},
    )
