"""Matcher — confidence banding + raw-row hydration."""

from __future__ import annotations

import pytest

from app.matcher import Matcher


class _FakeBackbone:
    def __init__(self, results):
        self._results = results
        self.last_call: dict | None = None

    async def search(self, **kwargs):
        self.last_call = kwargs
        return self._results


@pytest.mark.asyncio
async def test_high_band_threshold() -> None:
    fake = _FakeBackbone(
        [
            {
                "node_id": "n1",
                "node_type": "TranscriptSegment",
                "similarity": 0.92,
                "properties": {
                    "text": "California cigar lookback is 24 months.",
                    "speaker_id": "priya",
                    "speaker_role": "underwriting",
                    "session_id": "sess-1",
                    "artifact_id": "art-1",
                    "start_ms": 1000,
                    "end_ms": 4000,
                    "ordinal": 3,
                    "product_ids": ["lt5"],
                },
            },
            {
                "node_id": "n2",
                "node_type": "BusinessRule",
                "similarity": 0.71,
                "properties": {"rule_text": "Tobacco class drives rates."},
            },
        ]
    )
    matcher = Matcher(fake, high_threshold=0.85, medium_threshold=0.65)
    result = await matcher.match(
        tenant_id="t", trace_id="tr", query="ca tobacco lookback", limit=5
    )
    assert result.top_similarity == pytest.approx(0.92)
    assert result.confidence_band == "high"
    assert len(result.candidates) == 2
    top = result.candidates[0]
    assert top.speaker_id == "priya"
    assert top.product_ids == ("lt5",)


@pytest.mark.asyncio
async def test_medium_band() -> None:
    fake = _FakeBackbone(
        [{"node_id": "n1", "node_type": "X", "similarity": 0.72, "properties": {"text": "x"}}]
    )
    matcher = Matcher(fake)
    result = await matcher.match(
        tenant_id="t", trace_id="tr", query="x", limit=5
    )
    assert result.confidence_band == "medium"


@pytest.mark.asyncio
async def test_no_results_is_none_band() -> None:
    fake = _FakeBackbone([])
    matcher = Matcher(fake)
    result = await matcher.match(
        tenant_id="t", trace_id="tr", query="x", limit=5
    )
    assert result.is_empty
    assert result.confidence_band == "none"
    assert result.top_similarity == 0.0


@pytest.mark.asyncio
async def test_empty_query_returns_empty_result() -> None:
    fake = _FakeBackbone([{"node_id": "n", "similarity": 1.0, "properties": {}}])
    matcher = Matcher(fake)
    result = await matcher.match(
        tenant_id="t", trace_id="tr", query="   ", limit=5
    )
    assert result.is_empty
    assert fake.last_call is None  # should not even call backbone


@pytest.mark.asyncio
async def test_sorted_descending_by_similarity() -> None:
    fake = _FakeBackbone(
        [
            {"node_id": "a", "similarity": 0.7, "properties": {"text": "a"}},
            {"node_id": "b", "similarity": 0.9, "properties": {"text": "b"}},
            {"node_id": "c", "similarity": 0.8, "properties": {"text": "c"}},
        ]
    )
    matcher = Matcher(fake)
    result = await matcher.match(
        tenant_id="t", trace_id="tr", query="q", limit=5
    )
    sims = [c.similarity for c in result.candidates]
    assert sims == sorted(sims, reverse=True)


def test_threshold_validation() -> None:
    fake = _FakeBackbone([])
    with pytest.raises(ValueError):
        Matcher(fake, high_threshold=0.5, medium_threshold=0.8)  # inverted
