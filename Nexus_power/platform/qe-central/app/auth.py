"""QE-Central — JWT Authentication (fail-closed, HS256, shared secret).

Mirrors ``platform/api/app/auth.py`` mechanics: Bearer-header token with a
GET-only ``?token=`` query fallback for browser-loaded resources, validated
against the shared ``NEXUS_JWT_SECRET``, plus a fail-closed middleware that
rejects every non-public ``/api/*`` request without a valid token — even if
the route handler forgot ``Depends(require_auth)``.

ONE deliberate hardening on top of the platform-api mirror: a token whose
``tenant_id`` claim is missing/empty is REJECTED (401) instead of falling
back to a "default" tenant — every QE-Central operation is tenant-scoped
(RLS GUC), so an untenanted principal has no honest meaning here.
"""
from __future__ import annotations

import logging

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import settings

logger = logging.getLogger(__name__)

_bearer = HTTPBearer(auto_error=False)

# Public routes that skip auth (health checks, docs) — platform-api parity.
PUBLIC_PATHS = frozenset({"/", "/health", "/docs", "/openapi.json", "/redoc"})


def _accept_missing_aud(
    token: str, secret: str, algorithms: list[str],
) -> dict:
    """Handle a validly-signed token that carries NO ``aud`` claim.

    The transition window (Phase-6): today's tokens — including shared VKPower
    human sessions and ``mint_service_jwt`` service tokens — carry no ``aud``.
    They are ACCEPTED so nothing breaks while issuers migrate, UNLESS
    ``QEC_REQUIRE_AUD`` is set (then REJECTED).  In a deployed env the acceptance
    is logged loudly so operators can see they still have un-audienced issuers
    before flipping ``QEC_REQUIRE_AUD`` on.

    The signature (and ``exp``) were already verified by the caller's
    ``pyjwt.decode`` (which raised ``MissingRequiredClaimError`` only AFTER those
    passed); the re-decode here just returns the payload with ``aud`` unchecked.
    """
    if settings.qec_require_aud:
        logger.warning("qe_central.auth.rejected_missing_audience_required")
        raise HTTPException(
            status_code=401, detail="Token missing required audience claim",
        )
    import jwt as pyjwt

    try:
        payload = pyjwt.decode(
            token, secret, algorithms=algorithms, options={"verify_aud": False},
        )
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    if settings.is_deployed_env:
        logger.warning(
            "qe_central.auth.accepted_missing_audience_transition "
            "(token has no `aud`; set QEC_REQUIRE_AUD once every issuer stamps it)"
        )
    return payload


def _decode_token(token: str) -> dict:
    """Validate an HS256 JWT and return the QE-Central user context.

    Returns ``{sub, tenant_id, email, role}`` (shared-conventions keys).
    Raises 401 on ANY validation failure — bad signature, expiry, malformed
    payload, or a missing/empty ``tenant_id`` claim (fail-closed).

    Phase-6 audience gate (no VKPower<->Verdict token bleed): the token's ``aud``
    is verified against ``QEC_JWT_AUDIENCE`` (default ``vkpower-verdict``).
      * ``aud`` present and MATCHING           → accepted.
      * ``aud`` present but WRONG (foreign)     → REJECTED (401), every env.
      * ``aud`` MISSING                         → transition-accepted (or rejected
        when ``QEC_REQUIRE_AUD``); see :func:`_accept_missing_aud`.
    Setting ``QEC_JWT_AUDIENCE=""`` opts the audience gate out entirely.
    """
    if not token:
        raise HTTPException(status_code=401, detail="Authorization header required")

    import jwt as pyjwt
    from jwt import InvalidAudienceError, MissingRequiredClaimError

    secret = settings.nexus_jwt_secret
    algorithms = [settings.jwt_algorithm]
    audience = (settings.qec_jwt_audience or "").strip()

    try:
        if audience:
            # decode(audience=...) accepts a matching aud, raises
            # InvalidAudienceError on a wrong aud, and MissingRequiredClaimError
            # when the token carries no aud at all.
            payload = pyjwt.decode(
                token, secret, algorithms=algorithms, audience=audience,
            )
        else:
            # Audience gate opted out (QEC_JWT_AUDIENCE="") — pre-Phase-6 posture.
            payload = pyjwt.decode(
                token, secret, algorithms=algorithms,
                options={"verify_aud": False},
            )
    except InvalidAudienceError:
        # aud present but not ours (e.g. a token minted for a different service).
        # ALWAYS reject — in every environment, incl. development/test.
        logger.warning("qe_central.auth.rejected_foreign_audience")
        raise HTTPException(status_code=401, detail="Invalid token audience")
    except MissingRequiredClaimError as exc:
        # Raised only because we asked for `audience` but the token has no `aud`.
        if (getattr(exc, "claim", "") or "") != "aud":
            raise HTTPException(status_code=401, detail="Invalid or expired token")
        payload = _accept_missing_aud(token, secret, algorithms)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    tenant_id = str(payload.get("tenant_id") or "").strip()
    if not tenant_id:
        # Hardening over the platform-api mirror: no "default"-tenant fallback.
        raise HTTPException(status_code=401, detail="Token missing tenant_id claim")

    return {
        "sub": str(payload.get("sub") or "anonymous"),
        "tenant_id": tenant_id,
        "email": str(payload.get("email") or ""),
        "role": str(payload.get("role") or "viewer"),
    }


