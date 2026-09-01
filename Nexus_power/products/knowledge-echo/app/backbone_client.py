"""Backbone HTTP client for semantic search.

Mirrors the auth pattern from the fusion engine's BackboneClient —
service JWT minting cached per tenant — but only exposes the
``/api/v1/backbone/search`` endpoint plus a health probe.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx
import jwt

logger = logging.getLogger(__name__)


class BackboneSearchError(Exception):
    """Transport / decode failure when calling Backbone search."""


class BackboneSearchClient:
    """Tenant-scoped search over the Backbone graph + vector store."""

    def __init__(
        self,
        base_url: str,
        *,
        jwt_secret: str,
        jwt_algorithm: str = "HS256",
        service_user_id: str = "service:knowledge-echo",
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
            base_url=self._base_url, timeout=timeout_seconds
        )
        self._token_cache: dict[str, tuple[str, datetime]] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

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
        """Return ranked candidates (already filtered by tenant)."""
        payload = {
            "tenant_id": tenant_id,
            "trace_id": trace_id,
            "query": query,
            "node_types": node_types or [
                "TranscriptSegment",
                "BusinessRule",
                "KnowledgeCard",
            ],
            "limit": max(1, min(100, int(limit))),
            "min_similarity": float(min_similarity),
        }
        token = self._token_for(tenant_id)
        try:
            resp = await self._client.post(
                "/api/v1/backbone/search",
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError as exc:
            raise BackboneSearchError(
                f"transport failure calling search: {exc}"
            ) from exc
        if resp.status_code >= 400:
            raise BackboneSearchError(
                f"backbone search returned {resp.status_code}: "
                f"{resp.text[:512]}"
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise BackboneSearchError(
                f"backbone search returned non-JSON: {exc}"
            ) from exc
        results = data.get("results") or []
        if not isinstance(results, list):
            raise BackboneSearchError(
                f"backbone search returned non-list results: {type(results).__name__}"
            )
        return results

    async def health(self) -> str:
        try:
            resp = await self._client.get("/health", timeout=3.0)
            return "healthy" if resp.status_code == 200 else f"degraded:{resp.status_code}"
        except Exception as exc:
            return f"unhealthy:{type(exc).__name__}"

    def _token_for(self, tenant_id: str) -> str:
        now = datetime.now(timezone.utc)
        cached = self._token_cache.get(tenant_id)
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
            "name": "knowledge-echo",
            "jti": uuid.uuid4().hex,
            "type": "access",
            "iat": now,
            "exp": now + timedelta(seconds=self._token_ttl),
        }
        token = jwt.encode(
            payload, self._jwt_secret, algorithm=self._jwt_algorithm
        )
        self._token_cache[tenant_id] = (
            token,
            now + timedelta(seconds=self._token_ttl),
        )
        return token
