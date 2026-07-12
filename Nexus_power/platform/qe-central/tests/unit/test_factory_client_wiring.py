"""QE-Central Phase 5.5 — factory-client WIRING tests (no network, no DB).

Locks the Wire-stage integration of the resilience + observability modules into
the two VKPower factory-client surfaces — ``app.clients.factory`` and the cycle
driver's ``HttpCycleClient`` — which previously had NO behavioural coverage:

  * an IDEMPOTENT read (``get_rtm`` / ``poll_run`` / ``get_run``) RETRIES a
    transient failure (transport error / 502-503-504) then succeeds;
  * a DETERMINISTIC status (404) is NOT retried (raised / honest-None on the
    first attempt — retrying only amplifies a real error);
  * a NON-idempotent trigger (``generate`` POST / ``run_playwright`` POST) is
    executed EXACTLY ONCE and NEVER auto-retried (the double-submit guard);
  * the correlation id bound in the current context is PROPAGATED to VKPower on
    the outbound request header; and
  * the factory-latency metric is recorded (when prometheus is available).

All transport is an in-process ``httpx.MockTransport`` and the retry backoff is
pinned to zero delay, so the tests are instant, exact, and hermetic.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

import app.clients.factory as factory
import app.observability as obs
from app.controlplane.cycle.driver import HttpCycleClient
from app.observability.middleware import _REQUEST_ID_CTX


def run(coro):
    return asyncio.run(coro)


def _mock_async_client(handler):
    """Return an ``httpx.AsyncClient`` factory bound to a scripted MockTransport.

    ``handler(request) -> httpx.Response`` (or ``raise httpx.ConnectError`` to
    simulate a transport failure).  Injected in place of ``httpx.AsyncClient`` so
    the client code runs UNCHANGED against a deterministic in-process transport.
    """
    real = httpx.AsyncClient

    def _factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    return _factory


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch):
    # Pin the retry backoff to zero so the retrying tests never actually sleep.
    monkeypatch.setenv("QEC_HTTP_RETRY_BASE_DELAY_S", "0")
    monkeypatch.setenv("QEC_HTTP_RETRY_MAX_DELAY_S", "0")
    monkeypatch.setenv("QEC_HTTP_MAX_RETRIES", "2")


# ══════════════════════ app.clients.factory (get_rtm / generate) ════════════

def test_get_rtm_retries_transient_then_succeeds(monkeypatch):
    """An idempotent /rtm read retries a transport blip and propagates the id."""
    calls = {"n": 0, "request_id": "unset"}

    def handler(request):
        calls["n"] += 1
        calls["request_id"] = request.headers.get("x-request-id")
        if calls["n"] == 1:
            raise httpx.ConnectError("first attempt drops")
        return httpx.Response(200, json={"artifact_id": "a1", "tests": []})

    monkeypatch.setattr(factory.httpx, "AsyncClient", _mock_async_client(handler))
    token = _REQUEST_ID_CTX.set("corr-rtm-1")  # bind a correlation id in-context
    try:
        out = run(factory.get_rtm(tenant_id="t1", artifact_id="a1"))
    finally:
        _REQUEST_ID_CTX.reset(token)

    assert out == {"artifact_id": "a1", "tests": []}
    assert calls["n"] == 2  # retried exactly once
    assert calls["request_id"] == "corr-rtm-1"  # correlation id propagated to VKPower


def test_get_rtm_does_not_retry_deterministic_404(monkeypatch):
    """A 404 is a deterministic error — raised on the FIRST attempt, no retry."""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(404, json={"detail": "unknown artifact"})

    monkeypatch.setattr(factory.httpx, "AsyncClient", _mock_async_client(handler))
    with pytest.raises(factory.FactoryClientError) as ei:
        run(factory.get_rtm(tenant_id="t1", artifact_id="a1"))

    assert ei.value.status_code == 404
    assert calls["n"] == 1  # NOT retried


def test_generate_post_is_never_retried(monkeypatch):
    """The non-idempotent generate trigger runs EXACTLY ONCE even on a 503."""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(503, json={"detail": "busy"})

    monkeypatch.setattr(factory.httpx, "AsyncClient", _mock_async_client(handler))
    with pytest.raises(factory.FactoryClientError) as ei:
        run(factory.generate(tenant_id="t1", artifact_id="a1"))

    assert ei.value.status_code == 503
    assert calls["n"] == 1  # a transient 503 does NOT amplify into a double-submit


def test_no_bound_id_adds_no_request_header(monkeypatch):
    """With no correlation id bound, no X-Request-ID is fabricated on the call."""
    seen = {"request_id": "unset"}

    def handler(request):
        seen["request_id"] = request.headers.get("x-request-id")
        return httpx.Response(200, json={"artifact_id": "a1", "tests": []})

    monkeypatch.setattr(factory.httpx, "AsyncClient", _mock_async_client(handler))
    # No _REQUEST_ID_CTX.set() → current_request_id() is "" → header omitted.
    run(factory.get_rtm(tenant_id="t1", artifact_id="a1"))
    assert seen["request_id"] is None


def test_factory_call_records_latency_metric(monkeypatch):
    """record_factory_call fires with the LOGICAL endpoint label (when enabled)."""
    if not obs.is_enabled():
        pytest.skip("prometheus_client unavailable / metrics disabled")
    registry = obs.get_metrics_registry()
    before = registry.get_sample_value(
        "qec_factory_calls_total",
        {"endpoint": "rtm", "method": "GET", "status": "200"},
    ) or 0.0

    def handler(request):
        return httpx.Response(200, json={"artifact_id": "a1", "tests": []})

    monkeypatch.setattr(factory.httpx, "AsyncClient", _mock_async_client(handler))
    run(factory.get_rtm(tenant_id="t1", artifact_id="a1"))

    after = registry.get_sample_value(
        "qec_factory_calls_total",
        {"endpoint": "rtm", "method": "GET", "status": "200"},
    ) or 0.0
    assert after == before + 1.0


# ══════════════════════ ANSWERS P1 — answer_key on generate ════════════════

def test_generate_without_answer_key_stays_bodyless(monkeypatch):
    """No answer_key → the POST carries NO body (byte-for-byte the historical call
    so every existing caller is unaffected)."""
    seen = {"body": "unset"}

    def handler(request):
        seen["body"] = request.content  # bytes
        return httpx.Response(200, json={"success": True})

    monkeypatch.setattr(factory.httpx, "AsyncClient", _mock_async_client(handler))
    run(factory.generate(tenant_id="t1", artifact_id="a1"))
    assert seen["body"] == b""  # body-less


def test_generate_with_answer_key_sends_json_body(monkeypatch):
    """A non-empty answer_key is sent as ``{"answer_key": {...}}`` JSON."""
    import json as _json
    seen = {"body": None, "ctype": None}

    def handler(request):
        seen["body"] = _json.loads(request.content)
        seen["ctype"] = request.headers.get("content-type")
        return httpx.Response(200, json={"success": True})

    monkeypatch.setattr(factory.httpx, "AsyncClient", _mock_async_client(handler))
    contract = {"outcomes": [{"field": "premium", "expected": 28.4, "match": "numeric"}], "rules": []}
    run(factory.generate(tenant_id="t1", artifact_id="a1", answer_key=contract))
    assert seen["body"] == {"answer_key": contract}
    assert "application/json" in (seen["ctype"] or "")


def test_httpcycleclient_generate_projects_raw_key_to_contract(monkeypatch):
    """HttpCycleClient projects the RAW answer_key → clean {outcomes,rules} on the
    wire (the factory never sees fill/exact/etc.)."""
    import json as _json
    seen = {"body": None}

    def handler(request):
        seen["body"] = _json.loads(request.content) if request.content else None
        return httpx.Response(200, json={"success": True})

    monkeypatch.setattr(httpx, "AsyncClient", _mock_async_client(handler))
    raw = {"fill": {"age": 35}, "outcomes": {"monthly_premium": "$28.40"}, "notes": "x"}
    run(HttpCycleClient().generate(tenant_id="t1", artifact_id="a1", answer_key=raw))
    # fill/notes dropped; outcomes normalized to a value-expectation record
    assert seen["body"]["answer_key"]["outcomes"] == [{
        "field": "monthly_premium", "when": {}, "expected": 28.40,
        "tolerance": None, "source_hint": "", "match": "numeric"}]
    assert "fill" not in seen["body"]["answer_key"]


def test_httpcycleclient_generate_empty_key_stays_bodyless(monkeypatch):
    """An answer_key with no outcomes/rules sends NO body (no empty-contract noise)."""
    seen = {"body": "unset"}

    def handler(request):
        seen["body"] = request.content
        return httpx.Response(200, json={"success": True})

    monkeypatch.setattr(httpx, "AsyncClient", _mock_async_client(handler))
    run(HttpCycleClient().generate(tenant_id="t1", artifact_id="a1", answer_key={"fill": {"age": 35}}))
    assert seen["body"] == b""


# ══════════════════════ driver.HttpCycleClient (poll / run / get_run) ═══════

def test_poll_run_retries_transient_get(monkeypatch):
    """poll_run is an idempotent GET — a transport blip is retried then succeeds."""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("blip")
        return httpx.Response(200, json={"run_id": "r1", "status": "running"})

    monkeypatch.setattr(httpx, "AsyncClient", _mock_async_client(handler))
    out = run(HttpCycleClient().poll_run(tenant_id="t1", artifact_id="a1", run_id="r1"))

    assert out.get("status") == "running"
    assert calls["n"] == 2  # retried exactly once


def test_run_trigger_post_not_retried(monkeypatch):
    """run_playwright is a NON-idempotent POST — one attempt, honest {} on failure."""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        raise httpx.ConnectError("blip")

    monkeypatch.setattr(httpx, "AsyncClient", _mock_async_client(handler))
    out = run(HttpCycleClient().run_playwright(
        tenant_id="t1", artifact_id="a1", test_ids=["x"], base_url="http://e",
    ))

    assert out == {}  # honest degradation, never a double-submit
    assert calls["n"] == 1


def test_get_run_deterministic_404_is_honest_none(monkeypatch):
    """get_run degrades a deterministic 404 to None WITHOUT retrying."""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(404, json={"detail": "no run"})

    monkeypatch.setattr(httpx, "AsyncClient", _mock_async_client(handler))
    out = run(HttpCycleClient().get_run(tenant_id="t1", artifact_id="a1", run_id="r1"))

    assert out is None
    assert calls["n"] == 1  # deterministic → no retry
