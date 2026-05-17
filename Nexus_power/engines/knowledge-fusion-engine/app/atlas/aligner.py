"""Cross-modal aligner.

For each newly upserted atlas node, the aligner proposes cross-layer
edges to existing nodes that share strong signals:

    * **Embedding similarity** — fetched via Backbone search restricted
      to ``KnowledgeCard`` + ``TranscriptSegment`` + atlas-friendly
      types; same-product filter applied locally.
    * **Visual co-occurrence** — when ``source_segment_ids`` overlap
      between two nodes, that's a strong signal they describe the same
      moment in the demo. The aligner gives this a fixed boost.
    * **Layer compatibility** — only certain (from_layer, to_layer)
      pairs imply a sensible cross-layer relation. Same-layer edges
      are produced only for the rule-test pair (``test → rule``) and
      for ``experience → experience`` navigation chains.

The aligner returns ``AlignmentDecision`` per candidate so the builder
can write to either ``atlas_edges`` (auto-applied) or
``atlas_alignments`` (pending operator review).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Protocol

from ..backbone_client import BackboneClient, BackboneClientError
from .models import Layer, RelationKind

logger = logging.getLogger(__name__)


# ── DTOs ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AlignmentDecision:
    """One alignment proposal — the builder decides how to persist it."""

    to_backbone_node_id: str
    suggested_relation: RelationKind
    confidence: float
    similarity: float
    auto_apply: bool
    evidence: dict[str, Any]


@dataclass(frozen=True)
class AlignmentResult:
    decisions: list[AlignmentDecision] = field(default_factory=list)
    queried: int = 0
    errors: list[str] = field(default_factory=list)


# ── Layer relation map ─────────────────────────────────────────


_LAYER_RELATIONS: dict[tuple[Layer, Layer], RelationKind] = {
    (Layer.EXPERIENCE, Layer.APPLICATION): RelationKind.CALLS_API,
    (Layer.APPLICATION, Layer.DATA): RelationKind.READS_TABLE,
    (Layer.APPLICATION, Layer.RULE): RelationKind.ENFORCES_RULE,
    (Layer.TEST, Layer.RULE): RelationKind.TESTS_RULE,
    (Layer.EXPERIENCE, Layer.EXPERIENCE): RelationKind.NAVIGATES_TO,
    (Layer.APPLICATION, Layer.OPS): RelationKind.MONITORED_BY,
    (Layer.RULE, Layer.COMPLIANCE): RelationKind.GOVERNED_BY,
}


def _suggest_relation(from_layer: Layer, to_layer: Layer) -> Optional[RelationKind]:
    rel = _LAYER_RELATIONS.get((from_layer, to_layer))
    if rel is not None:
        return rel
    # Generic fallback for permitted cross-layer pairs that aren't
    # explicitly modelled — we still propose ``related_to`` for low-
    # confidence inspection. Same-layer pairs that aren't allowed
    # return None so the aligner skips them.
    if from_layer == to_layer:
        return None
    return RelationKind.RELATED_TO


# ── Search input ───────────────────────────────────────────────


class _AtlasIndexer(Protocol):
    """Read-side port over ``atlas_nodes`` — used to map backbone IDs to
    atlas IDs/layers when persisting decisions."""

    async def get_node_by_backbone(
        self, *, tenant_id: str, product_id: str, backbone_node_id: str
    ) -> Optional[dict[str, Any]]: ...


# ── Aligner ────────────────────────────────────────────────────


@dataclass(frozen=True)
class AlignerConfig:
    similarity_floor: float = 0.65
    auto_apply_floor: float = 0.85
    visual_overlap_boost: float = 0.12
    max_candidates: int = 10

    def __post_init__(self) -> None:
        if not (0.0 <= self.similarity_floor <= self.auto_apply_floor <= 1.0):
            raise ValueError(
                "0 <= similarity_floor <= auto_apply_floor <= 1"
            )


class CrossModalAligner:
    def __init__(
        self,
        backbone: BackboneClient,
        *,
        config: Optional[AlignerConfig] = None,
        same_product_node_types: Iterable[str] = (
            "TranscriptSegment",
            "KnowledgeCard",
            "BusinessRule",
            "APIEndpoint",
            "DatabaseTable",
            "UIScreen",
            "UIElement",
            "TestCase",
        ),
    ) -> None:
        self._backbone = backbone
        self._cfg = config or AlignerConfig()
        self._node_types = list(same_product_node_types)

    async def align(
        self,
        *,
        tenant_id: str,
        trace_id: str,
        product_id: str,
        source_backbone_node_id: str,
        source_layer: Layer,
        source_text: str,
        source_segment_ids: Iterable[str],
        indexer: _AtlasIndexer,
    ) -> AlignmentResult:
        if not source_text or not product_id:
            return AlignmentResult()
        try:
            rows = await self._backbone.search(
                tenant_id=tenant_id,
                trace_id=trace_id,
                query=source_text,
                node_types=self._node_types,
                limit=self._cfg.max_candidates,
                min_similarity=self._cfg.similarity_floor,
            )
        except BackboneClientError as exc:
            return AlignmentResult(
                queried=0, errors=[f"backbone_search: {exc}"]
            )

        decisions: list[AlignmentDecision] = []
        source_segments = set(source_segment_ids or ())

        for row in rows:
            cand_node_id = str(row.get("node_id") or "")
            if not cand_node_id or cand_node_id == source_backbone_node_id:
                continue
            similarity = _safe_float(row.get("similarity"), 0.0)
            if similarity < self._cfg.similarity_floor:
                continue

            # We need the candidate's atlas-side metadata to know its
            # layer + product. Look it up by backbone id, scoped to
            # the same product.
            atlas_row = await indexer.get_node_by_backbone(
                tenant_id=tenant_id,
                product_id=product_id,
                backbone_node_id=cand_node_id,
            )
            if atlas_row is None:
                continue

            try:
                cand_layer = Layer(atlas_row["layer"])
            except (KeyError, ValueError):
                continue

            relation = _suggest_relation(source_layer, cand_layer)
            if relation is None:
                continue

            cand_segments = set(atlas_row.get("source_segment_ids") or ())
            overlap = source_segments & cand_segments
            confidence = similarity
            if overlap:
                confidence = min(
                    1.0, confidence + self._cfg.visual_overlap_boost
                )

            evidence = {
                "similarity": similarity,
                "visual_overlap": len(overlap),
                "source_layer": source_layer.value,
                "candidate_layer": cand_layer.value,
                "candidate_node_type": atlas_row.get("node_type"),
            }

            decisions.append(
                AlignmentDecision(
                    to_backbone_node_id=cand_node_id,
                    suggested_relation=relation,
                    confidence=confidence,
                    similarity=similarity,
                    auto_apply=confidence >= self._cfg.auto_apply_floor,
                    evidence=evidence,
                )
            )
        return AlignmentResult(decisions=decisions, queried=len(rows))


# ── Helpers ────────────────────────────────────────────────────


def _safe_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
