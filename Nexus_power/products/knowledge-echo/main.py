"""Knowledge Echo product service — entrypoint.

Wires Phase 2 components together and exposes:

    POST /webhook/slack/events         — Slack Events API
    POST /webhook/slack/interactions   — Slack block-actions
    POST /api/v1/echo/simulate         — admin / integration test path
    GET  /api/v1/echo/dispatches       — recent decisions
    GET  /health                       — readiness + dependency status

Dependencies are gated by the same env var that Phase 0 introduced —
``NEXUS_KNOWLEDGE_FOUNDATION_ENABLED``. When false, the service still
boots but the orchestrator + feature-flag service are absent, so all
HTTP routes 503.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import uuid
from contextlib import asynccontextmanager
from typing import Any, Optional

import structlog
from fastapi import FastAPI

# ─── Path setup for SDK imports ────────────────────────────────
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_SDK_PATH = os.path.normpath(
    os.path.join(_THIS_DIR, "..", "..", "sdk", "nexus-sdk")
)
for p in (_THIS_DIR, _SDK_PATH):
    if p not in sys.path:
        sys.path.insert(0, p)

from nexus_sdk.auth import init_auth  # noqa: E402
from nexus_sdk.feature_flags import FeatureFlagService  # noqa: E402
from nexus_sdk.security.envelope import (  # noqa: E402
    EnvelopeService,
    LocalKekProvider,
    ProviderUnavailable,
)

from app.backbone_client import BackboneSearchClient  # noqa: E402
from app.classifier import QuestionClassifier  # noqa: E402
from app.config import EchoConfig, load_config  # noqa: E402
from app.db import Database  # noqa: E402
from app.dispatches import DispatchRepository  # noqa: E402
from app.llm import OllamaJsonClient  # noqa: E402
from app.matcher import Matcher  # noqa: E402
from app.orchestrator import EchoOrchestrator  # noqa: E402
from app.routes.admin import router as admin_router  # noqa: E402
from app.routes.slack import router as slack_router  # noqa: E402
from app.slack import (  # noqa: E402
    EchoCardComposer,
    SlackClient,
    SlackInstallationLoader,
)

logger = structlog.get_logger()


# ─── Redis client + null fallback ──────────────────────────────


class _NullRedis:
    """Same shape as the feature_flag service's null fallback."""

    async def get(self, key: str):  # noqa: ARG002
        return None

    async def setex(self, key: str, ttl: int, value):  # noqa: ARG002
        return None

    async def delete(self, *keys):  # noqa: ARG002
        return 0

    async def aclose(self):
        return None


async def _make_redis(config: EchoConfig):
    try:
        import redis.asyncio as aioredis
    except ImportError:
        logger.warning("echo.redis_package_missing — running without cache")
        return _NullRedis(), False
    try:
        client = aioredis.Redis(
            host=config.redis_host,
            port=config.redis_port,
            password=config.redis_password or None,
            db=config.redis_db,
            decode_responses=False,
            socket_connect_timeout=2.0,
            socket_timeout=2.0,
        )
        await client.ping()
        return client, True
    except Exception as exc:
        logger.warning("echo.redis_unavailable: %s", exc)
        return _NullRedis(), False


def _make_envelope() -> EnvelopeService:
    """Echo only needs to *decrypt* tenant credentials. The provider
    must match what platform-api used to *encrypt* them.
    """
    provider = os.environ.get("NEXUS_KEK_PROVIDER", "local").lower()
    if provider == "aws_kms":
        try:
            from nexus_sdk.security.envelope import AwsKmsProvider
        except ImportError as exc:
            raise ProviderUnavailable(str(exc)) from exc
        arn = os.environ.get("NEXUS_KEK_AWS_ARN")
        if not arn:
            raise ProviderUnavailable(
                "NEXUS_KEK_PROVIDER=aws_kms requires NEXUS_KEK_AWS_ARN"
            )

        async def _resolver(_tenant_id: str) -> str:
            return arn

        region = os.environ.get("AWS_REGION") or os.environ.get(
            "AWS_DEFAULT_REGION"
        )
        return EnvelopeService(
            AwsKmsProvider(kek_resolver=_resolver, region=region)
        )
    if provider == "local":
        from pathlib import Path

        master_key_path = os.environ.get(
            "NEXUS_LOCAL_KEK_PATH",
            str(Path.home() / ".nexus" / "kek" / "master.key"),
        )
        return EnvelopeService(LocalKekProvider(master_key_path))
    raise ProviderUnavailable(f"unsupported KEK provider: {provider!r}")


