"""Validate the Bot Framework JWT on every inbound request.

Microsoft signs every webhook with a JWT issued by the Bot Framework's
identity provider. The OpenID metadata document at
``https://login.botframework.com/v1/.well-known/openidconfiguration``
points at the JWKS we use to validate the bearer token.

Production correctness requirements (all enforced here):

    * RS256 signature verified against a key from the published JWKS.
    * ``iss`` is one of the known Bot Framework issuers.
    * ``aud`` matches the bot's Microsoft App ID (per-tenant config).
    * ``exp`` / ``nbf`` honored with a small skew tolerance.
    * ``serviceUrl`` claim (when present) matches the activity's
      ``serviceUrl`` to mitigate spoofed activity dispatch.

The metadata + JWKS are cached for ``cache_ttl_seconds``; the
``MetadataLoader`` makes the network IO injectable so tests can use
fixtures instead of live Microsoft endpoints.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

import httpx
import jwt
from jwt import InvalidTokenError, PyJWK

logger = logging.getLogger(__name__)


_DEFAULT_METADATA_URL = (
    "https://login.botframework.com/v1/.well-known/openidconfiguration"
)
# These issuers are stable per Microsoft docs (Public Cloud).
_DEFAULT_ALLOWED_ISSUERS = (
    "https://api.botframework.com",
)


class TeamsAuthError(Exception):
    """JWT validation failed for any reason."""


# ── Metadata + JWKS loader ─────────────────────────────────────


JsonLoader = Callable[[str], Awaitable[dict[str, Any]]]


async def _default_http_loader(url: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.json()


@dataclass
class _CachedJwks:
    keys_by_kid: dict[str, dict[str, Any]]
    expires_at: float


class TeamsMetadataLoader:
    """Loads + caches Bot Framework OIDC metadata and JWKS."""

    def __init__(
        self,
        *,
        metadata_url: str = _DEFAULT_METADATA_URL,
        cache_ttl_seconds: int = 3600,
        loader: Optional[JsonLoader] = None,
    ):
        self._metadata_url = metadata_url
        self._ttl = max(60, int(cache_ttl_seconds))
        self._loader = loader or _default_http_loader
        self._metadata: Optional[dict[str, Any]] = None
        self._metadata_expires_at = 0.0
        self._jwks: Optional[_CachedJwks] = None

    async def get_metadata(self) -> dict[str, Any]:
        now = time.monotonic()
        if self._metadata is None or now >= self._metadata_expires_at:
            md = await self._loader(self._metadata_url)
            if not isinstance(md, dict) or "jwks_uri" not in md:
                raise TeamsAuthError(
                    "OIDC metadata missing 'jwks_uri'"
                )
            self._metadata = md
            self._metadata_expires_at = now + self._ttl
        return self._metadata

    async def get_signing_key(self, kid: str) -> dict[str, Any]:
        if not kid:
            raise TeamsAuthError("JWT missing 'kid' header")
        now = time.monotonic()
        if self._jwks is None or now >= self._jwks.expires_at:
            md = await self.get_metadata()
            jwks_doc = await self._loader(md["jwks_uri"])
            keys = jwks_doc.get("keys") if isinstance(jwks_doc, dict) else None
            if not isinstance(keys, list) or not keys:
                raise TeamsAuthError("JWKS document has no keys")
            by_kid: dict[str, dict[str, Any]] = {}
            for k in keys:
                if isinstance(k, dict) and isinstance(k.get("kid"), str):
                    by_kid[k["kid"]] = k
            self._jwks = _CachedJwks(
                keys_by_kid=by_kid, expires_at=now + self._ttl
            )
        key = self._jwks.keys_by_kid.get(kid)
        if key is None:
            # Force refresh in case Microsoft rotated keys.
            self._jwks = None
            md = await self.get_metadata()
            jwks_doc = await self._loader(md["jwks_uri"])
            keys = jwks_doc.get("keys") if isinstance(jwks_doc, dict) else None
            by_kid = {
                k["kid"]: k
                for k in (keys or [])
                if isinstance(k, dict) and isinstance(k.get("kid"), str)
            }
            self._jwks = _CachedJwks(
                keys_by_kid=by_kid,
                expires_at=time.monotonic() + self._ttl,
            )
            key = by_kid.get(kid)
        if key is None:
            raise TeamsAuthError(f"unknown JWT kid={kid!r}")
        return key


# ── Verifier ───────────────────────────────────────────────────


class TeamsTokenVerifier:
    """Validate a Bot Framework JWT bearer token."""

    def __init__(
        self,
        metadata: TeamsMetadataLoader,
        *,
        allowed_issuers: tuple[str, ...] = _DEFAULT_ALLOWED_ISSUERS,
        leeway_seconds: int = 60,
    ):
        self._metadata = metadata
        self._allowed_issuers = tuple(allowed_issuers)
        self._leeway = max(0, int(leeway_seconds))

    async def verify(
        self,
        *,
        authorization_header: Optional[str],
        expected_audience: str,
        expected_service_url: Optional[str] = None,
    ) -> dict[str, Any]:
        if not expected_audience:
            raise TeamsAuthError("expected_audience must be set")
        if not authorization_header or not authorization_header.startswith("Bearer "):
            raise TeamsAuthError("missing or malformed Authorization header")
        token = authorization_header[len("Bearer "):].strip()
        if not token:
            raise TeamsAuthError("empty bearer token")

        try:
            unverified_header = jwt.get_unverified_header(token)
        except InvalidTokenError as exc:
            raise TeamsAuthError(f"unable to decode JWT header: {exc}") from exc

        kid = unverified_header.get("kid")
        alg = unverified_header.get("alg")
        if alg != "RS256":
            raise TeamsAuthError(
                f"unsupported JWT alg: {alg!r} (require RS256)"
            )

        jwk_dict = await self._metadata.get_signing_key(kid or "")
        signing_key = PyJWK(jwk_dict).key

        try:
            claims = jwt.decode(
                token,
                key=signing_key,
                algorithms=["RS256"],
                audience=expected_audience,
                leeway=self._leeway,
                options={"require": ["exp", "iat", "iss", "aud"]},
            )
        except InvalidTokenError as exc:
            raise TeamsAuthError(f"JWT validation failed: {exc}") from exc

        iss = claims.get("iss")
        if iss not in self._allowed_issuers:
            raise TeamsAuthError(f"untrusted issuer: {iss!r}")

        if expected_service_url:
            claim_su = claims.get("serviceurl") or claims.get("serviceUrl")
            if isinstance(claim_su, str) and claim_su:
                # Allow trailing-slash mismatch which Microsoft sometimes
                # emits but otherwise insist on an exact match.
                if claim_su.rstrip("/") != expected_service_url.rstrip("/"):
                    raise TeamsAuthError(
                        "serviceUrl claim does not match activity serviceUrl"
                    )
        return claims
