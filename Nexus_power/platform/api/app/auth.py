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
    """
    if credentials is None:
        raise HTTPException(status_code=401, detail="Authorization header required")
    try:
        import jwt as pyjwt

        config: PlatformAPIConfig = request.app.state.config
        payload = pyjwt.decode(
            credentials.credentials,
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

    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
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
