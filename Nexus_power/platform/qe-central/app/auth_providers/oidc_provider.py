"""QE-Central — OIDC auth provider (IdP-issued ID token verified via JWKS).

ACTIVE only when ``QEC_AUTH_PROVIDER=oidc``.  Verifies an OpenID-Connect ID token
issued by the client's IdP (Okta / Azure AD / Ping / …):

  * signature   — against the IdP's published JWKS (``QEC_OIDC_JWKS_URL``), keyed
                  by the token's ``kid`` (asymmetric RS256/ES256 — the IdP's
                  private key never touches Verdict);
  * issuer      — must equal ``QEC_OIDC_ISSUER``;
  * audience    — must contain ``QEC_OIDC_AUDIENCE``;
  * expiry      — ``exp`` required (small configurable clock leeway).

The IdP-verified claims are then mapped to the internal :class:`Principal` via
configurable claim names (tenant / role / email), so an IdP claim
(``QEC_OIDC_TENANT_CLAIM``) drives the Verdict RLS tenant.

MFA is enforced AT THE IdP (the IdP will not mint the ID token until the user
clears the second factor).  As optional defense-in-depth, ``QEC_OIDC_REQUIRED_ACR``
can pin the accepted ``acr`` (Authentication Context Class Reference) values so
Verdict additionally REFUSES a token whose IdP did not assert the required
authentication strength.

Depends only on PyJWT (already a base dependency); JWKS fetching + key caching is
PyJWT's :class:`jwt.PyJWKClient`.  For unit testing (and any deployment that
resolves keys out-of-band) an explicit ``signing_key_resolver`` may be injected,
in which case no network is touched.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Iterable, Sequence

import jwt as pyjwt
from fastapi import HTTPException, Request

from .base import AuthProviderConfigError, Principal

logger = logging.getLogger(__name__)

#: A resolver maps a raw JWT to the verification key (a PEM/`PyJWK`/public key).
SigningKeyResolver = Callable[[str], Any]

_DEFAULT_ALGORITHMS: tuple[str, ...] = ("RS256",)


class OidcAuthProvider:
    """Verify an IdP-issued OIDC ID token and map its claims to a Principal."""

    name = "oidc"

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        jwks_url: str = "",
        algorithms: Sequence[str] = _DEFAULT_ALGORITHMS,
        tenant_claim: str = "tenant_id",
        role_claim: str = "role",
        email_claim: str = "email",
        default_role: str = "viewer",
        required_acr: Iterable[str] = (),
        leeway_seconds: int = 60,
        signing_key_resolver: SigningKeyResolver | None = None,
    ) -> None:
        issuer = (issuer or "").strip()
        audience = (audience or "").strip()
        jwks_url = (jwks_url or "").strip()
        if not issuer:
            raise AuthProviderConfigError(
                "QEC_OIDC_ISSUER is required when QEC_AUTH_PROVIDER=oidc"
            )
        if not audience:
            raise AuthProviderConfigError(
                "QEC_OIDC_AUDIENCE is required when QEC_AUTH_PROVIDER=oidc"
            )
        if signing_key_resolver is None and not jwks_url:
            raise AuthProviderConfigError(
                "QEC_OIDC_JWKS_URL is required when QEC_AUTH_PROVIDER=oidc "
                "(unless an explicit signing-key resolver is supplied)"
            )

        self._issuer = issuer
        self._audience = audience
        self._jwks_url = jwks_url
        self._algorithms = tuple(algorithms) or _DEFAULT_ALGORITHMS
        self._tenant_claim = (tenant_claim or "tenant_id").strip()
        self._role_claim = (role_claim or "role").strip()
        self._email_claim = (email_claim or "email").strip()
        self._default_role = (default_role or "viewer").strip() or "viewer"
        self._required_acr = tuple(a for a in required_acr if a)
        self._leeway = max(0, int(leeway_seconds))
        self._explicit_resolver = signing_key_resolver
        self._jwks_client: pyjwt.PyJWKClient | None = None

    @classmethod
    def from_settings(
        cls, cfg, *, signing_key_resolver: SigningKeyResolver | None = None,
    ) -> "OidcAuthProvider":
        """Build from a settings-like object (fail-closed on missing config)."""
        algorithms = tuple(
            a.strip()
            for a in (getattr(cfg, "qec_oidc_algorithms", "") or "RS256").split(",")
            if a.strip()
        ) or _DEFAULT_ALGORITHMS
        required_acr = tuple(
            a.strip()
            for a in (getattr(cfg, "qec_oidc_required_acr", "") or "").split(",")
            if a.strip()
        )
        return cls(
            issuer=getattr(cfg, "qec_oidc_issuer", ""),
            audience=getattr(cfg, "qec_oidc_audience", ""),
            jwks_url=getattr(cfg, "qec_oidc_jwks_url", ""),
            algorithms=algorithms,
            tenant_claim=getattr(cfg, "qec_oidc_tenant_claim", "tenant_id"),
            role_claim=getattr(cfg, "qec_oidc_role_claim", "role"),
            email_claim=getattr(cfg, "qec_oidc_email_claim", "email"),
            default_role=getattr(cfg, "qec_oidc_default_role", "viewer"),
            required_acr=required_acr,
            leeway_seconds=getattr(cfg, "qec_oidc_leeway_seconds", 60),
            signing_key_resolver=signing_key_resolver,
        )

    # ── key resolution ────────────────────────────────────────────────────
    def _resolve_signing_key(self, token: str) -> Any:
        """Resolve the verification key for ``token`` (JWKS or injected)."""
        if self._explicit_resolver is not None:
            return self._explicit_resolver(token)
        if self._jwks_client is None:
            # Lazily built so construction (and the registry cache) touches no
            # network; PyJWKClient caches fetched keys across requests.
            self._jwks_client = pyjwt.PyJWKClient(self._jwks_url)
        return self._jwks_client.get_signing_key_from_jwt(token).key

    # ── authenticate ──────────────────────────────────────────────────────
    def authenticate(self, request: Request) -> Principal | None:
        """Verify the OIDC ID token and return its :class:`Principal`.

        Raises ``HTTPException`` (401) with a specific, non-leaking detail on any
        rejection (missing token, unresolvable key, wrong issuer/audience,
        expiry, insufficient ``acr``, or a missing tenant claim).
        """
        from ..auth import _token_from_request

        token = _token_from_request(request)
        if not token:
            raise HTTPException(status_code=401, detail="Authorization header required")

        try:
            key = self._resolve_signing_key(token)
        except HTTPException:
            raise
        except Exception as exc:  # JWKS unreachable / no matching kid / malformed
            logger.warning(
                "qe_central.auth.oidc.signing_key_unresolved error=%s",
                str(exc)[:200],
            )
            raise HTTPException(
                status_code=401, detail="Unable to resolve token signing key",
            )

        try:
            payload = pyjwt.decode(
                token,
                key,
                algorithms=list(self._algorithms),
                audience=self._audience,
                issuer=self._issuer,
                leeway=self._leeway,
                options={"require": ["exp", "iss", "aud"]},
            )
        except pyjwt.InvalidIssuerError:
            logger.warning("qe_central.auth.oidc.rejected_issuer")
            raise HTTPException(status_code=401, detail="Invalid token issuer")
        except pyjwt.InvalidAudienceError:
            logger.warning("qe_central.auth.oidc.rejected_audience")
            raise HTTPException(status_code=401, detail="Invalid token audience")
        except pyjwt.ExpiredSignatureError:
            raise HTTPException(status_code=401, detail="Invalid or expired token")
        except pyjwt.MissingRequiredClaimError as exc:
            claim = getattr(exc, "claim", "") or ""
            raise HTTPException(
                status_code=401,
                detail=f"Token missing required claim: {claim}" if claim
                else "Token missing a required claim",
            )
        except HTTPException:
            raise
        except Exception:
            # Bad signature / malformed / unsupported alg — never echo internals.
            raise HTTPException(status_code=401, detail="Invalid or expired token")

        # MFA defense-in-depth: refuse a token whose IdP did not assert the
        # required authentication-context class (the IdP is the primary MFA gate).
        if self._required_acr:
            acr = str(payload.get("acr") or "")
            if acr not in self._required_acr:
                logger.warning("qe_central.auth.oidc.rejected_acr")
                raise HTTPException(
                    status_code=401,
                    detail="Token does not satisfy required authentication "
                    "context (MFA)",
                )

        tenant_id = str(payload.get(self._tenant_claim) or "").strip()
        if not tenant_id:
            logger.warning(
                "qe_central.auth.oidc.missing_tenant_claim claim=%s",
                self._tenant_claim,
            )
            raise HTTPException(status_code=401, detail="Token missing tenant claim")

        return Principal(
            sub=str(payload.get("sub") or "anonymous"),
            tenant_id=tenant_id,
            email=str(payload.get(self._email_claim) or ""),
            role=str(payload.get(self._role_claim) or self._default_role),
            provider="oidc",
            claims=dict(payload),
        )


__all__ = ["OidcAuthProvider", "SigningKeyResolver"]
