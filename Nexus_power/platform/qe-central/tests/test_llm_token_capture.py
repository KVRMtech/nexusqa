"""M0.6 / T-OB-03 — token usage must survive the HTTP boundary and every path.

The defect this milestone fixes was silent: platform-api's ``/api/v1/llm/*``
COMPUTED provider token usage and then dropped it, so nothing downstream could
attribute spend to a crawl.  These tests hold the repaired contract at the two
seams that matter — the router's response shape, and this client's reading of it
on success AND on a provider error that still billed us.
"""
from __future__ import annotations

import httpx
import pytest

from app.clients import platform_api
from app.clients.platform_api import LLMUsage, _parse_usage
from app.observability import metrics as qec_metrics


def _response(status: int, *, json_body=None, headers=None) -> httpx.Response:
    return httpx.Response(
        status_code=status, json=json_body if json_body is not None else {},
        headers=headers or {},
        request=httpx.Request("POST", "http://platform-api/api/v1/llm/complete"),
    )


# ══ Reading usage off the wire ════════════════════════════════════════════


def test_usage_is_read_from_a_200_body():
    usage = _parse_usage(_response(200, json_body={
        "text": "2",
        "usage": {"prompt_tokens": 340, "completion_tokens": 12,
                  "total_tokens": 352, "provider": "anthropic",
                  "model": "claude-sonnet-5", "retries": 0, "latency_ms": 812},
    }), {"text": "2", "usage": {
        "prompt_tokens": 340, "completion_tokens": 12, "total_tokens": 352,
        "provider": "anthropic", "model": "claude-sonnet-5", "retries": 0,
        "latency_ms": 812}})
    assert usage.prompt_tokens == 340
    assert usage.completion_tokens == 12
    assert usage.total_tokens == 352
    assert usage.provider == "anthropic"
    assert usage.reported is True


def test_usage_survives_a_502_via_response_headers():
    """A provider that billed us and THEN failed must stay in the account.

    The 502 body is the existing ``detail`` string contract, so usage rides on
    headers — the only channel that survives both outcomes without breaking
    callers that already parse ``detail``.
    """
    usage = _parse_usage(_response(502, json_body={"detail": "LLM error"}, headers={
        "X-LLM-Prompt-Tokens": "500", "X-LLM-Completion-Tokens": "0",
        "X-LLM-Total-Tokens": "500", "X-LLM-Provider": "openai",
        "X-LLM-Model": "gpt-4o",
    }), {"detail": "LLM error"})
    assert usage.prompt_tokens == 500
    assert usage.total_tokens == 500
    assert usage.provider == "openai"
    assert usage.reported is True


def test_no_reported_usage_stays_none_and_is_never_guessed():
    """``None`` (not reported) and ``0`` (reported zero) are different facts.

    Flattening them would let a silent regression in provider reporting read as
    a stream of free calls.
    """
    usage = _parse_usage(_response(200, json_body={"text": "hi"}), {"text": "hi"})
    assert usage.prompt_tokens is None
    assert usage.completion_tokens is None
    assert usage.reported is False


def test_total_is_derived_when_the_boundary_did_not_send_one():
    usage = LLMUsage(prompt_tokens=10, completion_tokens=4)
    assert usage.as_dict()["total_tokens"] == 14


def test_unparseable_header_values_do_not_become_fabricated_counts():
    usage = _parse_usage(_response(502, headers={
        "X-LLM-Prompt-Tokens": "not-a-number"}), None)
    assert usage.prompt_tokens is None
    assert usage.reported is False


# ══ The client contract, end to end over a stubbed transport ══════════════


@pytest.mark.asyncio
async def test_complete_llm_returns_usage_on_success(monkeypatch):
    async def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "text": "2", "provider": "anthropic", "model": "claude-sonnet-5",
            "usage": {"prompt_tokens": 210, "completion_tokens": 3,
                      "total_tokens": 213, "provider": "anthropic",
                      "model": "claude-sonnet-5"},
        })

    _patch_transport(monkeypatch, _handler)
    result = await platform_api.complete_llm(tenant_id="t1", prompt="which?")
    assert result.ok is True
    assert result.usage.prompt_tokens == 210
    assert result.usage.completion_tokens == 3
    assert result.usage.total_tokens == 213