def _token_from_request(request: Request) -> str:
    """Extract the JWT from the Bearer header, or — GET only — ``?token=``.

    The query fallback exists for browser-loaded resources (``<img src>``)
    that cannot send an Authorization header.  It is rejected on
    state-changing methods so a token leaked into a URL (logs, referrer)
    can never be replayed to mutate data — platform-api parity.
    """
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:]
    if request.method == "GET":
        return request.query_params.get("token", "") or ""
    return ""


async def require_auth(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
) -> dict:
    """FastAPI dependency: authenticate the request and return the user context.

    Routes through the Phase-8 pluggable auth-provider registry
    (:func:`app.auth_providers.authenticate_request`), so the SAME downstream
    four-key context (``sub``/``tenant_id``/``email``/``role``) is produced
    regardless of the configured provider (``QEC_AUTH_PROVIDER``).  For the
    DEFAULT ``jwt`` provider this is byte-identical to
    ``_decode_token(_token_from_request(request))`` — today's behavior unchanged.

    The ``credentials`` dependency is retained so OpenAPI still advertises the
    Bearer scheme (the Authorize button); the provider re-reads the token from
    the request, so no behavior depends on this parameter.

    Attaches the user to ``request.state.user`` for handlers/audit hooks.
    """
    # Lazy import: app.auth_providers imports this module, so importing it here
    # (request time, after this module is fully initialised) avoids an import
    # cycle while remaining a cheap sys.modules lookup after the first call.
    from .auth_providers import authenticate_request

    user = authenticate_request(request)
    request.state.user = user
    return user


def require_role(*roles: str):
    """Dependency factory: require one of ``roles`` (e.g. 'admin','manager').

    Mirrors the platform-api RBAC gate (test_factory.py `_PRIVILEGED`):
    403 for authenticated-but-underprivileged principals.
    """
    allowed = frozenset(roles)

    async def _dep(user: dict = Depends(require_auth)) -> dict:
        if user.get("role", "viewer") not in allowed:
            raise HTTPException(
                status_code=403,
                detail=f"Requires role: {'|'.join(sorted(allowed))}",
            )
        return user

    return _dep


async def jwt_auth_middleware(request: Request, call_next):
    """Enforce JWT on all ``/api/*`` routes (except public paths).

    Fail-closed: non-public API routes without a valid JWT are rejected at
    the middleware layer, even if the route handler does not declare
    ``Depends(require_auth)`` — byte-for-byte the platform-api posture.
    """
    path = request.url.path.rstrip("/")
    if path in PUBLIC_PATHS or path.startswith("/docs") or path.startswith("/redoc"):
        return await call_next(request)

    # Non-API paths (static, etc.) pass through.
    if not path.startswith("/api/"):
        return await call_next(request)

    # Route through the Phase-8 provider registry (default ``jwt`` ⇒ byte-identical
    # to _decode_token(_token_from_request(request))).  Lazy import breaks the
    # app.auth <-> app.auth_providers cycle (see require_auth).
    from .auth_providers import authenticate_request

    try:
        request.state.user = authenticate_request(request)
    except HTTPException as exc:
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    return await call_next(request)
