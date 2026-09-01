"""Webhook plugin: signature, parser, composer, dispatcher."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx
import pytest

from app.matcher import MatchCandidate, MatchResult
from app.surfaces import SurfaceError, SurfaceUnavailable
from app.webhook.composer import WebhookComposer
from app.webhook.dispatcher import WebhookDispatcher, WebhookDispatchError
from app.webhook.installation import (
    WebhookInstallation,
    WebhookOutbound,
)
from app.webhook.parser import (
    WebhookInboundError,
    parse_webhook_inbound,
)
from app.webhook.signature import (
    WebhookSignatureError,
    sign_webhook_body,
    verify_webhook_signature,
)


SECRET = "test-shared-secret"


def _candidate(similarity: float = 0.91) -> MatchCandidate:
    return MatchCandidate(
        node_id="n1",
        node_type="TranscriptSegment",
        similarity=similarity,
        text="California cigar lookback is 24 months.",
        speaker_id="priya",
        speaker_role="underwriting",
        session_id="sess-1",
        artifact_id="art-1",
        start_ms=1000,
        end_ms=5000,
        ordinal=3,
        product_ids=("lt5",),
        raw={},
    )


def _match(sim: float = 0.91) -> MatchResult:
    if sim <= 0:
        return MatchResult(candidates=[], top_similarity=0.0, confidence_band="none")
    band = "high" if sim >= 0.85 else "medium" if sim >= 0.65 else "low"
    return MatchResult(
        candidates=[_candidate(sim)], top_similarity=sim, confidence_band=band  # type: ignore[arg-type]
    )


# ── Signature ───────────────────────────────────────────────────


def test_sign_then_verify_round_trip() -> None:
    body = b'{"v":1,"question":"hi"}'
    header, _ = sign_webhook_body(secret=SECRET, body=body)
    verify_webhook_signature(
        secret=SECRET, header_value=header, body=body
    )


def test_tampered_body_rejected() -> None:
    body = b'{"v":1,"question":"hi"}'
    header, _ = sign_webhook_body(secret=SECRET, body=body)
    with pytest.raises(WebhookSignatureError):
        verify_webhook_signature(
            secret=SECRET, header_value=header, body=body + b"x"
        )


def test_replay_outside_window_rejected() -> None:
    body = b"{}"
    old_ts = int(time.time()) - 600
    header, _ = sign_webhook_body(secret=SECRET, body=body, timestamp=old_ts)
    with pytest.raises(WebhookSignatureError):
        verify_webhook_signature(
            secret=SECRET, header_value=header, body=body, max_age_seconds=300
        )


def test_wrong_secret_rejected() -> None:
    body = b'{"v":1}'
    header, _ = sign_webhook_body(secret=SECRET, body=body)
    with pytest.raises(WebhookSignatureError):
        verify_webhook_signature(
            secret="other-secret", header_value=header, body=body
        )


def test_missing_header_rejected() -> None:
    with pytest.raises(WebhookSignatureError):
        verify_webhook_signature(
            secret=SECRET, header_value=None, body=b"{}"
        )


def test_malformed_header_rejected() -> None:
    with pytest.raises(WebhookSignatureError):
        verify_webhook_signature(
            secret=SECRET, header_value="malformed", body=b"{}"
        )


# ── Parser ──────────────────────────────────────────────────────


def test_valid_payload_parsed() -> None:
    body = {
        "v": 1,
        "tenant_id": "t1",
        "trigger": {"user_id": "u1", "channel_id": "c1"},
        "question": "What is the LT5 tobacco lookback?",
    }
    parsed = parse_webhook_inbound(body)
    assert parsed.tenant_id == "t1"
    assert parsed.question.startswith("What")
    assert parsed.trigger.user_id == "u1"


def test_question_strip_whitespace() -> None:
    parsed = parse_webhook_inbound({"v": 1, "question": "  hi?  "})
    assert parsed.question == "hi?"


def test_whitespace_only_question_rejected() -> None:
    with pytest.raises(WebhookInboundError):
        parse_webhook_inbound({"v": 1, "question": "   "})


def test_unknown_top_level_field_rejected() -> None:
    with pytest.raises(WebhookInboundError):
        parse_webhook_inbound({"v": 1, "question": "q", "extra": True})


def test_invalid_json_bytes_rejected() -> None:
    with pytest.raises(WebhookInboundError):
        parse_webhook_inbound(b"not-json")


# ── Composer ────────────────────────────────────────────────────


def test_composer_returns_none_on_empty_match() -> None:
    composer = WebhookComposer()
    assert composer.compose(
        dispatch_id="d1", question_text="q", match=_match(0.0)
    ) is None


def test_composer_emits_versioned_envelope() -> None:
    composer = WebhookComposer(schema_version=1)
    payload = composer.compose(
        dispatch_id="d1", question_text="q", match=_match(0.91)
    )
    assert payload is not None
    assert payload.surface == "webhook"
    body = payload.payload
    assert body["v"] == 1
    assert body["type"] == "knowledge_echo"
    assert body["match"]["similarity_pct"] == 91
    assert body["match"]["primary"]["speaker_id"] == "priya"
    # Hash is stable across calls.
    again = composer.compose(
        dispatch_id="d1", question_text="q", match=_match(0.91)
    )
    assert again is not None
    assert again.payload_hash == payload.payload_hash


# ── Dispatcher ──────────────────────────────────────────────────


@dataclass
class _FakeInstalls:
    install: Optional[WebhookInstallation]
    error: Optional[Exception] = None

    async def for_tenant(self, tenant_id: str):  # noqa: ARG002
        if self.error:
            raise self.error
        assert self.install is not None
        return self.install


def _install(*, with_outbound: bool = True) -> WebhookInstallation:
    out: Optional[WebhookOutbound] = None
    if with_outbound:
        out = WebhookOutbound(
            destination_url="https://customer.example.com/echoes",
            outbound_secret="outbound-secret",
            extra_headers={"X-Customer-Auth": "abc"},
        )
    return WebhookInstallation(
        tenant_id="t1",
        installation_id="inst-1",
        inbound_secret=SECRET,
        outbound=out,
        status="connected",
    )


@pytest.mark.asyncio
async def test_dispatcher_posts_signed_body() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.read()
        captured["sig"] = request.headers.get("X-Nexus-Signature")
        captured["dispatch_id"] = request.headers.get("X-Nexus-Dispatch-Id")
        captured["custom_header"] = request.headers.get("X-Customer-Auth")
        return httpx.Response(200, json={"received": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    installs = _FakeInstalls(install=_install())
    dispatcher = WebhookDispatcher(installs, client=client)
    composer = WebhookComposer()
    payload = composer.compose(
        dispatch_id="d1", question_text="What?", match=_match(0.91)
    )
    assert payload is not None

    outcome = await dispatcher.dispatch(
        tenant_id="t1",
        payload=payload,
        as_dm=False,
        is_live=True,
        user_id_ext="u1",
        channel_id_ext="c1",
        thread_ts=None,
    )
    assert outcome.decision == "posted_channel"
    assert captured["url"].endswith("/echoes")
    assert captured["custom_header"] == "abc"
    # Verify the signature with the same secret round-trips.
    verify_webhook_signature(
        secret="outbound-secret",
        header_value=captured["sig"],
        body=captured["body"],
    )


@pytest.mark.asyncio
async def test_dispatcher_unavailable_when_no_outbound() -> None:
    installs = _FakeInstalls(install=_install(with_outbound=False))
    dispatcher = WebhookDispatcher(installs)
    composer = WebhookComposer()
    payload = composer.compose(
        dispatch_id="d1", question_text="q", match=_match(0.91)
    )
    assert payload is not None
    with pytest.raises(SurfaceUnavailable):
        await dispatcher.dispatch(
            tenant_id="t1",
            payload=payload,
            as_dm=False,
            is_live=True,
            user_id_ext=None,
            channel_id_ext="c1",
            thread_ts=None,
        )


@pytest.mark.asyncio
async def test_dispatcher_5xx_retries_then_raises() -> None:
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(503, text="busy")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    dispatcher = WebhookDispatcher(
        _FakeInstalls(install=_install()),
        max_retries=2,
        client=client,
    )
    composer = WebhookComposer()
    payload = composer.compose(
        dispatch_id="d1", question_text="q", match=_match(0.91)
    )
    assert payload is not None
    with pytest.raises(WebhookDispatchError):
        await dispatcher.dispatch(
            tenant_id="t1",
            payload=payload,
            as_dm=False,
            is_live=True,
            user_id_ext=None,
            channel_id_ext="c1",
            thread_ts=None,
        )
    assert call_count["n"] == 3  # initial + 2 retries


@pytest.mark.asyncio
async def test_dispatcher_4xx_is_terminal_no_retry() -> None:
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(400, text="bad payload")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    dispatcher = WebhookDispatcher(
        _FakeInstalls(install=_install()),
        max_retries=3,
        client=client,
    )
    composer = WebhookComposer()
    payload = composer.compose(
        dispatch_id="d1", question_text="q", match=_match(0.91)
    )
    assert payload is not None
    with pytest.raises(WebhookDispatchError):
        await dispatcher.dispatch(
            tenant_id="t1",
            payload=payload,
            as_dm=False,
            is_live=True,
            user_id_ext=None,
            channel_id_ext="c1",
            thread_ts=None,
        )
    assert call_count["n"] == 1
