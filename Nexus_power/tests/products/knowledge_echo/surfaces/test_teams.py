"""Teams plugin: activity parser, Adaptive Card composer, JWT verifier."""

from __future__ import annotations

import time
from typing import Any, Optional

import pytest

from app.matcher import MatchCandidate, MatchResult
from app.teams.activity import (
    TeamsActivityKind,
    parse_teams_activity,
)
from app.teams.auth import (
    TeamsAuthError,
    TeamsMetadataLoader,
    TeamsTokenVerifier,
)
from app.teams.composer import TeamsComposer


def _candidate(similarity: float = 0.95) -> MatchCandidate:
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


def _match(sim: float = 0.95) -> MatchResult:
    band = "high" if sim >= 0.85 else "medium"
    return MatchResult(
        candidates=[_candidate(sim)], top_similarity=sim, confidence_band=band  # type: ignore[arg-type]
    )


# ── Activity parser ────────────────────────────────────────────


def test_personal_message_recognised() -> None:
    payload = {
        "type": "message",
        "id": "act-1",
        "serviceUrl": "https://smba.example/",
        "conversation": {
            "id": "conv-1",
            "tenantId": "aad-001",
            "conversationType": "personal",
        },
        "from": {"id": "29:user-1", "aadObjectId": "user-aad-1", "name": "Jordan"},
        "recipient": {"id": "28:bot-1", "name": "Nexus"},
        "text": "<at>Nexus</at> what is the CA tobacco lookback?",
        "channelData": {"tenant": {"id": "aad-001"}},
    }
    parsed = parse_teams_activity(payload)
    assert parsed.kind == TeamsActivityKind.MESSAGE_PERSONAL
    assert parsed.aad_tenant_id == "aad-001"
    assert parsed.user_id_ext == "user-aad-1"
    # Mention stripped
    assert "Nexus" not in parsed.text
    assert "tobacco" in parsed.text


def test_channel_message_recognised() -> None:
    payload = {
        "type": "message",
        "id": "act-2",
        "serviceUrl": "https://smba.example/",
        "conversation": {
            "id": "conv-2",
            "tenantId": "aad-002",
            "conversationType": "channel",
        },
        "from": {"id": "29:user-2", "name": "Pat"},
        "recipient": {"id": "28:bot-2"},
        "channelData": {
            "tenant": {"id": "aad-002"},
            "teamsChannelId": "19:channel.abc",
        },
        "text": "question about LT5?",
    }
    parsed = parse_teams_activity(payload)
    assert parsed.kind == TeamsActivityKind.MESSAGE_CHANNEL
    assert parsed.channel_id_ext == "19:channel.abc"


def test_group_message_recognised() -> None:
    payload = {
        "type": "message",
        "conversation": {"id": "g", "conversationType": "groupChat"},
        "from": {"id": "u"},
        "recipient": {"id": "b"},
        "text": "?",
    }
    parsed = parse_teams_activity(payload)
    assert parsed.kind == TeamsActivityKind.MESSAGE_GROUP


def test_bot_self_messages_ignored() -> None:
    payload = {
        "type": "message",
        "conversation": {"id": "c", "conversationType": "personal"},
        "from": {"id": "28:bot-1", "role": "bot"},
        "recipient": {"id": "28:bot-1"},
        "text": "loop?",
    }
    parsed = parse_teams_activity(payload)
    assert parsed.kind == TeamsActivityKind.IGNORED


def test_unknown_activity_type_ignored() -> None:
    parsed = parse_teams_activity({"type": "typing"})
    assert parsed.kind == TeamsActivityKind.IGNORED


def test_non_dict_payload_ignored() -> None:
    parsed = parse_teams_activity([])  # type: ignore[arg-type]
    assert parsed.kind == TeamsActivityKind.IGNORED


# ── Composer ───────────────────────────────────────────────────


def test_composer_returns_none_when_empty_match() -> None:
    composer = TeamsComposer()
    empty = MatchResult(candidates=[], top_similarity=0.0, confidence_band="none")
    assert composer.compose(dispatch_id="d1", question_text="q", match=empty) is None


def test_composer_emits_adaptive_card_attachment() -> None:
    composer = TeamsComposer()
    out = composer.compose(
        dispatch_id="d1",
        question_text="What is the CA tobacco lookback?",
        match=_match(0.92),
    )
    assert out is not None
    payload = out.payload
    assert payload["type"] == "message"
    attachments = payload["attachments"]
    assert isinstance(attachments, list) and attachments
    card = attachments[0]
    assert card["contentType"] == "application/vnd.microsoft.card.adaptive"
    body = card["content"]["body"]
    # Header / quote / context / question / asked context
    assert body[0]["text"].startswith("KNOWLEDGE ECHO")
    assert "92% MATCH" in body[0]["text"]
    # Actions present and carry dispatch_id
    actions = card["content"]["actions"]
    assert len(actions) == 3
    for a in actions:
        assert a["data"]["dispatch_id"] == "d1"
        # Teams expects ``msteams.type`` for messageBack actions.
        assert a["data"]["msteams"]["type"] == "messageBack"


def test_composer_payload_hash_stable_and_sensitive() -> None:
    composer = TeamsComposer()
    a = composer.compose(dispatch_id="d1", question_text="q", match=_match(0.9))
    b = composer.compose(dispatch_id="d1", question_text="q", match=_match(0.9))
    c = composer.compose(dispatch_id="d2", question_text="q", match=_match(0.9))
    assert a is not None and b is not None and c is not None
    assert a.payload_hash == b.payload_hash
    assert a.payload_hash != c.payload_hash