# ─── Lifespan ──────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(application: FastAPI):
    config = load_config()
    application.state.echo_config = config
    init_auth(jwt_secret=config.jwt_secret)

    application.state.feature_flags = None
    application.state.orchestrator = None
    application.state.slack_installs = None
    application.state.dispatch_repo = None
    application.state.feature_flags_redis = None

    foundation_enabled = (
        os.environ.get(
            "NEXUS_KNOWLEDGE_FOUNDATION_ENABLED", "false"
        ).lower()
        in {"1", "true", "yes", "on"}
    )
    if not foundation_enabled:
        logger.info("echo.foundation_disabled — running in inert mode")
        yield
        return

    db = Database(config.postgres_url)
    await db.connect()
    application.state.db = db

    redis, _ = await _make_redis(config)
    application.state.feature_flags_redis = redis

    flags = FeatureFlagService(
        session_factory=db.factory(),
        redis=redis,
    )
    application.state.feature_flags = flags

    envelope = _make_envelope()
    install_loader = SlackInstallationLoader(db, envelope)
    application.state.slack_installs = install_loader

    llm = OllamaJsonClient(
        base_url=config.ollama_base_url,
        timeout_seconds=config.classifier_timeout_seconds,
    )
    application.state.llm = llm

    classifier = QuestionClassifier(
        llm=llm,
        model=config.classifier_model,
        cache=redis,
        cache_ttl_seconds=config.classifier_cache_ttl_seconds,
    )

    backbone = BackboneSearchClient(
        base_url=config.backbone_url,
        jwt_secret=config.jwt_secret,
        jwt_algorithm=config.jwt_algorithm,
        service_user_id=config.service_account_user_id,
        service_role=config.service_account_role,
        token_ttl_seconds=config.service_account_token_ttl_seconds,
        timeout_seconds=config.http_timeout_seconds,
    )
    application.state.backbone = backbone

    matcher = Matcher(
        backbone,
        high_threshold=config.min_confidence_high,
        medium_threshold=config.min_confidence_medium,
    )

    slack = SlackClient(timeout_seconds=config.http_timeout_seconds)
    application.state.slack_client = slack

    composer = EchoCardComposer()
    repo = DispatchRepository(db)
    application.state.dispatch_repo = repo

    orchestrator = EchoOrchestrator(
        feature_flags=flags,
        feature_key=config.feature_key,
        classifier=classifier,
        matcher=matcher,
        composer=composer,
        slack=slack,
        installs=install_loader,
        dispatches=repo,
        dedup_window_seconds=config.dedup_window_seconds,
        max_match_candidates=config.max_match_candidates,
        min_confidence_high=config.min_confidence_high,
        min_confidence_medium=config.min_confidence_medium,
        end_to_end_timeout_seconds=config.end_to_end_timeout_seconds,
    )
    application.state.orchestrator = orchestrator

    logger.info(
        "echo.started",
        port=config.port,
        backbone=config.backbone_url,
        ollama=config.ollama_base_url,
        kek_provider=envelope.provider_id,
    )

    try:
        yield
    finally:
        # Shutdown in reverse dependency order.
        try:
            await slack.aclose()
        except Exception:
            pass
        try:
            await backbone.aclose()
        except Exception:
            pass
        try:
            await llm.aclose()
        except Exception:
            pass
        try:
            await redis.aclose()
        except Exception:
            pass
        try:
            await db.disconnect()
        except Exception:
            pass
        logger.info("echo.stopped")


# ─── Application ───────────────────────────────────────────────


app = FastAPI(
    title="Nexus Knowledge Echo",
    description="Phase 2 — Slack inbound, classify, match, compose, dispatch.",
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(slack_router)
app.include_router(admin_router)


@app.get("/health")
async def health() -> dict[str, Any]:
    cfg = getattr(app.state, "echo_config", None)
    db = getattr(app.state, "db", None)
    db_status = await db.health() if db is not None else "uninitialised"
    backbone = getattr(app.state, "backbone", None)
    backbone_status = (
        await backbone.health() if backbone is not None else "uninitialised"
    )
    llm = getattr(app.state, "llm", None)
    llm_status = await llm.health() if llm is not None else "uninitialised"
    healthy = (
        db_status == "healthy"
        and backbone_status == "healthy"
        and getattr(app.state, "orchestrator", None) is not None
    )
    return {
        "status": "healthy" if healthy else "degraded",
        "service": "knowledge-echo",
        "version": cfg.service_version if cfg else "unknown",
        "database": db_status,
        "backbone": backbone_status,
        "ollama": llm_status,
        "orchestrator": "ready"
        if getattr(app.state, "orchestrator", None) is not None
        else "absent",
    }


if __name__ == "__main__":
    import uvicorn

    cfg = load_config()
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    uvicorn.run("main:app", host=cfg.host, port=cfg.port, log_level="info")
