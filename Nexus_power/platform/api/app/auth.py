"""
Platform API — JWT Authentication.

Provides the ``get_current_user`` dependency and JWT middleware.
"""
from __future__ import annotations

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from .config import PlatformAPIConfig

_bearer = HTTPBearer(auto_error=False)

# Public routes that skip auth (health checks, docs)
PUBLIC_PATHS = frozenset({"/", "/health", "/docs", "/openapi.json", "/redoc"})


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
):
    """Validate JWT and return decoded user context.

    Attaches user info to ``request.state`` for use by route handlers.

    Reads the token from the standard Bearer header, OR — for resources
    loaded by the browser without a custom fetch (e.g. ``<img src=...?token=>``
    of an annotated frame or run screenshot) that cannot send an
    Authorization header — from the ``?token=`` query param.  Mirrors the
    query fallback already in ``jwt_auth_middleware``; the token is still
    fully validated below, so this stays fail-closed.
    """
    if credentials is not None:
        token = credentials.credentials
    elif request.method == "GET":
        # Query-param fallback is for browser-loaded resources (<img>/<video>
        # src) that cannot send an Authorization header — those are GETs only.
        # Reject ?token= on state-changing methods so a token leaked in a URL
        # (logs, referrer) cannot be replayed to mutate data.
        token = request.query_params.get("token", "") or ""
    else:
        token = ""
    if not token:
        raise HTTPException(status_code=401, detail="Authorization header required")
    try:
        import jwt as pyjwt

        config: PlatformAPIConfig = request.app.state.config
        payload = pyjwt.decode(
            token,
            config.jwt_secret,
            algorithms=[config.jwt_algorithm],
        )
        request.state.user = {
            "user_id": payload.get("sub", "anonymous"),
            "tenant_id": payload.get("tenant_id", "default"),
            "email": payload.get("email", ""),
            "role": payload.get("role", "viewer"),
        }
        return request.state.user
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired token")


async def jwt_auth_middleware(request: Request, call_next):
    """Enforce JWT on all API routes (except public paths).

    Fail-closed: non-public API routes without a valid JWT are rejected
    at the middleware layer, even if the route handler does not declare
    ``Depends(get_current_user)``.
    """
    path = request.url.path.rstrip("/")
    if path in PUBLIC_PATHS or path.startswith("/docs") or path.startswith("/redoc"):
        return await call_next(request)

    # Non-API paths (static, etc.) pass through
    if not path.startswith("/api/"):
        return await call_next(request)

    # Token from the standard Bearer header, OR — for resources loaded by the
    # browser without a custom fetch (e.g. <img src> of a run screenshot) — from
    # a ``?token=`` query param. The query fallback is still a full JWT validated
    # below, so this stays fail-closed; it only changes WHERE the token is read.
    auth_header = request.headers.get("authorization", "")
    token = ""
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
    elif request.method == "GET":
        # ?token= fallback is only for browser-loaded GET resources (<img>/
        # <video> src). Reject query-param tokens on state-changing methods so
        # a token leaked into a URL cannot be replayed to mutate data; header
        # auth still works for those.
        token = request.query_params.get("token", "") or ""

    if token:
        try:
            import jwt as pyjwt

            config: PlatformAPIConfig = request.app.state.config
            payload = pyjwt.decode(
                token, config.jwt_secret, algorithms=[config.jwt_algorithm],
            )
            request.state.user = {
                "user_id": payload.get("sub", "anonymous"),
                "tenant_id": payload.get("tenant_id", "default"),
                "email": payload.get("email", ""),
                "role": payload.get("role", "viewer"),
            }
        except Exception:
            from fastapi.responses import JSONResponse
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid or expired token"},
            )
    else:
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=401,
            content={"detail": "Authorization header required"},
        )

    return await call_next(request)