# ── JWT verifier ───────────────────────────────────────────────


# Use the cryptography library to mint a tiny RSA key and a JWT signed
# with it; build a tiny in-memory ``cert_loader`` for the verifier.


def _build_rsa_jwk_and_signing_key():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from jwt.algorithms import RSAAlgorithm
    import json

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub_pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    jwk = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
    jwk["kid"] = "test-kid"
    return jwk, key, pub_pem


def _make_jwt(*, signing_key, kid: str, claims: dict[str, Any]) -> str:
    import jwt as pyjwt

    return pyjwt.encode(
        claims,
        signing_key,
        algorithm="RS256",
        headers={"kid": kid, "alg": "RS256"},
    )


def _metadata_loader(jwk: dict[str, Any]):
    async def _loader(url: str) -> dict[str, Any]:
        if url.endswith("openidconfiguration"):
            return {
                "jwks_uri": "https://login.botframework.com/keys",
                "issuer": "https://api.botframework.com",
            }
        if url.endswith("/keys"):
            return {"keys": [jwk]}
        raise AssertionError(f"unexpected loader url: {url}")

    return _loader


@pytest.mark.asyncio
async def test_jwt_verifier_accepts_valid_token() -> None:
    jwk, key, _ = _build_rsa_jwk_and_signing_key()
    metadata = TeamsMetadataLoader(loader=_metadata_loader(jwk))
    verifier = TeamsTokenVerifier(metadata)

    now = int(time.time())
    token = _make_jwt(
        signing_key=key,
        kid="test-kid",
        claims={
            "iss": "https://api.botframework.com",
            "aud": "ms-app-id-1",
            "iat": now,
            "exp": now + 600,
            "serviceurl": "https://smba.example/",
        },
    )
    claims = await verifier.verify(
        authorization_header=f"Bearer {token}",
        expected_audience="ms-app-id-1",
        expected_service_url="https://smba.example",
    )
    assert claims["iss"] == "https://api.botframework.com"


@pytest.mark.asyncio
async def test_jwt_verifier_rejects_wrong_audience() -> None:
    jwk, key, _ = _build_rsa_jwk_and_signing_key()
    metadata = TeamsMetadataLoader(loader=_metadata_loader(jwk))
    verifier = TeamsTokenVerifier(metadata)
    now = int(time.time())
    token = _make_jwt(
        signing_key=key, kid="test-kid",
        claims={
            "iss": "https://api.botframework.com",
            "aud": "ms-app-id-1",
            "iat": now,
            "exp": now + 600,
        },
    )
    with pytest.raises(TeamsAuthError):
        await verifier.verify(
            authorization_header=f"Bearer {token}",
            expected_audience="other-app-id",
        )


@pytest.mark.asyncio
async def test_jwt_verifier_rejects_expired_token() -> None:
    jwk, key, _ = _build_rsa_jwk_and_signing_key()
    metadata = TeamsMetadataLoader(loader=_metadata_loader(jwk))
    verifier = TeamsTokenVerifier(metadata, leeway_seconds=0)
    now = int(time.time())
    token = _make_jwt(
        signing_key=key, kid="test-kid",
        claims={
            "iss": "https://api.botframework.com",
            "aud": "ms-app-id-1",
            "iat": now - 7200,
            "exp": now - 60,
        },
    )
    with pytest.raises(TeamsAuthError):
        await verifier.verify(
            authorization_header=f"Bearer {token}",
            expected_audience="ms-app-id-1",
        )


@pytest.mark.asyncio
async def test_jwt_verifier_rejects_untrusted_issuer() -> None:
    jwk, key, _ = _build_rsa_jwk_and_signing_key()
    metadata = TeamsMetadataLoader(loader=_metadata_loader(jwk))
    verifier = TeamsTokenVerifier(metadata)
    now = int(time.time())
    token = _make_jwt(
        signing_key=key, kid="test-kid",
        claims={
            "iss": "https://evil.example",
            "aud": "ms-app-id-1",
            "iat": now,
            "exp": now + 600,
        },
    )
    with pytest.raises(TeamsAuthError):
        await verifier.verify(
            authorization_header=f"Bearer {token}",
            expected_audience="ms-app-id-1",
        )


@pytest.mark.asyncio
async def test_jwt_verifier_rejects_service_url_mismatch() -> None:
    jwk, key, _ = _build_rsa_jwk_and_signing_key()
    metadata = TeamsMetadataLoader(loader=_metadata_loader(jwk))
    verifier = TeamsTokenVerifier(metadata)
    now = int(time.time())
    token = _make_jwt(
        signing_key=key, kid="test-kid",
        claims={
            "iss": "https://api.botframework.com",
            "aud": "ms-app-id-1",
            "iat": now,
            "exp": now + 600,
            "serviceurl": "https://smba.real/",
        },
    )
    with pytest.raises(TeamsAuthError):
        await verifier.verify(
            authorization_header=f"Bearer {token}",
            expected_audience="ms-app-id-1",
            expected_service_url="https://smba.fake/",
        )


@pytest.mark.asyncio
async def test_jwt_verifier_rejects_missing_bearer() -> None:
    jwk, _, _ = _build_rsa_jwk_and_signing_key()
    metadata = TeamsMetadataLoader(loader=_metadata_loader(jwk))
    verifier = TeamsTokenVerifier(metadata)
    with pytest.raises(TeamsAuthError):
        await verifier.verify(
            authorization_header=None,
            expected_audience="ms-app-id-1",
        )
    with pytest.raises(TeamsAuthError):
        await verifier.verify(
            authorization_header="Token abc",
            expected_audience="ms-app-id-1",
        )
