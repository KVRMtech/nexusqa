"""Async HTTP client for the Backbone engine.

The fusion engine creates one ``TranscriptSegment`` Backbone node per
chunk. Backbone owns embedding (BAAI/bge-large-en) and vector
persistence — we just hand it the text plus metadata.

Authentication: the fusion engine mints short-lived service JWTs and
attaches them as Bearer tokens. The service identity is configured in
``FusionConfig.service_account_*``.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx
import jwt

logger = logging.getLogger(__name__)


class BackboneClientError(Exception):
    """Wraps HTTP / transport / decode failures from Backbone."""


class BackboneClient:
    """Tenant-aware client for ``POST /api/v1/backbone/nodes``.

    Connection pool is reused; service tokens are minted with a TTL
    refresh window so we don't sign on every call.
    """

    def __init__(
        self,
        base_url: str,
        *,
        jwt_secret: str,
        jwt_algorithm: str = "HS256",
        service_user_id: str,
        service_role: str = "api",
        token_ttl_seconds: int = 3600,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._jwt_secret = jwt_secret
        self._jwt_algorithm = jwt_algorithm
        self._service_user_id = service_user_id
        self._service_role = service_role
        self._token_ttl = token_ttl_seconds
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout_seconds,
            http2=False,
        )
        self._token_cache: dict[str, tuple[str, datetime]] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    # ── Public API ──────────────────────────────────────────────

    async def store_transcript_segment(
        self,
        *,
        tenant_id: str,
        trace_id: str,
        session_id: str,
        text: str,
        properties: dict[str, Any],
        tags: Optional[list[str]] = None,
    ) -> str:
        """Create a TRANSCRIPT_SEGMENT node. Returns ``node_id``.

        ``properties`` may include speaker, ordinal, start/end ms,
        topic, etc.  Backbone embeds string properties for vector
        search; ensure ``text`` is included in ``properties`` so it
        contributes to the embedding.
        """
        payload = {
            "tenant_id": tenant_id,
            "trace_id": trace_id,
            "node_type": "TranscriptSegment",
            "properties": {**properties, "text": text},
            "source": {
                "session_id": session_id,
                "engine": "knowledge-fusion",
            },
            "tags": tags or ["transcript", "substrate"],
        }
        data = await self._post_json(
            "/api/v1/backbone/nodes", tenant_id, payload
        )
        node_id = data.get("node_id")
        if not isinstance(node_id, str) or not node_id:
            raise BackboneClientError(
                f"backbone response missing node_id: {data!r}"
            )
        return node_id

    async def create_relation(
        self,
        *,
        tenant_id: str,
        trace_id: str,
        from_node_id: str,
        to_node_id: str,
        relation_type: str,
        properties: Optional[dict[str, Any]] = None,
    ) -> None:
        payload = {
            "tenant_id": tenant_id,
            "trace_id": trace_id,
            "from_node_id": from_node_id,
            "to_node_id": to_node_id,
            "relation_type": relation_type,
            "properties": properties or {},
        }
        await self._post_json(
            "/api/v1/backbone/relations", tenant_id, payload
        )

    async def store_knowledge_card_node(
        self,
        *,
        tenant_id: str,
        trace_id: str,
        card_id: str,
        topic_label: str,
        canonical_statement: str,
        product_id: Optional[str] = None,
        tags: Optional[list[str]] = None,
        properties: Optional[dict[str, Any]] = None,
    ) -> str:
        """Create a KNOWLEDGE_CARD node so the card participates in search.

        The embedding is derived from ``canonical_statement`` server-side;
        ``properties`` carries audit fields useful at match time.
        """
        merged_props: dict[str, Any] = {
            "card_id": card_id,
            "topic_label": topic_label,
            "text": canonical_statement,
            "canonical_statement": canonical_statement,
        }
        if product_id:
            merged_props["product_id"] = product_id
        if properties:
            merged_props.update(properties)

        payload = {
            "tenant_id": tenant_id,
            "trace_id": trace_id,
            "node_type": "KnowledgeCard",
            "properties": merged_props,
            "source": {"engine": "knowledge-fusion"},
            "tags": tags or ["knowledge_card"],
        }
        data = await self._post_json(
            "/api/v1/backbone/nodes", tenant_id, payload
        )
        node_id = data.get("node_id")
        if not isinstance(node_id, str) or not node_id:
            raise BackboneClientError(
                f"backbone response missing node_id: {data!r}"
            )
        return node_id

    async def search(
        self,
        *,
        tenant_id: str,
        trace_id: str,
        query: str,
        node_types: Optional[list[str]] = None,
        limit: int = 10,
        min_similarity: float = 0.65,
    ) -> list[dict[str, Any]]:
        """Semantic search filtered by tenant.

        Used by the card synthesizer to locate existing cards similar
        to a newly indexed segment.
        """
        payload = {
            "tenant_id": tenant_id,
            "trace_id": trace_id,
            "query": query,
            "node_types": node_types or ["KnowledgeCard"],
            "limit": max(1, min(100, int(limit))),
            "min_similarity": float(min_similarity),
        }
        data = await self._post_json(
            "/api/v1/backbone/search", tenant_id, payload
        )
        results = data.get("results") or []
        if not isinstance(results, list):
            raise BackboneClientError(
                f"backbone search returned non-list: {type(results).__name__}"
            )
        return results

    async def health(self) -> str:
        try:
            resp = await self._client.get("/health", timeout=3.0)
            return "healthy" if resp.status_code == 200 else f"degraded:{resp.status_code}"
        except Exception as exc:
            return f"unhealthy:{type(exc).__name__}"

    # ── Internals ───────────────────────────────────────────────

    async def _post_json(
        self, path: str, tenant_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        token = self._token_for(tenant_id)
        try:
            resp = await self._client.post(
                path,
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError as exc:
            raise BackboneClientError(
                f"transport failure calling {path}: {exc}"
            ) from exc
        if resp.status_code >= 400:
            raise BackboneClientError(
                f"backbone {path} returned {resp.status_code}: "
                f"{resp.text[:512]}"
            )
        try:
            return resp.json()
        except ValueError as exc:
            raise BackboneClientError(
                f"backbone {path} returned non-JSON body: {exc}"
            ) from exc

    def _token_for(self, tenant_id: str) -> str:
        now = datetime.now(timezone.utc)
        cached = self._token_cache.get(tenant_id)
        # Refresh when within 10% of TTL remaining.
        refresh_at = self._token_ttl * 0.9
        if cached:
            token, expires_at = cached
            remaining = (expires_at - now).total_seconds()
            if remaining > (self._token_ttl - refresh_at):
                return token
        payload = {
            "sub": self._service_user_id,
            "tenant_id": tenant_id,
            "email": f"{self._service_user_id}@nexus.internal",
            "role": self._service_role,
            "permissions": ["*"],
            "name": "knowledge-fusion",
            "jti": uuid.uuid4().hex,
            "type": "access",
            "iat": now,
            "exp": now + timedelta(seconds=self._token_ttl),
        }
        token = jwt.encode(payload, self._jwt_secret, algorithm=self._jwt_algorithm)
        self._token_cache[tenant_id] = (
            token,
            now + timedelta(seconds=self._token_ttl),
        )
        return token
