"""BackboneClient — verifies JWT minting, header propagation, and
HTTP error mapping using httpx's MockTransport.

No network IO; no real Backbone process required.
"""

from __future__ import annotations

import httpx
import jwt
import pytest

from app.backbone_client import BackboneClient, BackboneClientError


JWT_SECRET = "test-secret-do-not-use-in-production"


def _expect_token(request: httpx.Request, tenant_id: str) -> None:
    auth = request.headers.get("authorization", "")
    assert auth.startswith("Bearer "), "missing bearer token"
    token = auth[len("Bearer "):]
    payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    assert payload["tenant_id"] == tenant_id
    assert payload["role"] == "api"
    assert payload["type"] == "access"


@pytest.mark.asyncio
async def test_store_transcript_segment_posts_and_returns_node_id() -> None:
    seen_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        assert request.url.path == "/api/v1/backbone/nodes"
        _expect_token(request, "tenant-1")
        body = request.read()
        assert b"\"node_type\":\"TranscriptSegment\"" in body
        return httpx.Response(
            200,
            json={
                "success": True,
                "node_id": "node-xyz",
                "node_type": "TranscriptSegment",
                "embedded": True,
            },
        )

    client = BackboneClient(
        base_url="http://backbone.test",
        jwt_secret=JWT_SECRET,
        service_user_id="service:fusion",
    )
    client._client = httpx.AsyncClient(  # noqa: SLF001
        transport=httpx.MockTransport(handler),
        base_url="http://backbone.test",
    )
    node_id = await client.store_transcript_segment(
        tenant_id="tenant-1",
        trace_id="trace-1",
        session_id="sess-1",
        text="The applicant indicated tobacco use in the last 18 months.",
        properties={"ordinal": 0, "start_ms": 0, "end_ms": 1500},
    )
    assert node_id == "node-xyz"
    assert len(seen_requests) == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_token_is_cached_per_tenant() -> None:
    seen_tokens: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_tokens.append(request.headers["authorization"])
        return httpx.Response(
            200, json={"success": True, "node_id": "n", "embedded": True}
        )

    client = BackboneClient(
        base_url="http://backbone.test",
        jwt_secret=JWT_SECRET,
        service_user_id="service:fusion",
        token_ttl_seconds=3600,
    )
    client._client = httpx.AsyncClient(  # noqa: SLF001
        transport=httpx.MockTransport(handler),
        base_url="http://backbone.test",
    )

    for _ in range(3):
        await client.store_transcript_segment(
            tenant_id="tenant-1",
            trace_id="trace",
            session_id="sess",
            text="x",
            properties={},
        )
    # Same tenant → same token across calls (cache hit).
    assert len(set(seen_tokens)) == 1

    await client.store_transcript_segment(
        tenant_id="tenant-2",
        trace_id="trace",
        session_id="sess",
        text="x",
        properties={},
    )
    # Different tenant → different token (cache miss for new key).
    assert len(set(seen_tokens)) == 2
    await client.aclose()


@pytest.mark.asyncio
async def test_http_error_raises_backbone_client_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal error")

    client = BackboneClient(
        base_url="http://backbone.test",
        jwt_secret=JWT_SECRET,
        service_user_id="service:fusion",
    )
    client._client = httpx.AsyncClient(  # noqa: SLF001
        transport=httpx.MockTransport(handler),
        base_url="http://backbone.test",
    )
    with pytest.raises(BackboneClientError) as exc:
        await client.store_transcript_segment(
            tenant_id="tenant-1",
            trace_id="trace",
            session_id="sess",
            text="x",
            properties={},
        )
    assert "500" in str(exc.value)
    await client.aclose()


@pytest.mark.asyncio
async def test_missing_node_id_raises() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True})  # no node_id

    client = BackboneClient(
        base_url="http://backbone.test",
        jwt_secret=JWT_SECRET,
        service_user_id="service:fusion",
    )
    client._client = httpx.AsyncClient(  # noqa: SLF001
        transport=httpx.MockTransport(handler),
        base_url="http://backbone.test",
    )
    with pytest.raises(BackboneClientError) as exc:
        await client.store_transcript_segment(
            tenant_id="tenant-1",
            trace_id="trace",
            session_id="sess",
            text="x",
            properties={},
        )
    assert "node_id" in str(exc.value)
    await client.aclose()


@pytest.mark.asyncio
async def test_create_relation_posts_relations_endpoint() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.url.path == "/api/v1/backbone/relations"
        return httpx.Response(
            200, json={"success": True, "relation_type": "EXTRACTED_FROM"}
        )

    client = BackboneClient(
        base_url="http://backbone.test",
        jwt_secret=JWT_SECRET,
        service_user_id="service:fusion",
    )
    client._client = httpx.AsyncClient(  # noqa: SLF001
        transport=httpx.MockTransport(handler),
        base_url="http://backbone.test",
    )
    await client.create_relation(
        tenant_id="tenant-1",
        trace_id="trace",
        from_node_id="seg-1",
        to_node_id="sess-1",
        relation_type="EXTRACTED_FROM",
    )
    assert len(seen) == 1
    await client.aclose()
