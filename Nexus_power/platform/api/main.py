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
from app.routers.llm import router as llm_router

# QI Engineer Portal (Phase 7)
from app.routers.personas import router as personas_router
from app.routers.missions import router as missions_router

# Test Architect
from app.routers.test_strategy import router as test_strategy_router

# E2E Architect
from app.routers.e2e_architect import router as e2e_architect_router

# E2E Scenario Lifecycle (P3)
from app.routers.e2e_lifecycle import router as e2e_lifecycle_router

# E2E Co-Architect chat (P4)
from app.routers.co_architect import router as co_architect_router

# E2E Execution Feedback (P6)
from app.routers.test_runs_feedback import router as test_runs_feedback_router

# E2E Demo Diff + Self-Healing (P7)
from app.routers.diff_and_heal import router as diff_and_heal_router

# Canonical artifacts & workflow read model
from app.routers.artifacts import router as artifacts_router

# Phase 0 — Knowledge Foundation (feature-flag gated; see knowledge_foundation.py)
from app.routers.feature_flags import router as feature_flags_router
from app.routers.integrations import router as integrations_router
from app.routers.products import router as products_router
from app.routers.knowledge_cards import router as knowledge_cards_router
from app.routers.atlas import router as atlas_router
from app.routers.scim import router as scim_router
from app.routers.org_awareness import router as org_awareness_router
from app.routers.actions import router as actions_router
from app.routers.marketplace import router as marketplace_router
from app.knowledge_foundation import (
    init_knowledge_foundation,
    is_enabled as knowledge_foundation_enabled,
    shutdown_knowledge_foundation,
)

# Storyboard Phase 1 — picture-first visual evidence + LLM-driven captions.
# All derivations are additive to the frozen pipeline outputs.
from app.routers.storyboard import router as storyboard_router
from app.services.storyboard import load_config as load_storyboard_config
# Test Factory Phase 1 — demonstrated functional E2E from Pages & Forms.
# Additive: reads frozen evidence, writes only factory_test_cases.
from app.routers.test_factory import router as test_factory_router
from app.routers.preflight import router as preflight_router
# Lights-Out (Mode C) — schedule CRUD + reports (additive; gated background runner).
# Optional router: degrade gracefully if the module isn't present in this build.
try:
    from app.routers.scheduler import router as scheduler_router
except ImportError:
    scheduler_router = None
