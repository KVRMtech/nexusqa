"""CrossModalAligner — proposes auto/pending edges between atlas nodes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import pytest

# Import the *same* BackboneClientError that the aligner sees so the
# error-handling test's raised exception type matches the aligner's
# ``except`` clause regardless of test-collection module-purge order.
from app.atlas.aligner import (
    AlignerConfig,
    CrossModalAligner,
)
from app.atlas.models import Layer, RelationKind
from app.backbone_client import BackboneClientError


# ── Doubles ────────────────────────────────────────────────────


@dataclass
class _FakeBackbone:
    rows: list[dict[str, Any]]

    async def search(
        self,
        *,
        tenant_id,
        trace_id,
        query,
        node_types=None,
        limit=10,
        min_similarity=0.0,
    ):
        return [r for r in self.rows if r.get("similarity", 0.0) >= min_similarity][:limit]


class _FakeIndexer:
    def __init__(self, nodes: dict[str, dict[str, Any]]):
        # Keyed by backbone_node_id; value is the atlas row.
        self._nodes = nodes
        self.calls: list[tuple[str, str]] = []

    async def get_node_by_backbone(self, *, tenant_id, product_id, backbone_node_id):
        self.calls.append((tenant_id, backbone_node_id))
        return self._nodes.get(backbone_node_id)


# ── Helpers ────────────────────────────────────────────────────


def _row(node_id: str, similarity: float) -> dict[str, Any]:
    return {
        "node_id": node_id,
        "node_type": "TranscriptSegment",
        "similarity": similarity,
        "properties": {"text": "sample"},
    }


def _atlas(node_id: str, layer: str, segments: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "atlas_node_id": f"atlas-{node_id}",
        "backbone_node_id": node_id,
        "layer": layer,
        "node_type": "TranscriptSegment",
        "source_segment_ids": list(segments),
    }


# ── Tests ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_high_similarity_proposes_auto_apply_with_correct_relation() -> None:
    backbone = _FakeBackbone([_row("api-1", 0.95)])
    indexer = _FakeIndexer({"api-1": _atlas("api-1", "application")})
    aligner = CrossModalAligner(backbone)  # type: ignore[arg-type]

    result = await aligner.align(
        tenant_id="t1",
        trace_id="tr-1",
        product_id="p_lt5",
        source_backbone_node_id="ui-1",
        source_layer=Layer.EXPERIENCE,
        source_text="The Get Quote button calls /api/v2/quote/generate.",
        source_segment_ids=("seg-1",),
        indexer=indexer,
    )
    assert len(result.decisions) == 1
    d = result.decisions[0]
    assert d.to_backbone_node_id == "api-1"
    assert d.auto_apply is True
    assert d.suggested_relation == RelationKind.CALLS_API


@pytest.mark.asyncio
async def test_visual_overlap_boosts_confidence() -> None:
    backbone = _FakeBackbone([_row("api-1", 0.78)])
    indexer = _FakeIndexer(
        {"api-1": _atlas("api-1", "application", segments=("seg-shared",))}
    )
    aligner = CrossModalAligner(
        backbone,  # type: ignore[arg-type]
        config=AlignerConfig(similarity_floor=0.7, auto_apply_floor=0.85),
    )
    result = await aligner.align(
        tenant_id="t1",
        trace_id="tr-1",
        product_id="p_lt5",
        source_backbone_node_id="ui-1",
        source_layer=Layer.EXPERIENCE,
        source_text="Submit triggers an API call.",
        source_segment_ids=("seg-shared", "seg-other"),
        indexer=indexer,
    )
    assert len(result.decisions) == 1
    d = result.decisions[0]
    # 0.78 + 0.12 visual_overlap_boost = 0.90 → auto_apply tips True
    assert d.confidence >= 0.85
    assert d.auto_apply is True
    assert d.evidence["visual_overlap"] == 1


@pytest.mark.asyncio
async def test_medium_similarity_pending_review() -> None:
    backbone = _FakeBackbone([_row("api-1", 0.72)])
    indexer = _FakeIndexer({"api-1": _atlas("api-1", "application")})
    aligner = CrossModalAligner(backbone)  # type: ignore[arg-type]
    result = await aligner.align(
        tenant_id="t1",
        trace_id="tr-1",
        product_id="p_lt5",
        source_backbone_node_id="ui-1",
        source_layer=Layer.EXPERIENCE,
        source_text="Generic text without segment overlap.",
        source_segment_ids=(),
        indexer=indexer,
    )
    assert len(result.decisions) == 1
    d = result.decisions[0]
    assert d.auto_apply is False


@pytest.mark.asyncio
async def test_self_match_filtered() -> None:
    backbone = _FakeBackbone([_row("ui-1", 0.99)])
    indexer = _FakeIndexer({"ui-1": _atlas("ui-1", "experience")})
    aligner = CrossModalAligner(backbone)  # type: ignore[arg-type]
    result = await aligner.align(
        tenant_id="t1",
        trace_id="tr-1",
        product_id="p_lt5",
        source_backbone_node_id="ui-1",
        source_layer=Layer.EXPERIENCE,
        source_text="anything",
        source_segment_ids=(),
        indexer=indexer,
    )
    assert result.decisions == []


@pytest.mark.asyncio
async def test_unknown_target_skipped() -> None:
    backbone = _FakeBackbone([_row("ghost", 0.95)])
    indexer = _FakeIndexer({})  # no atlas row for "ghost"
    aligner = CrossModalAligner(backbone)  # type: ignore[arg-type]
    result = await aligner.align(
        tenant_id="t1",
        trace_id="tr-1",
        product_id="p_lt5",
        source_backbone_node_id="ui-1",
        source_layer=Layer.EXPERIENCE,
        source_text="anything",
        source_segment_ids=(),
        indexer=indexer,
    )
    assert result.decisions == []


@pytest.mark.asyncio
async def test_same_layer_no_relation_filtered() -> None:
    # data <-> data has no entry in the relation map (not nav/contain etc).
    backbone = _FakeBackbone([_row("db-2", 0.95)])
    indexer = _FakeIndexer({"db-2": _atlas("db-2", "data")})
    aligner = CrossModalAligner(backbone)  # type: ignore[arg-type]
    result = await aligner.align(
        tenant_id="t1",
        trace_id="tr-1",
        product_id="p_lt5",
        source_backbone_node_id="db-1",
        source_layer=Layer.DATA,
        source_text="anything",
        source_segment_ids=(),
        indexer=indexer,
    )
    assert result.decisions == []


@pytest.mark.asyncio
async def test_navigates_to_for_same_experience_layer() -> None:
    backbone = _FakeBackbone([_row("ui-2", 0.92)])
    indexer = _FakeIndexer({"ui-2": _atlas("ui-2", "experience")})
    aligner = CrossModalAligner(backbone)  # type: ignore[arg-type]
    result = await aligner.align(
        tenant_id="t1",
        trace_id="tr-1",
        product_id="p_lt5",
        source_backbone_node_id="ui-1",
        source_layer=Layer.EXPERIENCE,
        source_text="anything",
        source_segment_ids=(),
        indexer=indexer,
    )
    assert len(result.decisions) == 1
    assert result.decisions[0].suggested_relation == RelationKind.NAVIGATES_TO


@pytest.mark.asyncio
async def test_test_layer_to_rule_proposes_tests_rule() -> None:
    backbone = _FakeBackbone([_row("rule-1", 0.90)])
    indexer = _FakeIndexer({"rule-1": _atlas("rule-1", "rule")})
    aligner = CrossModalAligner(backbone)  # type: ignore[arg-type]
    result = await aligner.align(
        tenant_id="t1",
        trace_id="tr-1",
        product_id="p_lt5",
        source_backbone_node_id="test-1",
        source_layer=Layer.TEST,
        source_text="any",
        source_segment_ids=(),
        indexer=indexer,
    )
    assert len(result.decisions) == 1
    assert result.decisions[0].suggested_relation == RelationKind.TESTS_RULE


@pytest.mark.asyncio
async def test_backbone_error_caught_in_errors_list() -> None:
    @dataclass
    class _ExplodingBackbone:
        async def search(self, **kwargs):
            raise BackboneClientError("down")

    aligner = CrossModalAligner(_ExplodingBackbone())  # type: ignore[arg-type]
    result = await aligner.align(
        tenant_id="t1",
        trace_id="tr-1",
        product_id="p_lt5",
        source_backbone_node_id="x",
        source_layer=Layer.EXPERIENCE,
        source_text="anything",
        source_segment_ids=(),
        indexer=_FakeIndexer({}),
    )
    assert result.decisions == []
    assert result.errors and "backbone_search" in result.errors[0]


def test_aligner_config_validates() -> None:
    with pytest.raises(ValueError):
        AlignerConfig(similarity_floor=0.9, auto_apply_floor=0.5)
    with pytest.raises(ValueError):
        AlignerConfig(similarity_floor=-0.1)
