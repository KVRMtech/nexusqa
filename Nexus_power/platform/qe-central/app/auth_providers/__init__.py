"""QE-Central — pluggable auth-provider registry + request dispatcher (Phase-8).

Selects the active authentication provider from ``QEC_AUTH_PROVIDER`` (default
``jwt``) and dispatches an inbound request to it, producing the provider-agnostic
:class:`Principal` / four-key auth context every existing route consumes.

Backward-compat invariant: with ``QEC_AUTH_PROVIDER`` unset the active provider
is :class:`~app.auth_providers.jwt_provider.JwtAuthProvider`, whose
``authenticate`` delegates verbatim to the unchanged
:func:`app.auth._decode_token` / :func:`app.auth._token_from_request`, so the
dispatched context is byte-identical to today's — nothing changes and the
existing auth suite stays green.

Fail-closed posture:
  * an unknown ``QEC_AUTH_PROVIDER`` value ⇒ :class:`AuthProviderConfigError`
    (dispatcher maps it to a 401 — the request is DENIED, never allowed through);
  * a provider selected but under-configured (e.g. ``oidc`` with no issuer) ⇒
    the same fail-closed 401 at request time;
  * a provider returning ``None`` (no credential) ⇒ 401.

Providers are cached per resolved-config signature so the OIDC JWKS client (and
its key cache) is reused across requests, while a config change (or a test
monkeypatching ``settings``) transparently yields a freshly-built provider.
"""
from __future__ import annotations

import logging
import threading
from typing import Callable

from fastapi import HTTPException, Request

from ..config import settings
from .base import (
    AuthProvider,
    AuthProviderConfigError,
    AuthProviderError,
    Principal,
    first_value,
)
from .jwt_provider import JwtAuthProvider
from .oidc_provider import OidcAuthProvider
from .saml_provider import (
    INTERNAL_PRINCIPAL_ISSUER,
    SamlAuthProvider,
    mint_principal_token,
)

logger = logging.getLogger(__name__)


# ─── Provider factories (name → builder(cfg) -> AuthProvider) ─────────────────
def _build_jwt(cfg) -> AuthProvider:
    # Stateless — reads live settings via _decode_token on each call.
    return JwtAuthProvider()


def _build_oidc(cfg) -> AuthProvider:
    return OidcAuthProvider.from_settings(cfg)


def _build_saml(cfg) -> AuthProvider:
    return SamlAuthProvider.from_settings(cfg)


_FACTORIES: dict[str, Callable[[object], AuthProvider]] = {
    "jwt": _build_jwt,
    "oidc": _build_oidc,
    "saml": _build_saml,
}

_cache: dict[tuple, AuthProvider] = {}
_cache_lock = threading.Lock()


def available_providers() -> tuple[str, ...]:
    """Return the registered provider names (sorted)."""
    return tuple(sorted(_FACTORIES))


def _config_signature(name: str, cfg) -> tuple:
    """A hashable signature of the config a provider is built from.

    Included in the cache key so a config change (or a test monkeypatching
    ``settings``) yields a freshly-built provider rather than a stale one; the
    stateless ``jwt`` provider needs no config, hence an empty signature.
    """
    if name == "oidc":
        return (
            getattr(cfg, "qec_oidc_issuer", ""),
            getattr(cfg, "qec_oidc_jwks_url", ""),
            getattr(cfg, "qec_oidc_audience", ""),
            getattr(cfg, "qec_oidc_algorithms", ""),
            getattr(cfg, "qec_oidc_tenant_claim", ""),
            getattr(cfg, "qec_oidc_role_claim", ""),
            getattr(cfg, "qec_oidc_email_claim", ""),
            getattr(cfg, "qec_oidc_default_role", ""),
            getattr(cfg, "qec_oidc_required_acr", ""),
            getattr(cfg, "qec_oidc_leeway_seconds", 60),
        )
    if name == "saml":
        return (
            getattr(cfg, "qec_saml_idp_entity_id", ""),
            getattr(cfg, "qec_saml_sp_entity_id", ""),
            getattr(cfg, "qec_saml_acs_url", ""),
            getattr(cfg, "qec_saml_tenant_attribute", ""),
            getattr(cfg, "qec_saml_role_attribute", ""),
            getattr(cfg, "qec_saml_email_attribute", ""),
            getattr(cfg, "qec_saml_default_role", ""),
            getattr(cfg, "qec_saml_session_ttl_seconds", 3600),
            getattr(cfg, "qec_jwt_audience", ""),
        )
    return ()


def get_auth_provider(cfg=None) -> AuthProvider:
    """Resolve (and cache) the active provider for ``cfg`` (defaults to settings).

    Raises:
        AuthProviderConfigError: ``QEC_AUTH_PROVIDER`` names an unknown provider,
            or the selected provider is under-configured (raised by its builder).
            Fail-closed — the dispatcher denies the request.
    """
    cfg = cfg if cfg is not None else settings
    name = (getattr(cfg, "qec_auth_provider", "jwt") or "jwt").strip().lower()
    factory = _FACTORIES.get(name)
    if factory is None:
        raise AuthProviderConfigError(
            f"unknown QEC_AUTH_PROVIDER={name!r}; "
            f"valid values: {', '.join(available_providers())}"
        )

    key = (name,) + _config_signature(name, cfg)
    with _cache_lock:
        provider = _cache.get(key)
        if provider is None:
            provider = factory(cfg)
            _cache[key] = provider
        return provider


def reset_provider_cache() -> None:
    """Clear the provider cache (used by tests after mutating ``settings``)."""
    with _cache_lock:
        _cache.clear()


def resolve_principal(request: Request) -> Principal:
    """Authenticate ``request`` via the active provider → :class:`Principal`.

    Fail-closed: a provider-config failure or a ``None`` (no-credential) result
    both raise ``HTTPException(401)`` so the request never reaches a handler
    with an unverified identity.
    """
    try:
        provider = get_auth_provider(settings)
    except AuthProviderConfigError as exc:
        logger.error(
            "qe_central.auth.provider_unavailable error=%s", str(exc)[:200],
        )
        raise HTTPException(
            status_code=401, detail="Authentication provider unavailable",
        )

    principal = provider.authenticate(request)
    if principal is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return principal


def authenticate_request(request: Request) -> dict:
    """Return the four-key auth context for ``request`` (provider-agnostic).

    This is what :mod:`app.auth`'s middleware + ``require_auth`` call; for the
    default ``jwt`` provider it is byte-identical to
    ``_decode_token(_token_from_request(request))``.
    """
    return resolve_principal(request).as_auth_context()


__all__ = [
    "INTERNAL_PRINCIPAL_ISSUER",
    "AuthProvider",
    "AuthProviderConfigError",
    "AuthProviderError",
    "JwtAuthProvider",
    "OidcAuthProvider",
    "Principal",
    "SamlAuthProvider",
    "authenticate_request",
    "available_providers",
    "first_value",
    "get_auth_provider",
    "mint_principal_token",
    "reset_provider_cache",
    "resolve_principal",
]
