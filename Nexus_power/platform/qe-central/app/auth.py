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


def _decode_token(token: str) -> dict:
    """Validate an HS256 JWT and return the QE-Central user context.

    Returns ``{sub, tenant_id, email, role}`` (shared-conventions keys).
    Raises 401 on ANY validation failure — bad signature, expiry, malformed
    payload, or a missing/empty ``tenant_id`` claim (fail-closed).
    """
    if not token:
        raise HTTPException(status_code=401, detail="Authorization header required")
    try:
        import jwt as pyjwt

        payload = pyjwt.decode(
            token,
            settings.nexus_jwt_secret,
            algorithms=[settings.jwt_algorithm],
        )
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
    """FastAPI dependency: validate the JWT and return the user context.

    Attaches the user to ``request.state.user`` for handlers/audit hooks.
    """
    if credentials is not None:
        token = credentials.credentials
    else:
        token = _token_from_request(request)
    user = _decode_token(token)
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

    token = _token_from_request(request)
    try:
        request.state.user = _decode_token(token)
    except HTTPException as exc:
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    return await call_next(request)
