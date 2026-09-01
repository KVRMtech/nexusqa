"""CardSynthesizer — branch coverage with in-memory repo + backbone.

These tests do not touch Postgres. The repository and backbone client
are replaced with deterministic in-memory doubles so we can assert
exactly which paths fire on which inputs.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Optional

import pytest

from app.backbone_client import BackboneClientError
from app.cards.models import SourceCandidate, SourceType
from app.cards.synthesizer import (
    CardSynthesizer,
    SynthesisConfig,
)


# ── In-memory doubles ───────────────────────────────────────────


@dataclass
class _FakeCard:
    card_id: str
    tenant_id: str
    topic_slug: str
    topic_label: str
    canonical_statement: str
    canonical_confidence: float = 0.0
    consensus_score: float = 0.0
    lifecycle_state: str = "tribal"
    authority_chain: list[dict] = field(default_factory=list)
    contributing_count: int = 0
    dissent_count: int = 0
    product_id: Optional[str] = None
    jurisdiction: Optional[str] = None
    halflife_days: int = 270
    superseded_by: Optional[str] = None
    backbone_node_id: Optional[str] = None
    tags: list[str] = field(default_factory=list)
    metadata_json: dict = field(default_factory=dict)
    last_verified_at: Optional[datetime] = None
    verify_due_at: Optional[datetime] = None
    version: int = 1
    created_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    updated_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    def as_dict(self) -> dict[str, Any]:
        return {
            "card_id": self.card_id,
            "tenant_id": self.tenant_id,
            "topic_slug": self.topic_slug,
            "topic_label": self.topic_label,
            "canonical_statement": self.canonical_statement,
            "canonical_confidence": self.canonical_confidence,
            "consensus_score": self.consensus_score,
            "lifecycle_state": self.lifecycle_state,
            "authority_chain": list(self.authority_chain),
            "contributing_count": self.contributing_count,
            "dissent_count": self.dissent_count,
            "product_id": self.product_id,
            "jurisdiction": self.jurisdiction,
            "superseded_by": self.superseded_by,
            "halflife_days": self.halflife_days,
            "backbone_node_id": self.backbone_node_id,
            "tags": list(self.tags),
            "metadata_json": dict(self.metadata_json),
            "version": self.version,
            "last_verified_at": self.last_verified_at,
            "verify_due_at": self.verify_due_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class _FakeSource:
    id: int
    card_id: str
    source_type: str
    source_id: str
    sme_id: Optional[str]
    sme_role: Optional[str]
    stated_at: Optional[date]
    similarity_to_canonical: Optional[float]
    weight: float
    status: str
    metadata: dict
    backbone_node_id: Optional[str]


class _FakeRepo:
    def __init__(self) -> None:
        self.cards: dict[str, _FakeCard] = {}
        self.sources: list[_FakeSource] = []
        self.history: list[dict] = []
        self.next_source_id = 1

    async def get_authority_overrides(self, tenant_id: str) -> dict[str, float]:
        return {}

    async def get(self, *, tenant_id: str, card_id: str):
        c = self.cards.get(card_id)
        if c is None or c.tenant_id != tenant_id:
            return None
        return c.as_dict()

    async def list_sources(
        self, *, tenant_id: str, card_id: str
    ) -> list[dict]:
        return [
            {
                "id": s.id,
                "source_id": s.source_id,
                "sme_id": s.sme_id,
                "sme_role": s.sme_role,
                "weight": s.weight,
                "status": s.status,
                "stated_at": s.stated_at,
            }
            for s in self.sources
            if s.card_id == card_id
        ]

    async def create_card(
        self,
        *,
        tenant_id,
        topic_slug,
        topic_label,
        canonical_statement,
        product_id,
        jurisdiction,
        tags,
        halflife_days,
        changed_by,
    ):
        card_id = uuid.uuid4().hex
        card = _FakeCard(
            card_id=card_id,
            tenant_id=tenant_id,
            topic_slug=topic_slug,
            topic_label=topic_label,
            canonical_statement=canonical_statement,
            product_id=product_id,
            jurisdiction=jurisdiction,
            halflife_days=halflife_days,
            tags=list(tags or []),
        )
        self.cards[card_id] = card
        self.history.append(
            {"card_id": card_id, "change_type": "created", "by": changed_by}
        )
        return card.as_dict()

    async def add_source(
        self,
        *,
        tenant_id,
        card_id,
        source_type,
        source_id,
        backbone_node_id,
        session_id,
        artifact_id,
        sme_id,
        sme_role,
        stated_at,
        similarity_to_canonical,
        weight,
        status,
        metadata,
        changed_by,
    ):
        if any(
            s.card_id == card_id
            and s.source_type == source_type
            and s.source_id == source_id
            for s in self.sources
        ):
            return None
        new = _FakeSource(
            id=self.next_source_id,
            card_id=card_id,
            source_type=source_type,
            source_id=source_id,
            sme_id=sme_id,
            sme_role=sme_role,
            stated_at=stated_at,
            similarity_to_canonical=similarity_to_canonical,
            weight=weight,
            status=status,
            metadata=metadata or {},
            backbone_node_id=backbone_node_id,
        )
        self.sources.append(new)
        self.next_source_id += 1
        return new

    async def count_sources_by_status(self, *, tenant_id, card_id):
        out: dict[str, int] = {}
        for s in self.sources:
            if s.card_id != card_id:
                continue
            out[s.status] = out.get(s.status, 0) + 1
        return out

    async def sum_active_weight(self, *, tenant_id, card_id) -> float:
        return float(
            sum(s.weight for s in self.sources if s.card_id == card_id and s.status == "active")
        )

    async def update_card(
        self,
        *,
        tenant_id,
        card_id,
        expected_version,
        changes,
        change_type,
        changed_by,
        note=None,
        snapshot_extra=None,
    ):
        c = self.cards[card_id]
        assert c.tenant_id == tenant_id
        assert c.version == expected_version
        for k, v in changes.items():
            setattr(c, k, v)
        c.version += 1
        c.updated_at = datetime.now(timezone.utc)
        self.history.append(
            {"card_id": card_id, "change_type": change_type, "by": changed_by}
        )
        return c.as_dict()


@dataclass
class _BackboneRow:
    """Single search hit returned by the fake Backbone."""

    card_id: str
    similarity: float
    canonical_text: str = "anchor canonical statement"


class _FakeBackbone:
    def __init__(self, search_rows: list[_BackboneRow]):
        self._rows = list(search_rows)
        self.stored_nodes: list[dict] = []
        self.fail_store: Optional[Exception] = None

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
        # Return rows filtered by min_similarity.
        out = []
        for r in self._rows:
            if r.similarity >= min_similarity:
                out.append(
                    {
                        "node_id": "node-" + r.card_id,
                        "node_type": "KnowledgeCard",
                        "similarity": r.similarity,
                        "properties": {
                            "card_id": r.card_id,
                            "canonical_statement": r.canonical_text,
                            "text": r.canonical_text,
                        },
                    }
                )
        return out[: limit]

    async def store_knowledge_card_node(
        self,
        *,
        tenant_id,
        trace_id,
        card_id,
        topic_label,
        canonical_statement,
        product_id=None,
        tags=None,
        properties=None,
    ):
        if self.fail_store is not None:
            raise self.fail_store
        self.stored_nodes.append(
            {
                "card_id": card_id,
                "topic_label": topic_label,
                "canonical_statement": canonical_statement,
            }
        )
        return f"node-{card_id}"


# ── Helpers ─────────────────────────────────────────────────────


def _candidate(
    *,
    source_id: str = "seg-1",
    text: str = "California uses a 24-month tobacco lookback for cigar users.",
    sme_id: str = "priya",
    sme_role: str = "compliance",
    product_id: Optional[str] = None,
) -> SourceCandidate:
    return SourceCandidate(
        source_type=SourceType.SEGMENT,
        source_id=source_id,
        backbone_node_id=f"node-{source_id}",
        text=text,
        session_id="sess-1",
        artifact_id="art-1",
        sme_id=sme_id,
        sme_role=sme_role,
        stated_at=date(2026, 5, 1),
        product_id=product_id,
    )


def _synth(
    *,
    backbone: _FakeBackbone,
    repo: _FakeRepo,
    cfg: Optional[SynthesisConfig] = None,
) -> CardSynthesizer:
    return CardSynthesizer(
        repo=repo,  # type: ignore[arg-type]
        backbone=backbone,  # type: ignore[arg-type]
        config=cfg,
    )


# ── Tests ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_match_creates_new_tribal_card() -> None:
    repo = _FakeRepo()
    backbone = _FakeBackbone([])  # empty search
    synth = _synth(backbone=backbone, repo=repo)
    result = await synth.synthesize_batch(
        tenant_id="t1", trace_id="tr-1", candidates=[_candidate()]
    )
    assert len(result.outcomes) == 1
    out = result.outcomes[0]
    assert out.action == "created_new"
    assert out.card_id is not None
    assert len(repo.cards) == 1
    card = next(iter(repo.cards.values()))
    assert card.lifecycle_state == "tribal"
    assert card.contributing_count == 1
    assert card.backbone_node_id is not None  # registered the node


@pytest.mark.asyncio
async def test_high_similarity_attaches_to_existing_card() -> None:
    repo = _FakeRepo()
    existing = _FakeCard(
        card_id="card-prior",
        tenant_id="t1",
        topic_slug="ca-tobacco-lookback",
        topic_label="CA tobacco lookback",
        canonical_statement="California uses a 24-month tobacco lookback.",
        contributing_count=0,
    )
    repo.cards[existing.card_id] = existing
    backbone = _FakeBackbone(
        [_BackboneRow(card_id="card-prior", similarity=0.93,
                       canonical_text=existing.canonical_statement)]
    )
    synth = _synth(backbone=backbone, repo=repo)
    result = await synth.synthesize_batch(
        tenant_id="t1", trace_id="tr-1", candidates=[_candidate()]
    )
    out = result.outcomes[0]
    assert out.action == "added_to_existing"
    assert out.card_id == "card-prior"
    # exactly one new source row
    assert len([s for s in repo.sources if s.card_id == "card-prior"]) == 1
    src = repo.sources[0]
    assert src.status == "active"


@pytest.mark.asyncio
async def test_contradicting_source_attached_as_dissent() -> None:
    repo = _FakeRepo()
    existing = _FakeCard(
        card_id="card-prior",
        tenant_id="t1",
        topic_slug="ca-tobacco-lookback",
        topic_label="CA tobacco lookback",
        canonical_statement=(
            "California uses a 24-month tobacco lookback for cigar users."
        ),
    )
    repo.cards["card-prior"] = existing
    backbone = _FakeBackbone(
        [_BackboneRow(
            card_id="card-prior",
            similarity=0.78,
            canonical_text=existing.canonical_statement,
        )]
    )
    synth = _synth(backbone=backbone, repo=repo)
    candidate = _candidate(
        source_id="seg-2",
        text="California uses a 12-month tobacco lookback for cigar users.",
    )
    result = await synth.synthesize_batch(
        tenant_id="t1", trace_id="tr-1", candidates=[candidate]
    )
    out = result.outcomes[0]
    assert out.action == "added_to_existing"
    assert out.status == "dissenting"
    assert out.dissent_signal == "numeric_mismatch"
    assert existing.dissent_count == 1
    assert existing.lifecycle_state == "contested"


@pytest.mark.asyncio
async def test_duplicate_source_is_noop() -> None:
    repo = _FakeRepo()
    existing = _FakeCard(
        card_id="card-prior",
        tenant_id="t1",
        topic_slug="ca-tobacco-lookback",
        topic_label="CA tobacco lookback",
        canonical_statement="California uses a 24-month tobacco lookback.",
    )
    repo.cards["card-prior"] = existing
    backbone = _FakeBackbone(
        [_BackboneRow(card_id="card-prior", similarity=0.95,
                       canonical_text=existing.canonical_statement)]
    )
    synth = _synth(backbone=backbone, repo=repo)
    # First pass — attaches.
    await synth.synthesize_batch(
        tenant_id="t1", trace_id="tr-1", candidates=[_candidate()]
    )
    # Second pass — noop.
    result = await synth.synthesize_batch(
        tenant_id="t1", trace_id="tr-1", candidates=[_candidate()]
    )
    assert result.outcomes[0].action == "noop_duplicate"
    assert len([s for s in repo.sources if s.card_id == "card-prior"]) == 1


@pytest.mark.asyncio
async def test_promotion_to_consensus_after_threshold() -> None:
    """3 active sources, no dissent → lifecycle promoted to 'consensus'."""
    repo = _FakeRepo()
    backbone = _FakeBackbone([])  # first one creates new card
    synth = _synth(backbone=backbone, repo=repo)

    # 1st source — creates card.
    await synth.synthesize_batch(
        tenant_id="t1",
        trace_id="tr-1",
        candidates=[
            _candidate(source_id="seg-1", text="CA uses 24-month tobacco lookback.")
        ],
    )
    card = next(iter(repo.cards.values()))
    # 2nd + 3rd — same text → matched and attached.
    backbone._rows = [
        _BackboneRow(
            card_id=card.card_id,
            similarity=0.95,
            canonical_text=card.canonical_statement,
        )
    ]
    await synth.synthesize_batch(
        tenant_id="t1",
        trace_id="tr-2",
        candidates=[
            _candidate(source_id="seg-2", text="CA uses a 24-month tobacco lookback."),
        ],
    )
    await synth.synthesize_batch(
        tenant_id="t1",
        trace_id="tr-3",
        candidates=[
            _candidate(source_id="seg-3", text="CA tobacco lookback is 24 months."),
        ],
    )
    assert card.contributing_count == 3
    assert card.dissent_count == 0
    assert card.lifecycle_state == "consensus"


@pytest.mark.asyncio
async def test_empty_text_skipped() -> None:
    repo = _FakeRepo()
    backbone = _FakeBackbone([])
    synth = _synth(backbone=backbone, repo=repo)
    result = await synth.synthesize_batch(
        tenant_id="t1",
        trace_id="tr-1",
        candidates=[_candidate(text="")],
    )
    assert result.outcomes[0].action == "skipped"
    assert not repo.cards


@pytest.mark.asyncio
async def test_backbone_search_error_captured() -> None:
    repo = _FakeRepo()

    class _ExplodingBackbone(_FakeBackbone):
        async def search(self, **kwargs):
            raise BackboneClientError("vector store down")

    backbone = _ExplodingBackbone([])
    synth = _synth(backbone=backbone, repo=repo)
    result = await synth.synthesize_batch(
        tenant_id="t1", trace_id="tr-1", candidates=[_candidate()]
    )
    assert not result.outcomes  # nothing created
    assert result.errors and "backbone" in result.errors[0]


@pytest.mark.asyncio
async def test_node_register_failure_is_non_fatal() -> None:
    repo = _FakeRepo()
    backbone = _FakeBackbone([])
    backbone.fail_store = BackboneClientError("node service down")
    synth = _synth(backbone=backbone, repo=repo)
    result = await synth.synthesize_batch(
        tenant_id="t1", trace_id="tr-1", candidates=[_candidate()]
    )
    # Card was created; we just couldn't register the node.
    assert result.outcomes[0].action == "created_new"
    card = next(iter(repo.cards.values()))
    assert card.backbone_node_id is None
    assert card.contributing_count == 1


def test_synthesis_config_threshold_validation() -> None:
    with pytest.raises(ValueError):
        SynthesisConfig(dedup_threshold=0.5, related_threshold=0.9)
    with pytest.raises(ValueError):
        SynthesisConfig(dedup_threshold=1.2)
