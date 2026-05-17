"""Phase 0 wiring — feature flag service, envelope encryption, and the
integration catalog.

This module owns the *initialisation* of the new Phase 0 dependencies,
including its own Redis client so the wiring required in
``platform/api/main.py`` is a single function call.

The whole subsystem is gated by a single environment variable:

    NEXUS_KNOWLEDGE_FOUNDATION_ENABLED  (default: false)

When false, ``init_knowledge_foundation`` is a no-op: the routers
registered in ``platform/api/main.py`` will return 503 on every call
because the required objects are absent from ``app.state``. This is
the global kill-switch — flip the env var, restart the API service,
the whole feature path is inert.

When true, the function instantiates:

    * ``FeatureFlagService``        — at ``app.state.feature_flags``
    * ``EnvelopeService``           — at ``app.state.envelope_service``
    * ``integration_catalog``       — at ``app.state.integration_catalog``

Plus an internal Redis connection (db=2 by convention, separate from
the platform-API response cache at db=1) released on shutdown.

Nothing in this module mutates database schema; all DDL lives in
alembic migration ``019_knowledge_foundation``.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional

from nexus_sdk.feature_flags import FeatureFlagService
from nexus_sdk.feature_flags.service import ServiceConfig as FFConfig
from nexus_sdk.integrations import (
    IntegrationManifest,
    ManifestError,
    load_manifest,
)
from nexus_sdk.security.envelope import (
    AwsKmsProvider,
    EnvelopeService,
    KekProvider,
    LocalKekProvider,
    ProviderUnavailable,
)

logger = logging.getLogger(__name__)


_ENABLE_ENV = "NEXUS_KNOWLEDGE_FOUNDATION_ENABLED"
_REDIS_DB = int(os.environ.get("NEXUS_FF_REDIS_DB", "2"))


def is_enabled() -> bool:
    return os.environ.get(_ENABLE_ENV, "false").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _kek_provider() -> KekProvider:
    """Choose the KEK provider for the current deployment."""
    provider = os.environ.get("NEXUS_KEK_PROVIDER", "local").lower()
    if provider == "aws_kms":
        # Resolver must be supplied by the deployment — at Phase 0 the
        # registry comes from tenant_keks; for now we accept a single
        # ARN env var as a bootstrap. Real per-tenant resolution lands
        # in the dedicated KEK resolver service.
        single_arn = os.environ.get("NEXUS_KEK_AWS_ARN")
        if not single_arn:
            raise ProviderUnavailable(
                "NEXUS_KEK_PROVIDER=aws_kms requires NEXUS_KEK_AWS_ARN until "
                "the per-tenant resolver service is wired in Phase 1."
            )

        async def _resolver(_tenant_id: str) -> str:
            return single_arn

        region = os.environ.get("AWS_REGION") or os.environ.get(
            "AWS_DEFAULT_REGION"
        )
        return AwsKmsProvider(kek_resolver=_resolver, region=region)

    if provider == "local":
        master_key_path = os.environ.get(
            "NEXUS_LOCAL_KEK_PATH",
            str(Path.home() / ".nexus" / "kek" / "master.key"),
        )
        return LocalKekProvider(master_key_path=master_key_path)

    raise ProviderUnavailable(
        f"unsupported NEXUS_KEK_PROVIDER value: {provider!r}"
    )


def _load_catalog() -> dict[str, IntegrationManifest]:
    """Discover and validate manifests under ``plugins/``."""
    root_env = os.environ.get("NEXUS_INTEGRATION_PLUGINS_DIR")
    if root_env:
        roots = [Path(root_env)]
    else:
        # Default convention: <repo>/plugins
        # This module sits at platform/api/app/knowledge_foundation.py;
        # repo root is four levels up.
        here = Path(__file__).resolve()
        repo_root = here.parents[3]
        roots = [repo_root / "plugins"]

    catalog: dict[str, IntegrationManifest] = {}
    for root in roots:
        if not root.exists() or not root.is_dir():
            logger.info(
                "knowledge_foundation.catalog_dir_missing",
                extra={"dir": str(root)},
            )
            continue
        for entry in sorted(root.iterdir()):
            if not entry.is_dir() or entry.name.startswith("_"):
                continue
            for candidate in ("plugin.yaml", "plugin.yml", "plugin.json"):
                manifest_path = entry / candidate
                if not manifest_path.exists():
                    continue
                try:
                    manifest = load_manifest(manifest_path)
                except ManifestError as exc:
                    logger.error(
                        "knowledge_foundation.manifest_invalid",
                        extra={
                            "path": str(manifest_path),
                            "error": str(exc),
                        },
                    )
                    continue
                if manifest.id in catalog:
                    logger.error(
                        "knowledge_foundation.manifest_duplicate",
                        extra={
                            "integration_id": manifest.id,
                            "path": str(manifest_path),
                        },
                    )
                    continue
                catalog[manifest.id] = manifest
                logger.info(
                    "knowledge_foundation.manifest_loaded",
                    extra={
                        "integration_id": manifest.id,
                        "version": manifest.version,
                        "path": str(manifest_path),
                    },
                )
                break
    return catalog


async def _make_redis_client(config: Any) -> Optional[Any]:
    """Spin up a dedicated Redis client for the feature flag service.

    Uses ``db=_REDIS_DB`` so it doesn't share keyspace with the platform
    response cache. Returns None if Redis isn't reachable; downstream
    components handle this gracefully (cache misses fall through to DB).
    """
    try:
        import redis.asyncio as aioredis
    except ImportError:
        logger.warning(
            "knowledge_foundation.redis_package_missing — feature flag cache disabled"
        )
        return None
    try:
        client = aioredis.Redis(
            host=getattr(config, "redis_host", "localhost"),
            port=getattr(config, "redis_port", 6379),
            password=getattr(config, "redis_password", "") or None,
            db=_REDIS_DB,
            decode_responses=False,
            socket_connect_timeout=2.0,
            socket_timeout=2.0,
        )
        await client.ping()
        logger.info(
            "knowledge_foundation.redis_connected",
            extra={
                "host": getattr(config, "redis_host", "localhost"),
                "db": _REDIS_DB,
            },
        )
        return client
    except Exception as exc:
        logger.warning(
            "knowledge_foundation.redis_unavailable: %s", exc
        )
        return None


async def init_knowledge_foundation(
    app,
    *,
    session_factory,
    config: Any,
    feature_flag_config: Optional[FFConfig] = None,
) -> bool:
    """Bind Phase 0 services to ``app.state``.

    Returns True when the subsystem is initialised; False when the env
    flag is off (or initialisation failed and the subsystem is left
    intentionally absent, so dependent routers 503).
    """
    # Always ensure the attributes exist so router lookups don't AttributeError.
    app.state.feature_flags = None
    app.state.envelope_service = None
    app.state.integration_catalog = {}
    app.state.feature_flags_redis = None

    if not is_enabled():
        logger.info(
            "knowledge_foundation.disabled",
            extra={"env": _ENABLE_ENV},
        )
        return False

    try:
        provider = _kek_provider()
        envelope = EnvelopeService(provider)
    except ProviderUnavailable as exc:
        logger.error("knowledge_foundation.envelope_init_failed: %s", exc)
        return False

    if session_factory is None:
        logger.error(
            "knowledge_foundation.no_session_factory; refusing to init"
        )
        app.state.envelope_service = envelope
        return False

    redis_client = await _make_redis_client(config)
    if redis_client is None:
        # Provide a no-op client so the service falls through to DB on every
        # read but still operates correctly. The class methods on this stand-in
        # match the _RedisLike protocol used by FeatureFlagService.
        redis_client = _NullRedis()

    feature_flags = FeatureFlagService(
        session_factory=session_factory,
        redis=redis_client,
        config=feature_flag_config,
    )

    catalog = _load_catalog()

    app.state.feature_flags = feature_flags
    app.state.envelope_service = envelope
    app.state.integration_catalog = catalog
    app.state.feature_flags_redis = (
        redis_client if not isinstance(redis_client, _NullRedis) else None
    )

    logger.info(
        "knowledge_foundation.ready",
        extra={
            "kek_provider": envelope.provider_id,
            "catalog_size": len(catalog),
            "redis_cache": app.state.feature_flags_redis is not None,
        },
    )
    return True


async def shutdown_knowledge_foundation(app) -> None:
    """Release resources held by the subsystem."""
    redis_client = getattr(app.state, "feature_flags_redis", None)
    if redis_client is not None:
        try:
            await redis_client.aclose()
        except Exception as exc:
            logger.warning(
                "knowledge_foundation.redis_close_failed: %s", exc
            )
    app.state.feature_flags = None
    app.state.envelope_service = None
    app.state.integration_catalog = {}
    app.state.feature_flags_redis = None


class _NullRedis:
    """No-op stand-in used when Redis is unreachable at startup.

    Implements the minimal protocol consumed by FeatureFlagService so
    that cache reads return ``None`` (forcing DB fall-through) and
    writes are silent. This keeps the feature path live in degraded
    environments without burdening every caller with try/except.
    """

    async def get(self, key: str):  # noqa: D401, ARG002
        return None

    async def setex(self, key: str, ttl: int, value):  # noqa: ARG002
        return None

    async def delete(self, *keys: str):  # noqa: ARG002
        return 0

    async def aclose(self):
        return None
