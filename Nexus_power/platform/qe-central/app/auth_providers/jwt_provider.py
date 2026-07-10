"""QE-Central — first-party HS256-JWT auth provider (the default seam).

This is today's authentication logic refactored BEHIND the
:class:`app.auth_providers.base.AuthProvider` protocol WITHOUT any behavior
change.  It delegates to the unchanged :func:`app.auth._token_from_request`
(Bearer header, or a GET-only ``?token=`` fallback) and
:func:`app.auth._decode_token` (HS256 verification, the Phase-6 audience gate,
and the fail-closed missing/empty ``tenant_id`` rejection).

Because it reuses those functions verbatim, the produced context
(:meth:`Principal.as_auth_context`) is byte-identical to the dict the service
returns today — the default ``QEC_AUTH_PROVIDER=jwt`` path changes nothing.
"""
from __future__ import annotations

from fastapi import Request

from .base import Principal


class JwtAuthProvider:
    """Default provider: the existing shared-secret HS256 JWT flow.

    Stateless — it reads the live ``settings`` (secret / audience / require-aud)
    through :func:`app.auth._decode_token` on every call, so a config change is
    observed immediately and a single cached instance is safe to reuse.
    """

    name = "jwt"

    def authenticate(self, request: Request) -> Principal | None:
        """Validate the first-party JWT and return its :class:`Principal`.

        Raises ``HTTPException`` (401) on ANY validation failure — missing/empty
        token, bad signature, expiry, a foreign audience, or a missing
        ``tenant_id`` — exactly as :func:`app.auth._decode_token` does today.
        """
        # Imported lazily: app.auth imports this package only from inside its
        # request handlers, so by the time authenticate() runs, app.auth is fully
        # initialised and this import is a cheap sys.modules lookup.
        from ..auth import _decode_token, _token_from_request

        token = _token_from_request(request)
        ctx = _decode_token(token)
        return Principal(
            sub=ctx["sub"],
            tenant_id=ctx["tenant_id"],
            email=ctx["email"],
            role=ctx["role"],
            provider="jwt",
            claims=ctx,
        )


__all__ = ["JwtAuthProvider"]
