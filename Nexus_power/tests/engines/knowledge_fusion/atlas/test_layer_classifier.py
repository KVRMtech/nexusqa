"""HeuristicLayerClassifier — type-map + regex + LLM fallback."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.atlas.layer_classifier import (
    ChatMessage,
    HeuristicLayerClassifier,
    LayerVerdict,
)
from app.atlas.models import Layer


@pytest.mark.asyncio
async def test_explicit_hint_wins() -> None:
    c = HeuristicLayerClassifier()
    v = await c.classify(
        node_type="UIScreen",
        text="POST /api/v2/quote",  # would otherwise heuristic to application
        layer_hint="data",
    )
    assert v.layer == Layer.DATA
    assert v.source == "type_map"
    assert v.confidence >= 0.95


@pytest.mark.asyncio
async def test_type_map_for_ui_screen() -> None:
    c = HeuristicLayerClassifier()
    v = await c.classify(node_type="UIScreen", text="Generic explanation.")
    assert v.layer == Layer.EXPERIENCE
    assert v.source == "type_map"


@pytest.mark.asyncio
async def test_heuristic_for_api_text_overrides_ambiguous_type() -> None:
    """TranscriptSegment is ambiguous (rule-by-default at 0.6); an API
    keyword should win via the higher-confidence heuristic."""
    c = HeuristicLayerClassifier()
    v = await c.classify(
        node_type="TranscriptSegment",
        text="The frontend calls POST /api/v2/quote/generate on submit.",
    )
    assert v.layer == Layer.APPLICATION
    assert v.source == "heuristic"


@pytest.mark.asyncio
async def test_heuristic_for_sql_text() -> None:
    c = HeuristicLayerClassifier()
    v = await c.classify(
        node_type="TranscriptSegment",
        text="SELECT * FROM rate_tables WHERE state = 'CA';",
    )
    assert v.layer == Layer.DATA


@pytest.mark.asyncio
async def test_heuristic_for_compliance_text() -> None:
    c = HeuristicLayerClassifier()
    v = await c.classify(
        node_type="TranscriptSegment",
        text="Per NAIC bulletin 2025-03, tobacco classification requires...",
    )
    assert v.layer == Layer.COMPLIANCE


@pytest.mark.asyncio
async def test_heuristic_for_ops_text() -> None:
    c = HeuristicLayerClassifier()
    v = await c.classify(
        node_type="TranscriptSegment",
        text="The PagerDuty alert fires when p95 latency exceeds 4s.",
    )
    assert v.layer == Layer.OPS


@pytest.mark.asyncio
async def test_default_when_no_signal() -> None:
    c = HeuristicLayerClassifier()
    v = await c.classify(
        node_type="NoSuchType",
        text="The weather is nice today.",
    )
    assert v.source in ("default", "heuristic")
    assert 0.0 <= v.confidence <= 1.0


@pytest.mark.asyncio
async def test_llm_fallback_invoked_for_ambiguous_input() -> None:
    """When type-map and heuristics give low confidence, the LLM is asked."""

    @dataclass
    class _FakeLLM:
        async def chat_json(self, *, model, messages, temperature=0.0):
            assert isinstance(messages[0], ChatMessage)
            return {
                "layer": "ops",
                "confidence": 0.88,
                "rationale": "looks like an incident playbook",
            }

    c = HeuristicLayerClassifier(
        llm=_FakeLLM(),
        llm_model="llama3.2:1b",
        confidence_threshold_for_llm=0.9,  # force LLM use for our test
    )
    v = await c.classify(
        node_type="TranscriptSegment",
        text="When alpha breakpoint trips, gamma cascade restart sequence.",
    )
    assert v.source == "llm"
    assert v.layer == Layer.OPS
    assert v.confidence == 0.88


@pytest.mark.asyncio
async def test_llm_invalid_falls_back_to_heuristic() -> None:
    @dataclass
    class _BadLLM:
        async def chat_json(self, *, model, messages, temperature=0.0):
            return {"layer": "not_a_layer", "confidence": 0.9}

    c = HeuristicLayerClassifier(
        llm=_BadLLM(),
        llm_model="llama3.2:1b",
        confidence_threshold_for_llm=0.99,
    )
    v = await c.classify(
        node_type="TranscriptSegment",
        text="Some generic statement about policy",
    )
    assert isinstance(v, LayerVerdict)
    assert v.source in ("heuristic", "type_map", "default")


@pytest.mark.asyncio
async def test_llm_transport_error_falls_back() -> None:
    @dataclass
    class _ExplodingLLM:
        async def chat_json(self, *, model, messages, temperature=0.0):
            raise RuntimeError("timeout")

    c = HeuristicLayerClassifier(
        llm=_ExplodingLLM(),
        llm_model="llama3.2:1b",
        confidence_threshold_for_llm=0.99,
    )
    v = await c.classify(node_type="TranscriptSegment", text="weak signal")
    assert isinstance(v, LayerVerdict)
    assert v.source in ("heuristic", "type_map", "default")
