"""QE-Central — pluggable authentication-provider SEAM (Phase-8, core types).

Regulated / on-prem buyers require SSO (SAML / OIDC) + MFA.  This package adds a
pluggable auth-provider seam ON TOP of the existing HS256-JWT auth
(:mod:`app.auth`) WITHOUT changing today's behavior: the provider is selected by
``QEC_AUTH_PROVIDER`` (default ``jwt``), so with the variable unset the service
authenticates exactly as it does today (the 842-test posture is unchanged).

Every provider — regardless of the wire protocol it speaks (first-party HS256
JWT, an IdP-issued OIDC ID token verified via JWKS, or a SAML assertion exchanged
for an internal session token) — produces the SAME downstream principal
(:class:`Principal`).  :meth:`Principal.as_auth_context` returns the identical
four-key context dict (``sub`` / ``tenant_id`` / ``email`` / ``role``) that
:func:`app.auth._decode_token` returns today, so every existing route,
``require_role`` gate, and the RLS tenant-scoping (``nexus.current_tenant_id``
GUC) keep working byte-for-byte.

This module holds the protocol + value types ONLY (no provider implementations,
no FastAPI wiring) so the individual provider modules can import it without a
cycle.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, runtime_checkable

from fastapi import Request


class AuthProviderError(RuntimeError):
    """Base class for auth-provider configuration / wiring failures.

    Distinct from an authentication *rejection* (a per-request 401 raised as an
    ``HTTPException``): this signals that the SELECTED provider itself cannot be
    honoured (unknown name, or a provider chosen but left under-configured).  The
    dispatcher treats it fail-closed — the request is DENIED, never allowed
    through with an unverified identity.
    """


class AuthProviderConfigError(AuthProviderError):
    """Raised when ``QEC_AUTH_PROVIDER`` names an unknown provider, or a
    configured provider is missing a mandatory setting (e.g. ``oidc`` selected
    with no ``QEC_OIDC_ISSUER``).  Fail-closed: the dispatcher maps it to a 401.
    """


@dataclass(frozen=True)
class Principal:
    """The provider-agnostic authenticated identity.

    ``sub``/``tenant_id``/``email``/``role`` are the shared-convention claims the
    rest of QE-Central consumes; ``tenant_id`` is the RLS scope and is always
    required (a principal with no tenant has no honest meaning here — every
    QE-Central operation is tenant-scoped).  ``provider`` records which seam
    authenticated the request (audit/observability only).  ``claims`` carries the
    raw verified payload for downstream audit hooks; it is DELIBERATELY excluded
    from :meth:`as_auth_context` so the request-state context stays byte-identical
    to today's four-key dict.
    """

    sub: str
    tenant_id: str
    email: str = ""
    role: str = "viewer"
    provider: str = "jwt"
    claims: Mapping[str, Any] = field(default_factory=dict, repr=False)

    def as_auth_context(self) -> dict:
        """Return the four-key auth context set on ``request.state.user``.

        Byte-for-byte the shape :func:`app.auth._decode_token` returns today
        (``{sub, tenant_id, email, role}``) so no downstream consumer changes.
        """
        return {
            "sub": self.sub,
            "tenant_id": self.tenant_id,
            "email": self.email,
            "role": self.role,
        }


@runtime_checkable
class AuthProvider(Protocol):
    """Protocol every authentication provider implements.

    Contract for :meth:`authenticate`:
      * returns a :class:`Principal` when the request carries a VALID credential
        for this provider;
      * returns ``None`` when NO credential is present and the provider elects to
        defer (the dispatcher then fail-closes to 401);
      * raises ``fastapi.HTTPException`` (401/…) when a credential IS present but
        invalid — carrying a specific, non-leaking ``detail`` — so the fail-closed
        posture and error semantics of :mod:`app.auth` are preserved.
    """

    #: Stable provider key, matching a ``QEC_AUTH_PROVIDER`` value.
    name: str

    def authenticate(self, request: Request) -> Principal | None:
        """Authenticate ``request`` and return its :class:`Principal` (or None)."""
        ...


def first_value(value: Any) -> Any:
    """Return the first element of a list/tuple claim, else the value itself.

    IdP assertions frequently deliver a single logical attribute as a
    single-element list (notably SAML ``AttributeValue`` sets and some OIDC
    multi-valued claims).  Normalising here keeps the claim-mapping in the
    providers simple and consistent.
    """
    if isinstance(value, (list, tuple)):
        return value[0] if value else None
    return value


__all__ = [
    "AuthProvider",
    "AuthProviderConfigError",
    "AuthProviderError",
    "Principal",
    "first_value",
]