from app.services.storyboard.composer import StoryboardComposer
from app.services.storyboard.frame_annotator import FrameAnnotator
from app.services.llm import load_config as load_llm_config, LLMRouter

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

    # Phase 0 — Knowledge Foundation (idempotent no-op when env disabled)
    foundation_ready = await init_knowledge_foundation(
        application,
        session_factory=sf,
        config=config,
    )

    # ── Storyboard Phase 1 — picture-first visual evidence ─────────
    # Initialised even when no LLM is configured; the caption rewriter
    # falls back to deterministic captions in that case.  Pillow is
    # optional — when missing, the frame annotator is disabled but
    # storyboard responses still work (raw frame paths returned).
    storyboard_config = load_storyboard_config()
    llm_router = LLMRouter(load_llm_config())
    application.state.storyboard_config = storyboard_config
    application.state.llm_router = llm_router

    frame_annotator: FrameAnnotator | None = None
    try:
        from nexus_sdk.storage import create_storage
        from nexus_sdk.storage.artifact_store import ArtifactStore

        artifact_store = ArtifactStore(create_storage())
        # eyes_base_url defaults to the docker-network hostname so the
        # annotator can fetch raw frames via the same HTTP route the
        # browser uses, regardless of the eyes storage backend.
        eyes_base_url = os.environ.get(
            "NEXUS_EYES_ENGINE_URL",
            os.environ.get("EYES_ENGINE_URL", "http://nexus-eyes:8003"),
        )
        frame_annotator = FrameAnnotator(
            artifact_store=artifact_store,
            config=storyboard_config.frame_annotator,
            eyes_base_url=eyes_base_url,
        )
    except Exception as exc:  # pragma: no cover - storage init failure
        logger.warning(
            "platform_api.frame_annotator_init_failed",
            error=str(exc)[:200],
        )

    application.state.frame_annotator = frame_annotator
    application.state.storyboard_composer = StoryboardComposer(
        config=storyboard_config,
        llm_router=llm_router,
        frame_annotator=frame_annotator,
    )

    logger.info(
        "platform_api.started",
        port=config.port,
        db=is_db_connected(),
        cache=_cache.is_connected,
        knowledge_foundation=foundation_ready,
        llm_tiers=list(llm_router.config.tiers.keys()),
        frame_annotator_ready=(
            frame_annotator is not None and frame_annotator.pil_available
        ),
    )

    # Lights-Out (Mode C): start the background scheduler. GATED by NEXUS_SCHEDULER_ENABLED
    # (default off) and FAIL-OPEN — a pure no-op unless explicitly enabled, so it can never
    # affect a deployment that does not opt in.
    try:
        from app.services.scheduler.scheduler import start_scheduler
        start_scheduler()
    except Exception as _sched_exc:  # never block startup
        logger.warning("scheduler.start_skipped", error=str(_sched_exc)[:160])

    # SENTINEL AUTONOMY (lifespan-hosted; @app.on_event is IGNORED when a
    # lifespan= handler is set, so the daemon must start here).
    try:
        import asyncio as _aio
        from app.services.test_factory.qe_agents import sentinel_daemon
        _aio.create_task(sentinel_daemon())
    except Exception:
        import logging as _lg
        _lg.getLogger(__name__).warning("sentinel.daemon_start_failed")

    yield

    await shutdown_knowledge_foundation(application)
    if hasattr(application.state, "llm_router") and application.state.llm_router:
        await application.state.llm_router.close()
    if hasattr(application.state, "frame_annotator") and application.state.frame_annotator:
        await application.state.frame_annotator.close()
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
app.include_router(llm_router)
app.include_router(test_cases_router)
app.include_router(personas_router)
app.include_router(missions_router)
app.include_router(artifacts_router)
app.include_router(test_strategy_router)
app.include_router(e2e_architect_router)
app.include_router(e2e_lifecycle_router)
app.include_router(co_architect_router)
app.include_router(test_runs_feedback_router)
app.include_router(diff_and_heal_router)

# Phase 0/1 — Knowledge Foundation routers. Unconditionally registered so
# their paths exist; each endpoint returns 503 when the subsystem isn't
# initialised (NEXUS_KNOWLEDGE_FOUNDATION_ENABLED=false).
app.include_router(feature_flags_router)
app.include_router(integrations_router)
app.include_router(products_router)
app.include_router(knowledge_cards_router)
app.include_router(atlas_router)
app.include_router(scim_router)
app.include_router(org_awareness_router)
app.include_router(actions_router)
app.include_router(marketplace_router)

# Storyboard Phase 1 — picture-first visual evidence
app.include_router(storyboard_router)
app.include_router(test_factory_router)
app.include_router(preflight_router)


@app.on_event("startup")
async def _start_sentinel_daemon() -> None:
    # SENTINEL AUTONOMY: scheduled watcher (env NEXUS_SENTINEL_INTERVAL_MIN,
    # default 60; 0 disables). Fully guarded — can never affect requests.
    try:
        import asyncio as _aio
        from app.services.test_factory.qe_agents import sentinel_daemon
        _aio.create_task(sentinel_daemon())
    except Exception:  # pragma: no cover
        import logging as _lg
        _lg.getLogger(__name__).warning("sentinel.daemon_start_failed")
if scheduler_router is not None:  # Lights-Out (Mode C): schedules + reports + run-now
    app.include_router(scheduler_router)


# ─── Health ───────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "service": "nexus-platform-api",
        "database": "connected" if is_db_connected() else "disconnected",
        "cache": "connected" if _cache.is_connected else "disabled",
        "knowledge_foundation": (
            "enabled"
            if knowledge_foundation_enabled() and app.state.feature_flags is not None
            else "disabled"
        ),
    }


# ─── Entry Point ──────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=config.host, port=config.port)
