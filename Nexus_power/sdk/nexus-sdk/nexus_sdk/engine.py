"""
Nexus Engine — Base class that every engine extends.

This is the core "plug-in" interface. Every engine (Ears, Eyes, Shield, Heart,
Backbone, Nerves, Legs, Hands, Spine, Mouth) inherits from NexusEngine and gets:

- A FastAPI application with standard routes
- Health check endpoints
- Prometheus metrics
- Event bus connection
- Authentication middleware
- Plugin system integration (domain extensions loaded at startup)
- Structured logging
- Graceful startup/shutdown

Usage:
    from nexus_sdk import NexusEngine, EngineConfig

    class EarsConfig(EngineConfig):
        whisper_model: str = "large-v3"

    class EarsEngine(NexusEngine):
        def __init__(self):
            super().__init__(
                name="ears",
                version="0.1.0",
                config=EarsConfig(engine_name="ears", engine_port=8002),
            )

        def register_routes(self, app):
            @app.post("/api/v1/ears/transcribe")
            async def transcribe(...):
                ...

        async def on_startup(self):
            # Load Whisper model, etc.
            # Access domain extensions via self.plugin_registry
            vocab = self.plugin_registry.get_merged_vocabulary()
            pass

    if __name__ == "__main__":
        EarsEngine().run()
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Optional

import structlog
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import make_asgi_app as make_metrics_app

from nexus_sdk.config import EngineConfig
from nexus_sdk.health import HealthCheck
from nexus_sdk.events import EventBus
from nexus_sdk.stores import JobStore
from nexus_sdk.auth import init_auth
from nexus_sdk.plugins.registry import PluginRegistry
from nexus_sdk.observability.logging import setup_logging, get_logger as _get_obs_logger
from nexus_sdk.observability.metrics import MetricsMiddleware, init_metrics
from nexus_sdk.observability.correlation import CorrelationIdMiddleware
from nexus_sdk.observability.tracing import setup_tracing
from nexus_sdk.security.headers import SecurityHeadersMiddleware
from nexus_sdk.security.sanitization import RequestSizeLimitMiddleware
from nexus_sdk.security.rate_limit import RateLimitMiddleware

logger = structlog.get_logger()


class NexusEngine:
    """Base class for all Nexus engines."""

    def __init__(
        self,
        name: str,
        version: str,
        config: EngineConfig,
        description: str = "",
    ):
        self.name = name
        self.version = version
        self.config = config
        self.description = description

        # Health checks
        self.health = HealthCheck(engine_name=name, version=version)

        # Event bus (initialized on startup)
        self.event_bus: Optional[EventBus] = None

        # Plugin registry (singleton, shared across engines)
        self._plugin_registry = PluginRegistry.instance()

        # Redis-backed job store (replaces in-memory dict in all engines)
        self.job_store = JobStore(
            prefix=name,
            redis_host=config.redis_host,
            redis_port=config.redis_port,
            redis_password=config.redis_password,
        )

        # FastAPI app
        self.app = self._create_app()

    @property
    def plugin_registry(self) -> PluginRegistry:
        """Access the shared plugin registry for domain extensions."""
        return self._plugin_registry

    def _create_app(self) -> FastAPI:
        """Create the FastAPI application with standard middleware and routes."""

        @asynccontextmanager
        async def lifespan(app: FastAPI):
            # ── Startup ──
            logger.info(
                "engine.starting",
                engine=self.name,
                version=self.version,
                port=self.config.engine_port,
            )

            # Initialize auth
            init_auth(
                jwt_secret=self.config.nexus_jwt_secret,
                jwt_expiry_hours=self.config.nexus_jwt_expiry_hours,
            )

            # Initialize event bus
            self.event_bus = EventBus(
                redis_host=self.config.redis_host,
                redis_port=self.config.redis_port,
                redis_password=self.config.redis_password,
                consumer_group=f"nexus-{self.name}",
                consumer_name=f"{self.name}-{id(self)}",
            )
            for attempt in range(5):
                try:
                    await self.event_bus.connect()
                    self.health.add_check("redis", self._check_redis)
                    break
                except Exception as e:
                    if attempt < 4:
                        logger.warning(
                            "engine.redis_retry",
                            engine=self.name,
                            attempt=attempt + 1,
                            error=str(e),
                        )
                        await asyncio.sleep(2)
                    else:
                        logger.warning("engine.redis_unavailable", error=str(e))

            # Initialize job store (piggy-backs on same Redis)
            # Retry up to 5 times with 2s backoff to handle container startup ordering
            connected = False
            for attempt in range(5):
                connected = await self.job_store.connect()
                if connected:
                    break
                if attempt < 4:
                    logger.warning(
                        "engine.job_store_retry",
                        engine=self.name,
                        attempt=attempt + 1,
                    )
                    await asyncio.sleep(2)
            if connected:
                self.health.set_mode("job_store", "redis")
            else:
                self.health.set_mode("job_store", "in-memory")
                from nexus_sdk.config import production_guard
                production_guard(
                    f"Redis job store ({self.name})",
                    available=False,
                )

            # Load domain plugins (if configured)
            await self._load_plugins()

            # Engine-specific startup
            await self.on_startup()

            # Start event consumer in background
            if self.event_bus and self.event_bus._handlers:
                asyncio.create_task(self.event_bus.start_consuming())

            logger.info("engine.started", engine=self.name)

            yield

            # ── Shutdown ──
            logger.info("engine.stopping", engine=self.name)
            await self.on_shutdown()
            if self.event_bus:
                await self.event_bus.stop_consuming()
                await self.event_bus.disconnect()
            await self.job_store.close()
            logger.info("engine.stopped", engine=self.name)

        app = FastAPI(
            title=f"Nexus {self.name.title()} Engine",
            description=self.description or f"Nexus {self.name.title()} Engine API",
            version=self.version,
            lifespan=lifespan,
        )

        # ── Middleware stack (order matters: outermost runs first) ──

        # 1. Security headers on every response (outermost)
        app.add_middleware(
            SecurityHeadersMiddleware,
            include_hsts=self.config.nexus_env != "development",
        )

        # 2. Request body size limit (reject oversized requests early)
        app.add_middleware(
            RequestSizeLimitMiddleware,
            max_body_size=self.config.max_request_body_mb * 1024 * 1024,
        )

        # 2b. Rate limiting per client IP (sliding window)
        app.add_middleware(RateLimitMiddleware)

        # 3. Correlation ID propagation (generates/forwards X-Request-ID)
        app.add_middleware(CorrelationIdMiddleware, service_name=self.name)

        # 4. Prometheus metrics middleware (instrument all endpoints)
        if self.config.metrics_enabled:
            app.add_middleware(MetricsMiddleware, service_name=self.name)

        # 5. CORS — restricted via CORS_ALLOWED_ORIGINS env var
        origins = [
            o.strip()
            for o in self.config.cors_allowed_origins.split(",")
            if o.strip()
        ]
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=True if "*" not in origins else False,
            allow_methods=["*"],
            allow_headers=["*"],
        )

        # Health check routes
        app.include_router(self.health.router)

        # Prometheus metrics endpoint
        if self.config.metrics_enabled:
            metrics_app = make_metrics_app()
            app.mount("/metrics", metrics_app)

        # Engine-specific routes
        self.register_routes(app)

        # Root info endpoint
        @app.get("/")
        async def root():
            return {
                "engine": self.name,
                "version": self.version,
                "status": "running",
                "plugins": [
                    {"id": m.plugin_id, "domain": m.domain, "version": m.version}
                    for m in self._plugin_registry.list_plugins()
                ],
                "docs": f"http://localhost:{self.config.engine_port}/docs",
            }

        return app

    # ─── Plugin Loading ───────────────────────────────────────────

    async def _load_plugins(self) -> None:
        """
        Load domain plugins from configured module paths.

        Reads NEXUS_PLUGIN_MODULES env var (comma-separated Python module paths)
        and loads each one. Plugins are loaded into the shared PluginRegistry.

        Example env var:
            NEXUS_PLUGIN_MODULES=products.nexus_qa.plugin

        Engines then access domain extensions via self.plugin_registry:
            vocab = self.plugin_registry.get_merged_vocabulary()
            pii = self.plugin_registry.get_merged_pii()
        """
        import os

        plugin_modules = os.environ.get("NEXUS_PLUGIN_MODULES", "")
        if not plugin_modules.strip():
            logger.debug("engine.no_plugins_configured", engine=self.name)
            return

        for module_path in plugin_modules.split(","):
            module_path = module_path.strip()
            if not module_path:
                continue
            try:
                plugin = self._plugin_registry.load_from_module(module_path)
                logger.info(
                    "engine.plugin_loaded",
                    engine=self.name,
                    plugin_id=plugin.plugin_id,
                    domain=plugin.domain,
                )
            except Exception as exc:
                logger.error(
                    "engine.plugin_load_failed",
                    engine=self.name,
                    module=module_path,
                    error=str(exc),
                )

    # ─── Override these in each engine ────────────────────────────

    def register_routes(self, app: FastAPI) -> None:
        """
        Override this to register engine-specific API routes.
        
        Example:
            def register_routes(self, app):
                @app.post("/api/v1/ears/transcribe")
                async def transcribe(request: TranscribeRequest):
                    ...
        """
        pass

    async def on_startup(self) -> None:
        """
        Override this for engine-specific startup logic.
        
        Examples: Load ML models, connect to databases, warm up caches.
        """
        pass

    async def on_shutdown(self) -> None:
        """
        Override this for engine-specific shutdown logic.
        
        Examples: Release GPU memory, close DB connections.
        """
        pass

    # ─── Health check helpers ─────────────────────────────────────

    async def _check_redis(self):
        """Check Redis connection."""
        if self.event_bus and self.event_bus._client:
            await self.event_bus._client.ping()
            return "connected"
        return "not connected"

    # ─── Run the engine ───────────────────────────────────────────

    def run(self) -> None:
        """Start the engine (blocking)."""
        # ── Structured logging (JSON in production, colored console in dev) ──
        setup_logging(
            service_name=self.name,
            log_level=self.config.nexus_log_level,
            environment=self.config.nexus_env,
        )

        # ── Prometheus custom metrics ──
        if self.config.metrics_enabled:
            metrics = init_metrics(self.name)
            metrics.set_service_info(
                version=self.version,
                environment=self.config.nexus_env,
            )

        # ── OpenTelemetry distributed tracing ──
        setup_tracing(
            service_name=self.name,
            service_version=self.version,
            otlp_endpoint=self.config.otel_exporter_endpoint,
            enabled=self.config.nexus_env != "test",
            environment=self.config.nexus_env,
            app=self.app,
        )

        uvicorn.run(
            self.app,
            host="0.0.0.0",
            port=self.config.engine_port,
            log_level=self.config.nexus_log_level.lower(),
        )
