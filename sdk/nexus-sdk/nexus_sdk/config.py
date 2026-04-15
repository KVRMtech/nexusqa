"""
Nexus Engine Configuration — Shared settings for all engines.

Each engine extends EngineConfig with its own specific settings.
All settings are loaded from environment variables (12-factor compliant).
"""

from __future__ import annotations

from pydantic_settings import BaseSettings
from pydantic import Field


class EngineConfig(BaseSettings):
    """Base configuration shared by every Nexus engine."""

    # --- Platform ---
    nexus_env: str = Field(default="production", description="Environment: production, staging, dev")
    nexus_log_level: str = Field(default="INFO", description="Log level: DEBUG, INFO, WARNING, ERROR")
    nexus_secret_key: str = Field(
        default="change-me",
        description="Platform secret key — MUST be set via NEXUS_SECRET_KEY env var in production",
    )

    # --- This Engine ---
    engine_name: str = Field(default="unnamed-engine", description="Name of this engine")
    engine_version: str = Field(default="0.1.0", description="Version of this engine")
    engine_port: int = Field(default=8000, description="Port this engine listens on")

    # --- Redis (Message Bus) ---
    redis_host: str = Field(default="localhost", description="Redis host")
    redis_port: int = Field(default=6379, description="Redis port")
    redis_password: str = Field(default="", description="Redis password")

    # --- Authentication ---
    nexus_jwt_secret: str = Field(
        default="change-me",
        description="JWT signing secret — MUST be set via NEXUS_JWT_SECRET env var in production",
    )

    # CORS policy
    cors_allowed_origins: str = Field(
        default="http://localhost:3000,http://localhost:5173,http://localhost:8080",
        description="Comma-separated allowed CORS origins. Override via CORS_ALLOWED_ORIGINS env var.",
    )
    nexus_jwt_expiry_hours: int = Field(default=24, description="JWT access token expiry in hours")
    nexus_jwt_refresh_expiry_days: int = Field(default=30, description="JWT refresh token expiry in days")

    # --- Storage ---
    nexus_storage_path: str = Field(default="/data/nexus", description="Base storage path")

    # --- Observability ---
    otel_exporter_endpoint: str = Field(
        default="http://localhost:4317", description="OpenTelemetry collector OTLP gRPC endpoint"
    )
    metrics_enabled: bool = Field(default=True, description="Enable Prometheus metrics")
    tracing_sample_rate: float = Field(
        default=1.0, description="Fraction of requests to trace (0.0-1.0)"
    )

    # --- Security ---
    max_request_body_mb: int = Field(
        default=500, description="Maximum request body size in megabytes"
    )

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


class PostgresConfig(BaseSettings):
    """PostgreSQL configuration for metadata storage."""

    postgres_host: str = Field(default="localhost")
    postgres_port: int = Field(default=5432)
    postgres_db: str = Field(default="nexus")
    postgres_user: str = Field(default="nexus")
    postgres_password: str = Field(default="change-me")

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    model_config = {"env_file": ".env", "extra": "ignore"}


class Neo4jConfig(BaseSettings):
    """Neo4j configuration for knowledge graph."""

    neo4j_uri: str = Field(default="bolt://localhost:7687")
    neo4j_user: str = Field(default="neo4j")
    neo4j_password: str = Field(default="change-me")

    model_config = {"env_file": ".env", "extra": "ignore"}


class MilvusConfig(BaseSettings):
    """Milvus configuration for vector store."""

    milvus_host: str = Field(default="localhost")
    milvus_port: int = Field(default=19530)

    model_config = {"env_file": ".env", "extra": "ignore"}


# ─── Production Guards ─────────────────────────────────────────

import os
import logging

_guard_logger = logging.getLogger("nexus.production_guard")


def production_guard(component: str, available: bool) -> None:
    """
    Block degraded/stub fallback in production environments.

    Call this after attempting to initialize a critical dependency
    (LLM, Playwright, Whisper, OCR, etc.). When ``available`` is False
    and ``NEXUS_ENV=production``, the service will refuse to start
    rather than silently serving stub responses.

    In development/staging, the warning is logged but execution continues.
    """
    if available:
        return

    nexus_env = os.getenv("NEXUS_ENV", "development").lower()

    if nexus_env == "production":
        raise RuntimeError(
            f"[NEXUS PRODUCTION GUARD] {component} failed to initialize. "
            f"Stub fallback is disabled in production (NEXUS_ENV=production). "
            f"Ensure the required dependency is installed and accessible, "
            f"or set NEXUS_ENV=development to allow degraded mode."
        )

    _guard_logger.warning(
        "production_guard.stub_allowed: %s unavailable — stub fallback active (%s)",
        component,
        nexus_env,
        extra={
            "component": component,
            "nexus_env": nexus_env,
        },
    )
