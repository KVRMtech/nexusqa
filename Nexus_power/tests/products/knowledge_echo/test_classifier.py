"""Classifier — heuristic fallback + LLM happy path.

LLM path is exercised through a fake ``OllamaJsonClient`` so no network
calls are made.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

from app.classifier import (
    ClassifierOutput,
    QuestionClassifier,
    SenderContext,
    _heuristic_fallback,
)
from app.llm import LLMError


class _FakeLLM:
    """Drop-in for OllamaJsonClient that returns a canned dict."""

    def __init__(self, payload: Any):
        self._payload = payload
        self.calls: list[dict[str, Any]] = []

    async def chat_json(self, *, model, messages, **kw):
        self.calls.append(
            {"model": model, "messages": [m.to_dict() for m in messages]}
        )
        if isinstance(self._payload, BaseException):
            raise self._payload
        return self._payload


class _CountingCache:
    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.gets = 0
        self.sets = 0

    async def get(self, key):
        self.gets += 1
        return self.store.get(key)

    async def setex(self, key, ttl, value):  # noqa: ARG002
        self.sets += 1
        self.store[key] = value


@pytest.mark.asyncio
async def test_llm_path_returns_valid_output() -> None:
    payload = {
        "is_question": True,
        "confidence": 0.92,
        "question_type": "policy",
        "domain_hints": ["underwriting/tobacco"],
        "product_hints": ["LT5"],
        "jurisdiction_hints": ["CA"],
        "urgency": "medium",
        "rationale_short": "Direct policy inquiry.",
    }
    cache = _CountingCache()
    classifier = QuestionClassifier(
        llm=_FakeLLM(payload),
        model="llama3.2:1b",
        cache=cache,
    )
    out = await classifier.classify(
        text="Does CA still use a 24-month tobacco lookback for LT5?",
        sender=SenderContext(surface="slack"),
    )
    assert out.is_question is True
    assert out.confidence == 0.92
    assert out.product_hints == ["LT5"]
    assert cache.sets == 1


@pytest.mark.asyncio
async def test_cache_hit_skips_llm() -> None:
    payload = {
        "is_question": True,
        "confidence": 0.8,
        "question_type": "factual",
        "rationale_short": "",
    }
    cache = _CountingCache()
    fake_llm = _FakeLLM(payload)
    classifier = QuestionClassifier(
        llm=fake_llm, model="llama3.2:1b", cache=cache
    )
    text = "what is the tobacco lookback in CA?"
    await classifier.classify(text=text, sender=SenderContext(surface="slack"))
    await classifier.classify(text=text, sender=SenderContext(surface="slack"))
    assert len(fake_llm.calls) == 1, "second call should hit cache"


@pytest.mark.asyncio
async def test_llm_error_falls_back_to_heuristic() -> None:
    classifier = QuestionClassifier(
        llm=_FakeLLM(LLMError("boom")),
        model="llama3.2:1b",
        cache=_CountingCache(),
    )
    out = await classifier.classify(
        text="What is the tobacco lookback in CA?",
        sender=SenderContext(surface="slack"),
    )
    assert isinstance(out, ClassifierOutput)
    assert "fallback" in out.rationale_short.lower()


@pytest.mark.asyncio
async def test_invalid_llm_output_falls_back() -> None:
    """Model returns garbage → we use the heuristic, don't crash."""
    classifier = QuestionClassifier(
        llm=_FakeLLM({"is_question": "not a bool"}),  # invalid type
        model="llama3.2:1b",
        cache=_CountingCache(),
    )
    out = await classifier.classify(
        text="Why?",
        sender=SenderContext(surface="slack"),
    )
    assert isinstance(out, ClassifierOutput)


@pytest.mark.asyncio
async def test_empty_text_returns_not_a_question() -> None:
    classifier = QuestionClassifier(
        llm=_FakeLLM({}),
        model="llama3.2:1b",
        cache=_CountingCache(),
    )
    out = await classifier.classify(
        text="", sender=SenderContext(surface="slack")
    )
    assert out.is_question is False
    assert out.confidence == 1.0


def test_heuristic_recognises_question_words_and_marks() -> None:
    out = _heuristic_fallback("What is the eligibility rule for CA?")
    assert out.is_question is True
    assert out.confidence >= 0.7


def test_heuristic_declines_statements() -> None:
    out = _heuristic_fallback("Closing the loop on the ticket.")
    assert out.is_question is False


def test_classifier_output_dedups_hints() -> None:
    out = ClassifierOutput.model_validate(
        {
            "is_question": True,
            "confidence": 0.5,
            "domain_hints": ["foo", "Foo", "FOO", "bar"],
            "product_hints": [],
            "jurisdiction_hints": [],
        }
    )
    # Case-insensitive dedup keeps first occurrence.
    assert out.domain_hints == ["foo", "bar"]