@pytest.mark.asyncio
async def test_complete_llm_retains_usage_on_a_provider_error(monkeypatch):
    async def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, json={"detail": "provider overloaded"},
                              headers={"X-LLM-Prompt-Tokens": "410",
                                       "X-LLM-Completion-Tokens": "0",
                                       "X-LLM-Provider": "anthropic"})

    _patch_transport(monkeypatch, _handler)
    result = await platform_api.complete_llm(tenant_id="t1", prompt="which?")
    assert result.ok is False
    assert result.usage.prompt_tokens == 410, "a failed call is not a free call"


@pytest.mark.asyncio
async def test_a_transport_failure_reports_no_usage_rather_than_zero(monkeypatch):
    async def _handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to platform-api")

    _patch_transport(monkeypatch, _handler)
    result = await platform_api.complete_llm(tenant_id="t1", prompt="which?")
    assert result.ok is False
    assert result.usage.reported is False


@pytest.mark.asyncio
async def test_usage_reaches_the_prometheus_registry(monkeypatch):
    qec_metrics.reset_for_tests() if hasattr(qec_metrics, "reset_for_tests") else None

    async def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "text": "1",
            "usage": {"prompt_tokens": 100, "completion_tokens": 20,
                      "provider": "anthropic", "model": "claude-sonnet-5"},
        })

    _patch_transport(monkeypatch, _handler)
    before = _token_total()
    await platform_api.complete_llm(tenant_id="t1", prompt="which?")
    assert _token_total() - before == 120


def _token_total() -> float:
    """Sum qec_llm_tokens_total across prompt+completion samples."""
    from prometheus_client import generate_latest
    from prometheus_client.parser import text_string_to_metric_families

    registry = qec_metrics.get_metrics_registry()
    if registry is None:
        return 0.0
    total = 0.0
    for family in text_string_to_metric_families(
            generate_latest(registry).decode("utf-8")):
        if family.name != "qec_llm_tokens":
            continue
        for sample in family.samples:
            if sample.name == "qec_llm_tokens_total" and \
                    sample.labels.get("kind") in ("prompt", "completion"):
                total += sample.value
    return total


def _patch_transport(monkeypatch, handler) -> None:
    """Route the client's httpx.AsyncClient through a MockTransport.

    Patches the constructor rather than the module function so the REAL client
    code path — including its status branching and usage parsing — is exercised.
    """
    real_client = httpx.AsyncClient

    def _factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(platform_api.httpx, "AsyncClient", _factory)
    monkeypatch.setattr(platform_api, "mint_service_jwt", lambda tenant_id: "tok")


# ══ Cardinality at the qe-central boundary ════════════════════════════════


def test_qec_llm_labels_are_cardinality_capped():
    """A per-request model id must cost one extra series, not one per request."""
    for i in range(qec_metrics.MAX_LLM_LABEL_VALUES + 20):
        qec_metrics.record_llm_call(endpoint="complete", provider="openai",
                                    model=f"gpt-{i}", outcome="success",
                                    prompt_tokens=1)
    from prometheus_client import generate_latest
    from prometheus_client.parser import text_string_to_metric_families

    registry = qec_metrics.get_metrics_registry()
    models = set()
    for family in text_string_to_metric_families(
            generate_latest(registry).decode("utf-8")):
        for sample in family.samples:
            if "model" in sample.labels:
                models.add(sample.labels["model"])
    assert "other" in models


def test_qec_llm_metrics_carry_no_tenant_or_crawl_label():
    qec_metrics.record_llm_call(endpoint="complete", provider="anthropic",
                                model="claude", outcome="success",
                                prompt_tokens=5, completion_tokens=1)
    from prometheus_client import generate_latest
    from prometheus_client.parser import text_string_to_metric_families

    registry = qec_metrics.get_metrics_registry()
    for family in text_string_to_metric_families(
            generate_latest(registry).decode("utf-8")):
        if not family.name.startswith("qec_llm"):
            continue
        for sample in family.samples:
            assert "tenant_id" not in sample.labels
            assert "crawl_id" not in sample.labels
