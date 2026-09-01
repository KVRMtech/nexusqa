"""Atlas builder — the integration point between ingest and projection.

For each new ``NodeCandidate``:

    1. Resolve a product. Skip when no product matches (no atlas row).
    2. Classify the layer.
    3. Upsert the atlas node.
    4. Ask the aligner for cross-layer proposals.
    5. Persist proposals as either ``atlas_edges`` rows (auto-applied)
       or ``atlas_alignments`` rows (pending operator review).
    6. After a batch, refresh ``atlas_layer_stats`` for touched products.

The builder owns the policy (auto vs pending) and is the only place
that writes to atlas tables. Tests can exercise it with in-memory
doubles of repository + backbone client.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Iterable, Optional

from ..backbone_client import BackboneClient
from .aligner import AlignmentResult, CrossModalAligner
from .layer_classifier import HeuristicLayerClassifier, LayerClassifier
from .models import EdgeStatus, Layer, NodeCandidate
from .product_resolver import ProductResolver
from .repository import AtlasRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BuilderResult:
    nodes_upserted: int = 0
    nodes_skipped_no_product: int = 0
    edges_auto: int = 0
    alignments_pending: int = 0
    products_touched: tuple[str, ...] = ()
    errors: list[str] = field(default_factory=list)


class AtlasBuilder:
    def __init__(
        self,
        repo: AtlasRepository,
        backbone: BackboneClient,
        *,
        classifier: Optional[LayerClassifier] = None,
        aligner: Optional[CrossModalAligner] = None,
    ) -> None:
        self._repo = repo
        self._backbone = backbone
        self._classifier = classifier or HeuristicLayerClassifier()
        self._aligner = aligner or CrossModalAligner(backbone)

    async def build(
        self,
        *,
        tenant_id: str,
        trace_id: str,
        candidates: Iterable[NodeCandidate],
        product_resolver: ProductResolver,
        refresh_stats: bool = True,
    ) -> BuilderResult:
        result = BuilderResult()
        touched: set[str] = set()
        nodes_upserted = 0
        nodes_skipped_no_product = 0
        edges_auto = 0
        alignments_pending = 0

        for cand in candidates:
            product_id = await self._resolve_product(cand, product_resolver)
            if product_id is None:
                nodes_skipped_no_product += 1
                continue

            verdict = (
                await self._classifier.classify(
                    node_type=cand.node_type,
                    text=cand.text or cand.label,
                    layer_hint=cand.layer.value if cand.layer else None,
                )
            )

            upsert = await self._repo.upsert_node(
                tenant_id=tenant_id,
                product_id=product_id,
                backbone_node_id=cand.backbone_node_id,
                node_type=cand.node_type,
                layer=verdict.layer.value,
                label=cand.label,
                confidence=verdict.confidence,
                source_session_ids=(
                    [cand.source_session_id] if cand.source_session_id else []
                ),
                source_artifact_ids=(
                    [cand.source_artifact_id] if cand.source_artifact_id else []
                ),
                source_segment_ids=cand.source_segment_ids,
                metadata={
                    **cand.metadata_json,
                    "layer_rationale": verdict.rationale,
                    "layer_source": verdict.source,
                },
            )
            nodes_upserted += 1
            touched.add(product_id)

            # Cross-modal alignment proposals.
            try:
                alignment_result = await self._aligner.align(
                    tenant_id=tenant_id,
                    trace_id=trace_id,
                    product_id=product_id,
                    source_backbone_node_id=cand.backbone_node_id,
                    source_layer=verdict.layer,
                    source_text=cand.text or cand.label,
                    source_segment_ids=cand.source_segment_ids,
                    indexer=self._repo,
                )
            except Exception as exc:  # pragma: no cover — defensive
                logger.exception(
                    "atlas.alignment_failed backbone_node=%s err=%s",
                    cand.backbone_node_id,
                    exc,
                )
                result.errors.append(
                    f"align:{cand.backbone_node_id}:{exc}"
                )
                continue

            persisted = await self._persist_decisions(
                tenant_id=tenant_id,
                product_id=product_id,
                source_atlas_node_id=upsert.atlas_node_id,
                alignment_result=alignment_result,
            )
            edges_auto += persisted.auto_count
            alignments_pending += persisted.pending_count
            if persisted.errors:
                result.errors.extend(persisted.errors)

        if refresh_stats:
            for product_id in touched:
                try:
                    await self._repo.refresh_layer_stats(
                        tenant_id=tenant_id, product_id=product_id
                    )
                except Exception as exc:  # pragma: no cover — defensive
                    logger.warning(
                        "atlas.refresh_stats_failed product=%s err=%s",
                        product_id,
                        exc,
                    )
                    result.errors.append(
                        f"refresh_stats:{product_id}:{exc}"
                    )

        return BuilderResult(
            nodes_upserted=nodes_upserted,
            nodes_skipped_no_product=nodes_skipped_no_product,
            edges_auto=edges_auto,
            alignments_pending=alignments_pending,
            products_touched=tuple(sorted(touched)),
            errors=result.errors,
        )

    # ── Internals ───────────────────────────────────────────────

    async def _resolve_product(
        self,
        cand: NodeCandidate,
        resolver: ProductResolver,
    ) -> Optional[str]:
        # 1. Explicit product wins.
        if cand.product_id:
            return cand.product_id
        # 2. Pre-resolved candidates list (e.g. from upstream tagger).
        if cand.product_candidates:
            return cand.product_candidates[0]
        # 3. Resolve from text.
        text = (cand.text or cand.label or "").strip()
        if not text or resolver.is_empty:
            return None
        verdict = resolver.resolve(text)
        return verdict.primary

    async def _persist_decisions(
        self,
        *,
        tenant_id: str,
        product_id: str,
        source_atlas_node_id: str,
        alignment_result: AlignmentResult,
    ) -> "_PersistCounts":
        counts = _PersistCounts()
        for decision in alignment_result.decisions:
            target = await self._repo.get_node_by_backbone(
                tenant_id=tenant_id,
                product_id=product_id,
                backbone_node_id=decision.to_backbone_node_id,
            )
            if target is None:
                # Target hasn't been atlas-projected yet — skip; the
                # aligner already filters to atlas-known nodes but the
                # row may have been deleted between calls.
                continue
            target_id = target["atlas_node_id"]
            if target_id == source_atlas_node_id:
                continue

            if decision.auto_apply:
                try:
                    await self._repo.upsert_edge(
                        tenant_id=tenant_id,
                        product_id=product_id,
                        from_atlas_node_id=source_atlas_node_id,
                        to_atlas_node_id=target_id,
                        relation_type=decision.suggested_relation.value,
                        confidence=decision.confidence,
                        status=EdgeStatus.AUTO.value,
                        evidence=decision.evidence,
                    )
                    counts.auto_count += 1
                except Exception as exc:  # pragma: no cover
                    counts.errors.append(f"edge:{exc}")
                continue
            try:
                await self._repo.upsert_alignment(
                    tenant_id=tenant_id,
                    product_id=product_id,
                    from_atlas_node_id=source_atlas_node_id,
                    to_atlas_node_id=target_id,
                    suggested_relation=decision.suggested_relation.value,
                    similarity=decision.similarity,
                    evidence=decision.evidence,
                    status="pending",
                )
                counts.pending_count += 1
            except Exception as exc:  # pragma: no cover
                counts.errors.append(f"align:{exc}")
        return counts


@dataclass
class _PersistCounts:
    auto_count: int = 0
    pending_count: int = 0
    errors: list[str] = field(default_factory=list)
