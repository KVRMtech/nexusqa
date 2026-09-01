"""QE-Central — SAML auth provider SEAM (assertion → session → principal token).

ACTIVE only when ``QEC_AUTH_PROVIDER=saml``.  SAML is a browser-redirect,
XML-assertion protocol: the IdP POSTs a signed assertion to the Service
Provider's ACS (Assertion Consumer Service) endpoint.  The industry-standard
shape — and the one this seam implements — is:

    IdP  --(signed SAML assertion, POST)-->  ACS endpoint
    ACS  --(validate signature + conditions, map attributes)-->  internal
          principal token (a Verdict-audience HS256 JWT = the "session")
    browser --(Bearer <principal token>)--> every /api/* request

So the provider's request-time job is to validate the **internal principal
token** established after login — which is a first-party Verdict HS256 JWT — using
the SAME proven decoder as the default provider (:func:`app.auth._decode_token`,
including the Phase-6 audience gate + the fail-closed ``tenant_id`` rule).  The
SAML-specific part is the ACS-side translation :meth:`assertion_to_principal_token`,
which maps validated assertion attributes (NameID + tenant/role/email) to that
internal token via :func:`mint_principal_token`.

The XML-signature verification, ``NotOnOrAfter``/``Recipient``/``Audience``
condition checks, and replay protection are the responsibility of the
deployment's ACS handler (which wires a SAML toolkit such as ``python3-saml``
against the client's IdP metadata).  This module deliberately does NOT bundle an
XML parser — it defines the attribute-mapping + token-mint seam those handlers
call, keeping the dependency surface minimal (PyJWT only) and the trust boundary
explicit.

MFA is enforced AT THE IdP (the assertion is only issued after the second
factor).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

import jwt as pyjwt
from fastapi import Request

from ..config import settings
from .base import AuthProviderConfigError, Principal, first_value

logger = logging.getLogger(__name__)

#: ``iss`` stamped on internal principal tokens minted from a SAML assertion, so
#: session tokens are distinguishable from first-party human/service JWTs in an
#: audit trail without altering how they verify.
INTERNAL_PRINCIPAL_ISSUER = "vkpower-verdict-saml"


def mint_principal_token(
    *,
    sub: str,
    tenant_id: str,
    email: str = "",
    role: str = "viewer",
    audience: str,
    secret: str | None = None,
    algorithm: str | None = None,
    ttl_seconds: int = 3600,
) -> str:
    """Mint an internal principal (session) token after a validated SSO login.

    The token is a first-party Verdict HS256 JWT — it carries the Verdict
    ``aud`` so :func:`app.auth._decode_token` accepts it, and the standard
    ``{sub, email, role, tenant_id, iat, exp}`` conventions claims.

    Args:
        sub: subject identity (SAML NameID, or a stable IdP user id).
        tenant_id: the RLS tenant scope (required — a principal token without a
            tenant has no honest meaning).
        email / role: mapped from the assertion (role falls back to viewer).
        audience: the Verdict audience to stamp (``settings.qec_jwt_audience``);
            REQUIRED so the verify side's audience gate accepts it.
        secret / algorithm: default to the shared ``NEXUS_JWT_SECRET`` / HS256.
        ttl_seconds: session lifetime.

    Raises:
        ValueError: on a missing/empty ``tenant_id`` or ``audience``.
    """
    tid = str(tenant_id or "").strip()
    if not tid:
        raise ValueError("tenant_id is required to mint a principal token")
    aud = (audience or "").strip()
    if not aud:
        raise ValueError(
            "audience is required for an internal principal token "
            "(the verify-side audience gate would otherwise reject it)"
        )

    now = datetime.now(timezone.utc)
    claims = {
        "sub": str(sub or "saml-user"),
        "email": str(email or ""),
        "role": str(role or "viewer"),
        "tenant_id": tid,
        "aud": aud,
        "iss": INTERNAL_PRINCIPAL_ISSUER,
        "iat": now,
        "exp": now + timedelta(seconds=int(ttl_seconds)),
    }
    return pyjwt.encode(
        claims,
        secret or settings.nexus_jwt_secret,
        algorithm=algorithm or settings.jwt_algorithm,
    )


class SamlAuthProvider:
    """Validate the post-login internal principal token; map assertions at ACS."""

    name = "saml"

    def __init__(
        self,
        *,
        idp_entity_id: str = "",
        sp_entity_id: str = "",
        acs_url: str = "",
        tenant_attribute: str = "tenant_id",
        role_attribute: str = "role",
        email_attribute: str = "email",
        default_role: str = "viewer",
        session_ttl_seconds: int = 3600,
        audience: str | None = None,
    ) -> None:
        self._idp_entity_id = (idp_entity_id or "").strip()
        self._sp_entity_id = (sp_entity_id or "").strip()
        self._acs_url = (acs_url or "").strip()
        if not self._idp_entity_id:
            raise AuthProviderConfigError(
                "QEC_SAML_IDP_ENTITY_ID is required when QEC_AUTH_PROVIDER=saml"
            )
        self._tenant_attribute = (tenant_attribute or "tenant_id").strip()
        self._role_attribute = (role_attribute or "role").strip()
        self._email_attribute = (email_attribute or "email").strip()
        self._default_role = (default_role or "viewer").strip() or "viewer"
        self._session_ttl = int(session_ttl_seconds)
        # Default the minted-token audience to the Verdict audience so the verify
        # side (_decode_token) accepts the session token out of the box.
        self._audience = (audience or settings.qec_jwt_audience or "").strip()

    @classmethod
    def from_settings(cls, cfg) -> "SamlAuthProvider":
        """Build from a settings-like object (fail-closed on missing config)."""
        return cls(
            idp_entity_id=getattr(cfg, "qec_saml_idp_entity_id", ""),
            sp_entity_id=getattr(cfg, "qec_saml_sp_entity_id", ""),
            acs_url=getattr(cfg, "qec_saml_acs_url", ""),
            tenant_attribute=getattr(cfg, "qec_saml_tenant_attribute", "tenant_id"),
            role_attribute=getattr(cfg, "qec_saml_role_attribute", "role"),
            email_attribute=getattr(cfg, "qec_saml_email_attribute", "email"),
            default_role=getattr(cfg, "qec_saml_default_role", "viewer"),
            session_ttl_seconds=getattr(cfg, "qec_saml_session_ttl_seconds", 3600),
            audience=getattr(cfg, "qec_jwt_audience", ""),
        )

    def assertion_to_principal_token(
        self, name_id: str, attributes: Mapping[str, Any] | None,
    ) -> str:
        """Translate a VALIDATED SAML assertion into an internal principal token.

        Call this from the ACS handler AFTER the SAML toolkit has verified the
        assertion's XML signature and conditions (issuer / audience /
        NotOnOrAfter / replay).  It maps the configured attributes to the
        internal :class:`Principal` and mints the session token every subsequent
        request carries as a Bearer credential.

        Args:
            name_id: the assertion Subject NameID (becomes ``sub``).
            attributes: the assertion AttributeStatement, values as scalars or
                single-element lists (SAML attributes are frequently lists).

        Raises:
            AuthProviderConfigError: when the assertion carries no tenant
                attribute (no RLS scope ⇒ fail-closed, no token minted).
        """
        attrs = attributes or {}
        tenant_id = str(first_value(attrs.get(self._tenant_attribute)) or "").strip()
        if not tenant_id:
            logger.warning(
                "qe_central.auth.saml.assertion_missing_tenant attr=%s",
                self._tenant_attribute,
            )
            raise AuthProviderConfigError(
                f"SAML assertion missing tenant attribute "
                f"{self._tenant_attribute!r}"
            )
        email = str(first_value(attrs.get(self._email_attribute)) or "")
        role = str(first_value(attrs.get(self._role_attribute)) or self._default_role)
        return mint_principal_token(
            sub=name_id or email or "saml-user",
            tenant_id=tenant_id,
            email=email,
            role=role,
            audience=self._audience,
            ttl_seconds=self._session_ttl,
        )

    def authenticate(self, request: Request) -> Principal | None:
        """Validate the internal principal (session) token established at ACS.

        The session token is a first-party Verdict HS256 JWT, so it is verified
        with the SAME proven decoder used for the default provider — the
        Phase-6 audience gate and the fail-closed ``tenant_id`` rule apply
        unchanged.  Raises ``HTTPException`` (401) on any failure.
        """
        from ..auth import _decode_token, _token_from_request

        token = _token_from_request(request)
        ctx = _decode_token(token)
        return Principal(
            sub=ctx["sub"],
            tenant_id=ctx["tenant_id"],
            email=ctx["email"],
            role=ctx["role"],
            provider="saml",
            claims=ctx,
        )


__all__ = [
    "INTERNAL_PRINCIPAL_ISSUER",
    "SamlAuthProvider",
    "mint_principal_token",
]
