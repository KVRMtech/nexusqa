"""
Nexus API Gateway v0.2.0 — Reverse Proxy & Rate Limiter.

v0.2.0 — Modular refactor:
  app.config       → GatewayConfig
  app.routes       → Route table builder
  app.rate_limiter → Sliding-window per-tenant rate limiter
  app.proxy        → Reverse-proxy request handler

Routes all /api/v1/* traffic to the correct engine.
Provides:
  - JWT validation at the edge
  - Per-tenant sliding-window rate limiting
  - Health-check aggregation across all engines
  - CORS handling
"""

from __future__ import annotations

import os
import sys
import logging

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

# ─── Path Setup ────────────────────────────────────────────────
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_SDK_PATH = os.path.join(_THIS_DIR, "..", "..", "sdk", "nexus-sdk")
for p in [_THIS_DIR, _SDK_PATH]:
    if p not in sys.path:
        sys.path.insert(0, p)

# ─── Modular sub-packages ─────────────────────────────────────
from app.config import GatewayConfig
from app.routes import build_route_table
from app.rate_limiter import RateLimiter
from app.proxy import proxy_request

# Security middleware from SDK
try:
    from nexus_sdk.security.headers import SecurityHeadersMiddleware
    from nexus_sdk.security.sanitization import RequestSizeLimitMiddleware
    _HAS_SDK_SECURITY = True
except ImportError:
    _HAS_SDK_SECURITY = False

logger = logging.getLogger(__name__)

# ─── Configuration ─────────────────────────────────────────────
config = GatewayConfig()
ROUTES = build_route_table(config)

# ─── Insecure Defaults Check ──────────────────────────────────
_INSECURE_DEFAULTS = {
    "nexus-change-in-production",
    "dev-jwt-secret-change-me",
    "dev-jwt-secret",
    "nexus-minio-secret-change-me",
}

_nexus_env = os.getenv("NEXUS_ENV", "development").lower()
if _nexus_env == "production" and config.nexus_jwt_secret in _INSECURE_DEFAULTS:
    logger.critical(
        "SECURITY: JWT secret is set to an insecure default (%s). "
        "Set NEXUS_JWT_SECRET to a strong random value before deploying to production! "
        "Refusing to start in NEXUS_ENV=production with default secrets.",
        config.nexus_jwt_secret[:8] + "...",
    )
    sys.exit(1)
elif config.nexus_jwt_secret in _INSECURE_DEFAULTS:
    logger.warning(
        "SECURITY WARNING: JWT secret is set to a default value. "
        "This is acceptable for development but MUST be changed for production. "
        "Set NEXUS_JWT_SECRET to a strong random value."
    )

# ─── Application ───────────────────────────────────────────────
app = FastAPI(
    title="Nexus API Gateway",
    description="Reverse proxy routing to all Nexus engines",
    version="0.2.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in config.cors_origins.split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Security headers + request size limit (from SDK)
if _HAS_SDK_SECURITY:
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(
        RequestSizeLimitMiddleware,
        max_body_size=config.gateway_max_request_body_mb * 1024 * 1024,
    )

# ─── Shared Singletons ────────────────────────────────────────
_client = httpx.AsyncClient(timeout=900.0)
_rate_limiter = RateLimiter(config.rate_limit_per_minute)


# ─── Root / Health ─────────────────────────────────────────────

@app.get("/")
async def root():
    return {
        "service": "nexus-gateway",
        "version": "0.2.0",
        "engines": list(ROUTES.keys()),
        "docs": "/docs",
    }


@app.get("/health")
async def health():
    """Gateway health check."""
    return {"status": "healthy", "service": "nexus-gateway"}


@app.get("/api/v1/engines/status")
async def engine_status():
    """Check health of all backend engines."""
    statuses = {}
    for prefix, url in ROUTES.items():
        engine_name = prefix.split("/")[-1]
        try:
            resp = await _client.get(f"{url}/health", timeout=5.0)
            statuses[engine_name] = resp.json()
        except Exception as e:
            statuses[engine_name] = {"status": "unreachable", "error": str(e)}
    return statuses


# ─── Catch-all Proxy ──────────────────────────────────────────

@app.api_route(
    "/api/v1/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
)
async def proxy(request: Request, path: str):
    return await proxy_request(
        request,
        path,
        routes=ROUTES,
        config=config,
        client=_client,
        rate_limiter=_rate_limiter,
    )


# ─── Entry Point ──────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=config.gateway_port, log_level="info")
