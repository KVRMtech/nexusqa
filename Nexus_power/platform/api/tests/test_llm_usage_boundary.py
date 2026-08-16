"""M0.6 / T-OB-03 — the LLM HTTP boundary must RETURN the token usage it computes.

The defect: ``routers/llm.py`` built a response from a ``CompletionResponse``
that already carried provider-reported ``prompt_tokens`` / ``completion_tokens``
(the providers normalise OpenAI's ``usage``, Anthropic's ``input_tokens`` and
Ollama's ``prompt_eval_count`` into those two fields) and then returned only
text/provider/model — so every downstream consumer was structurally blind to
spend.  These tests pin the repaired shape, including the error path.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

_API_ROOT = Path(__file__).resolve().parents[1]
if str(_API_ROOT) not in sys.path:
    sys.path.insert(0, str(_API_ROOT))

from app.routers.llm import (  # noqa: E402
    HDR_COMPLETION_TOKENS,
    HDR_PROMPT_TOKENS,
    HDR_TOTAL_TOKENS,
    _usage_headers,
    _usage_payload,
)


def _resp(**kwargs) -> types.SimpleNamespace:
    base = {
        "text": "2", "provider": "anthropic", "model": "claude-sonnet-5",
        "prompt_tokens": None, "completion_tokens": None,
        "cache_read_tokens": None, "cache_creation_tokens": None,
        "retries": 0, "latency_ms": 0, "fell_back": False,
    }
    base.update(kwargs)
    return types.SimpleNamespace(**base)


def test_reported_usage_is_surfaced_with_a_derived_total():
    usage = _usage_payload(_resp(prompt_tokens=340, completion_tokens=12))
    assert usage["prompt_tokens"] == 340
    assert usage["completion_tokens"] == 12
    assert usage["total_tokens"] == 352
    assert usage["provider"] == "anthropic"
    assert usage["model"] == "claude-sonnet-5"


def test_unreported_usage_stays_none_rather_than_zero():
    """"The provider did not report" and "the provider reported zero" differ.

    Flattening them would make a silent reporting regression look like a stream
    of free calls — the exact blindness this milestone removes.
    """
    usage = _usage_payload(_resp())
    assert usage["prompt_tokens"] is None
    assert usage["completion_tokens"] is None
    assert usage["total_tokens"] is None


def test_one_reported_half_still_produces_a_total():
    usage = _usage_payload(_resp(prompt_tokens=100))
    assert usage["total_tokens"] == 100


def test_cache_tokens_are_surfaced_separately_from_the_total():
    usage = _usage_payload(_resp(prompt_tokens=10, completion_tokens=5,
                                 cache_read_tokens=900,
                                 cache_creation_tokens=100))
    assert usage["total_tokens"] == 15, "cache tokens double-count the prompt"
    assert usage["cache_read_tokens"] == 900
    assert usage["cache_creation_tokens"] == 100


def test_retry_count_is_surfaced_so_spend_reconciles_against_calls():
    """Every retry attempt was billed; the caller needs the attempt count."""
    usage = _usage_payload(_resp(prompt_tokens=200, completion_tokens=4,
                                 retries=2))
    assert usage["retries"] == 2


def test_usage_headers_carry_only_reported_fields():
    headers = _usage_headers(_usage_payload(
        _resp(prompt_tokens=340, completion_tokens=12)))
    assert headers[HDR_PROMPT_TOKENS] == "340"
    assert headers[HDR_COMPLETION_TOKENS] == "12"
    assert headers[HDR_TOTAL_TOKENS] == "352"


def test_usage_headers_are_empty_when_the_provider_reported_nothing():
    """No fabricated zeros on the wire."""
    assert _usage_headers(_usage_payload(_resp())) == {
        "X-LLM-Provider": "anthropic", "X-LLM-Model": "claude-sonnet-5"}


@pytest.mark.parametrize("provider,prompt,completion", [
    ("openai", 11, 3),        # usage.prompt_tokens / completion_tokens
    ("anthropic", 11, 3),     # usage.input_tokens / output_tokens
    ("ollama", 11, 3),        # prompt_eval_count / eval_count
])
def test_all_provider_shapes_arrive_normalised(provider, prompt, completion):
    """The providers layer already normalises; the boundary must not re-mangle it.

    Pins that whatever provider produced the response, this router reads the two
    canonical fields and nothing provider-specific leaks into the contract.
    """
    usage = _usage_payload(_resp(provider=provider, prompt_tokens=prompt,
                                 completion_tokens=completion))
    assert (usage["prompt_tokens"], usage["completion_tokens"]) == (prompt, completion)
    assert usage["provider"] == provider


def test_a_long_model_name_cannot_bloat_a_response_header():
    headers = _usage_headers(_usage_payload(_resp(model="m" * 500,
                                                  prompt_tokens=1)))
    assert len(headers["X-LLM-Model"]) <= 64
