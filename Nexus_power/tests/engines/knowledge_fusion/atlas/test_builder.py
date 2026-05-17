"""AtlasBuilder — end-to-end flow with in-memory repo + aligner doubles."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

import pytest

from app.atlas.aligner import AlignmentDecision, AlignmentResult, CrossModalAligner
from app.atlas.builder import AtlasBuilder
from app.atlas.layer_classifier import HeuristicLayerClassifier, LayerVerdict
from app.atlas.models import EdgeStatus, Layer, NodeCandidate, RelationKind
from app.atlas.product_resolver import ProductCatalogEntry, ProductResolver
# Import UpsertResult at module scope so it shares the aligner/builder
# module's ``app.atlas`` reference under conftest's purge regime.
from app.atlas.repository import UpsertResult


# ── Doubles ────────────────────────────────────────────────────


@dataclass
class _NodeRecord:
    atlas_node_id: str
    tenant_id: str
    product_id: str
    backbone_node_id: str
    node_type: str
    layer: str
    label: str
    confidence: float
    source_session_ids: list[str] = field(default_factory=list)
    source_artifact_ids: list[str] = field(default_factory=list)
    source_segment_ids: list[str] = field(default_factory=list)
    metadata_json: dict[str, Any] = field(default_factory=dict)
    last_seen_at: Optional[datetime] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "atlas_node_id": self.atlas_node_id,
            "tenant_id": self.tenant_id,
            "product_id": self.product_id,
            "backbone_node_id": self.backbone_node_id,
            "node_type": self.node_type,
            "layer": self.layer,
            "label": self.label,
            "confidence": self.confidence,
            "source_session_ids": list(self.source_session_ids),
            "source_artifact_ids": list(self.source_artifact_ids),
            "source_segment_ids": list(self.source_segment_ids),
            "metadata_json": dict(self.metadata_json),
            "last_seen_at": self.last_seen_at,
        }


@dataclass
class _EdgeRecord:
    edge_id: str
    tenant_id: str
    product_id: str
    from_atlas_node_id: str
    to_atlas_node_id: str
    relation_type: str
    confidence: float
    status: str
    evidence_json: dict[str, Any]


@dataclass
class _FakeRepo:
    nodes_by_pk: dict[tuple[str, str, str], _NodeRecord] = field(default_factory=dict)
    edges: list[_EdgeRecord] = field(default_factory=list)
    alignments: list[dict[str, Any]] = field(default_factory=list)
    stats_refresh_count: dict[str, int] = field(default_factory=dict)

    async def upsert_node(
        self,
        *,
        tenant_id,
        product_id,
        backbone_node_id,
        node_type,
        layer,
        label,
        confidence,
        source_session_ids=(),
        source_artifact_ids=(),
        source_segment_ids=(),
        metadata=None,
        last_seen_at=None,
    ):
        key = (tenant_id, product_id, backbone_node_id)
        existing = self.nodes_by_pk.get(key)
        if existing is None:
            rec = _NodeRecord(
                atlas_node_id=uuid.uuid4().hex,
                tenant_id=tenant_id,
                product_id=product_id,
                backbone_node_id=backbone_node_id,
                node_type=node_type,
                layer=layer,
                label=label,
                confidence=float(confidence),
                source_session_ids=list(source_session_ids),
                source_artifact_ids=list(source_artifact_ids),
                source_segment_ids=list(source_segment_ids),
                metadata_json=dict(metadata or {}),
                last_seen_at=last_seen_at or datetime.now(timezone.utc),
            )
            self.nodes_by_pk[key] = rec
            return UpsertResult(atlas_node_id=rec.atlas_node_id, created=True)
        # Merge sets.
        existing.layer = layer
        existing.label = label
        existing.confidence = max(existing.confidence, float(confidence))
        existing.source_session_ids = _union(existing.source_session_ids, source_session_ids)
        existing.source_artifact_ids = _union(existing.source_artifact_ids, source_artifact_ids)
        existing.source_segment_ids = _union(existing.source_segment_ids, source_segment_ids)
        existing.metadata_json.update(metadata or {})
        existing.last_seen_at = last_seen_at or datetime.now(timezone.utc)
        return UpsertResult(atlas_node_id=existing.atlas_node_id, created=False)

    async def get_node_by_backbone(self, *, tenant_id, product_id, backbone_node_id):
        rec = self.nodes_by_pk.get((tenant_id, product_id, backbone_node_id))
        return rec.as_dict() if rec else None

    async def upsert_edge(
        self,
        *,
        tenant_id,
        product_id,
        from_atlas_node_id,
        to_atlas_node_id,
        relation_type,
        confidence,
        status,
        evidence,
    ):
        edge_id = uuid.uuid4().hex
        self.edges.append(
            _EdgeRecord(
                edge_id=edge_id,
                tenant_id=tenant_id,
                product_id=product_id,
                from_atlas_node_id=from_atlas_node_id,
                to_atlas_node_id=to_atlas_node_id,
                relation_type=relation_type,
                confidence=float(confidence),
                status=status,
                evidence_json=dict(evidence or {}),
            )
        )
        return edge_id

    async def upsert_alignment(
        self,
        *,
        tenant_id,
        product_id,
        from_atlas_node_id,
        to_atlas_node_id,
        suggested_relation,
        similarity,
        evidence,
        status="pending",
    ):
        align_id = uuid.uuid4().hex
        self.alignments.append(
            {
                "alignment_id": align_id,
                "tenant_id": tenant_id,
                "product_id": product_id,
                "from_atlas_node_id": from_atlas_node_id,
                "to_atlas_node_id": to_atlas_node_id,
                "suggested_relation": suggested_relation,
                "similarity": similarity,
                "evidence_json": evidence,
                "status": status,
            }
        )
        return align_id

    async def refresh_layer_stats(self, *, tenant_id, product_id):
        self.stats_refresh_count[product_id] = self.stats_refresh_count.get(product_id, 0) + 1
        return {}


class _FakeAligner:
    """Returns a canned set of AlignmentDecisions per source backbone id."""

    def __init__(self, by_source: dict[str, list[AlignmentDecision]]):
        self._by = by_source
        self.calls: list[tuple[str, Layer]] = []

    async def align(
        self,
        *,
        tenant_id,
        trace_id,
        product_id,
        source_backbone_node_id,
        source_layer,
        source_text,
        source_segment_ids,
        indexer,
    ):
        self.calls.append((source_backbone_node_id, source_layer))
        return AlignmentResult(
            decisions=list(self._by.get(source_backbone_node_id, [])),
            queried=len(self._by.get(source_backbone_node_id, [])),
        )


def _union(a: Iterable[str], b: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for v in list(a) + list(b):
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


# ── Helpers ────────────────────────────────────────────────────


def _resolver() -> ProductResolver:
    return ProductResolver(
        [
            ProductCatalogEntry(
                product_id="p_lt5",
                name="LT5 Term Life",
                slug="lt5",
                aliases=("LT-5",),
            ),
        ]
    )


def _cand(
    *,
    text: str = "LT5 quote tobacco lookback is 24 months in CA.",
    backbone_node_id: str = "seg-1",
    product_id: Optional[str] = None,
    label: str = "tobacco lookback",
    segments: tuple[str, ...] = ("seg-1",),
) -> NodeCandidate:
    return NodeCandidate(
        tenant_id="t1",
        backbone_node_id=backbone_node_id,
        node_type="TranscriptSegment",
        label=label,
        text=text,
        product_id=product_id,
        source_session_id="sess-1",
        source_artifact_id="art-1",
        source_segment_ids=list(segments),
    )


# ── Tests ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_builder_upserts_node_when_product_resolves() -> None:
    repo = _FakeRepo()
    aligner = _FakeAligner({})
    builder = AtlasBuilder(
        repo=repo,  # type: ignore[arg-type]
        backbone=None,  # type: ignore[arg-type]
        classifier=HeuristicLayerClassifier(),
        aligner=aligner,  # type: ignore[arg-type]
    )
    result = await builder.build(
        tenant_id="t1",
        trace_id="tr-1",
        candidates=[_cand()],
        product_resolver=_resolver(),
    )
    assert result.nodes_upserted == 1
    assert result.nodes_skipped_no_product == 0
    assert result.products_touched == ("p_lt5",)
    assert repo.stats_refresh_count["p_lt5"] == 1


@pytest.mark.asyncio
async def test_builder_skips_when_no_product_match() -> None:
    repo = _FakeRepo()
    builder = AtlasBuilder(
        repo=repo,  # type: ignore[arg-type]
        backbone=None,  # type: ignore[arg-type]
        aligner=_FakeAligner({}),  # type: ignore[arg-type]
    )
    cand = _cand(text="No products mentioned here.")
    result = await builder.build(
        tenant_id="t1",
        trace_id="tr-1",
        candidates=[cand],
        product_resolver=_resolver(),
    )
    assert result.nodes_skipped_no_product == 1
    assert result.nodes_upserted == 0
    assert repo.stats_refresh_count == {}


@pytest.mark.asyncio
async def test_builder_explicit_product_overrides_resolver() -> None:
    repo = _FakeRepo()
    builder = AtlasBuilder(
        repo=repo,  # type: ignore[arg-type]
        backbone=None,  # type: ignore[arg-type]
        aligner=_FakeAligner({}),  # type: ignore[arg-type]
    )
    # Text doesn't mention a product, but candidate carries one.
    cand = _cand(text="Unrelated discussion.", product_id="p_lt5")
    result = await builder.build(
        tenant_id="t1",
        trace_id="tr-1",
        candidates=[cand],
        product_resolver=_resolver(),
    )
    assert result.nodes_upserted == 1


@pytest.mark.asyncio
async def test_builder_auto_applies_high_confidence_alignment() -> None:
    repo = _FakeRepo()
    # Pre-create the target atlas node so the builder can resolve it.
    await repo.upsert_node(
        tenant_id="t1",
        product_id="p_lt5",
        backbone_node_id="api-1",
        node_type="APIEndpoint",
        layer="application",
        label="/quote/generate",
        confidence=0.95,
    )
    aligner = _FakeAligner(
        {
            "seg-1": [
                AlignmentDecision(
                    to_backbone_node_id="api-1",
                    suggested_relation=RelationKind.CALLS_API,
                    confidence=0.93,
                    similarity=0.93,
                    auto_apply=True,
                    evidence={},
                )
            ]
        }
    )
    builder = AtlasBuilder(
        repo=repo,  # type: ignore[arg-type]
        backbone=None,  # type: ignore[arg-type]
        aligner=aligner,  # type: ignore[arg-type]
    )
    result = await builder.build(
        tenant_id="t1",
        trace_id="tr-1",
        candidates=[_cand()],
        product_resolver=_resolver(),
    )
    assert result.edges_auto == 1
    assert result.alignments_pending == 0
    assert len(repo.edges) == 1
    assert repo.edges[0].status == "auto"


@pytest.mark.asyncio
async def test_builder_queues_pending_alignment_when_medium_confidence() -> None:
    repo = _FakeRepo()
    await repo.upsert_node(
        tenant_id="t1",
        product_id="p_lt5",
        backbone_node_id="api-1",
        node_type="APIEndpoint",
        layer="application",
        label="/quote",
        confidence=0.95,
    )
    aligner = _FakeAligner(
        {
            "seg-1": [
                AlignmentDecision(
                    to_backbone_node_id="api-1",
                    suggested_relation=RelationKind.CALLS_API,
                    confidence=0.72,
                    similarity=0.72,
                    auto_apply=False,
                    evidence={"visual_overlap": 0},
                )
            ]
        }
    )
    builder = AtlasBuilder(
        repo=repo,  # type: ignore[arg-type]
        backbone=None,  # type: ignore[arg-type]
        aligner=aligner,  # type: ignore[arg-type]
    )
    result = await builder.build(
        tenant_id="t1",
        trace_id="tr-1",
        candidates=[_cand()],
        product_resolver=_resolver(),
    )
    assert result.edges_auto == 0
    assert result.alignments_pending == 1
    assert repo.alignments[0]["status"] == "pending"


@pytest.mark.asyncio
async def test_builder_alignment_to_unknown_target_skipped() -> None:
    repo = _FakeRepo()
    aligner = _FakeAligner(
        {
            "seg-1": [
                AlignmentDecision(
                    to_backbone_node_id="ghost-9",
                    suggested_relation=RelationKind.CALLS_API,
                    confidence=0.95,
                    similarity=0.95,
                    auto_apply=True,
                    evidence={},
                )
            ]
        }
    )
    builder = AtlasBuilder(
        repo=repo,  # type: ignore[arg-type]
        backbone=None,  # type: ignore[arg-type]
        aligner=aligner,  # type: ignore[arg-type]
    )
    result = await builder.build(
        tenant_id="t1",
        trace_id="tr-1",
        candidates=[_cand()],
        product_resolver=_resolver(),
    )
    assert result.edges_auto == 0
    assert result.alignments_pending == 0
