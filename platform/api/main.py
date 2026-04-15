"""
Nexus Platform API — Cross-cutting backend for all UI module pages.

v0.3.0 — Modular refactor:
  app.config       → PlatformAPIConfig
  app.database     → Async SQLAlchemy engine, session factory, helpers
  app.auth         → JWT validation dependency & middleware
  app.middleware   → Security headers
  app.routers.*    → Domain-specific route modules

ALL data is persisted in PostgreSQL via async SQLAlchemy.
NO in-memory stores. NO hardcoded fake data.
"""

from __future__ import annotations

import os
import sys
import logging
from contextlib import asynccontextmanager

import httpx
import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# ─── Path Setup ────────────────────────────────────────────────
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_SDK_PATH = os.path.join(_THIS_DIR, "..", "..", "sdk", "nexus-sdk")
for p in [_THIS_DIR, _SDK_PATH]:
    if p not in sys.path:
        sys.path.insert(0, p)

# ─── Modular sub-packages ─────────────────────────────────────
from app.config import PlatformAPIConfig
from app.database import init_db, close_db, get_session_factory, is_db_connected
from app.auth import jwt_auth_middleware, get_current_user, PUBLIC_PATHS
from app.middleware import security_headers_middleware

# Route modules
from app.routers.sessions import router as sessions_router
from app.routers.sme import router as sme_router
from app.routers.contradictions import router as contradictions_router
from app.routers.guardrails import router as guardrails_router
from app.routers.traceability import router as traceability_router
from app.routers.tests import router as tests_router
from app.routers.data_forge import router as data_forge_router
from app.routers.compliance import router as compliance_router
from app.routers.insights import router as insights_router, init_insights
from app.routers.admin import router as admin_router, init_admin

# QI Engineer Portal (Phase 7)
from app.routers.personas import router as personas_router
from app.routers.missions import router as missions_router

# Test Architect
from app.routers.test_strategy import router as test_strategy_router

# E2E Architect
from app.routers.e2e_architect import router as e2e_architect_router

# Canonical artifacts & workflow read model
from app.routers.artifacts import router as artifacts_router

# Existing test_cases router (already modular)
from routers.test_cases import router as test_cases_router, init_router as init_test_cases

# Redis cache
from cache import RedisCache

# ── Backward-compat re-exports so existing imports from main still work ──
from app.config import PlatformAPIConfig  # noqa: F811
from app.database import (  # noqa: F811
    require_db,
    new_id,
    utc_now,
    row_to_dict,
)

logger = structlog.get_logger()

# ─── Configuration ─────────────────────────────────────────────
config = PlatformAPIConfig()

# ─── Redis Cache ───────────────────────────────────────────────
_cache = RedisCache(
    host=config.redis_host,
    port=config.redis_port,
    password=config.redis_password,
)

# ─── HTTP client for engine health checks ─────────────────────
_http = httpx.AsyncClient(timeout=30.0)


# ─── Lifespan ──────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(application: FastAPI):
    await init_db(config)
    await _cache.connect()

    # Initialize test-cases router with DB session factory
    sf = get_session_factory()
    if sf:
        init_test_cases(sf)

    # Wire insights + admin routers with shared HTTP client, cache, config
    init_insights(_http, _cache, config)
    init_admin(_http, _cache, config)

    logger.info(
        "platform_api.started",
        routes=36,
        port=config.port,
        db=is_db_connected(),
        cache=_cache.is_connected,
    )
    yield

    await _cache.close()
    await _http.aclose()
    await close_db()
    logger.info("platform_api.stopped")


# ─── Application ───────────────────────────────────────────────

app = FastAPI(
    title="Nexus Platform API",
    description="Cross-cutting API — PostgreSQL-backed, zero hardcoded data",
    version="0.3.0",
    lifespan=lifespan,
)

# Store config in app state for auth middleware access
app.state.config = config

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        o.strip()
        for o in os.getenv(
            "CORS_ALLOWED_ORIGINS",
            "http://localhost:3000,http://localhost:5173,http://localhost:8080",
        ).split(",")
        if o.strip()
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── Middleware (order matters — outermost runs first) ─────────
app.middleware("http")(security_headers_middleware)
app.middleware("http")(jwt_auth_middleware)


# ─── Register routers ─────────────────────────────────────────
app.include_router(sessions_router)
app.include_router(sme_router)
app.include_router(contradictions_router)
app.include_router(guardrails_router)
app.include_router(traceability_router)
app.include_router(tests_router)
app.include_router(data_forge_router)
app.include_router(compliance_router)
app.include_router(insights_router)
app.include_router(admin_router)
app.include_router(test_cases_router)
app.include_router(personas_router)
app.include_router(missions_router)
app.include_router(artifacts_router)
app.include_router(test_strategy_router)
app.include_router(e2e_architect_router)


# ─── Health ───────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "service": "nexus-platform-api",
        "routes": 36,
        "database": "connected" if is_db_connected() else "disconnected",
        "cache": "connected" if _cache.is_connected else "disabled",
    }


# ─── Entry Point ──────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=config.host, port=config.port)
