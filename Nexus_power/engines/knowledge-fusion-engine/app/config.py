"""Configuration for the Knowledge Fusion Engine.

Reads from environment variables only — no hardcoded production
endpoints. Every default is dev-safe; production values are injected
by the deployment platform.
"""

from __future__ import annotations

from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class FusionConfig(BaseSettings):
    """Runtime configuration for the fusion engine.

    Field order matches the dependency layers — service identity,
    persistence, event bus, downstream engines, processing tunables.
    """

    model_config = SettingsConfigDict(
        env_prefix="FUSION_",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Service identity ─────────────────────────────────────────
    engine_name: str = "knowledge-fusion"
    engine_version: str = "1.0.0"
    host: str = "0.0.0.0"
    port: int = 8020

    # ── Postgres (canonical_artifacts + transcript_segments) ────
    postgres_host: str = Field(default="localhost", alias="POSTGRES_HOST")
    postgres_port: int = Field(default=5432, alias="POSTGRES_PORT")
    postgres_db: str = Field(default="nexus", alias="POSTGRES_DB")
    postgres_user: str = Field(default="nexus", alias="POSTGRES_USER")
    postgres_password: str = Field(default="nexus-dev", alias="POSTGRES_PASSWORD")

    @property
    def postgres_url(self) -> str:
        return (
            "postgresql+asyncpg://"
            f"{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    # ── Redis (event bus + worker coordination) ─────────────────
    redis_host: str = Field(default="localhost", alias="REDIS_HOST")
    redis_port: int = Field(default=6379, alias="REDIS_PORT")
    redis_password: Optional[str] = Field(default=None, alias="REDIS_PASSWORD")
    redis_db: int = 0

    # ── Auth (JWT for outbound calls to Backbone/Spine) ─────────
    jwt_secret: str = Field(default="test-secret-do-not-use-in-production", alias="JWT_SECRET")
    jwt_algorithm: str = "HS256"
    service_account_user_id: str = "service:knowledge-fusion"
    service_account_role: str = "api"
    service_account_token_ttl_seconds: int = 3600

    # ── Downstream engines ──────────────────────────────────────
    backbone_url: str = Field(
        default="http://localhost:8005", alias="BACKBONE_ENGINE_URL"
    )
    shield_url: str = Field(
        default="http://localhost:8001", alias="SHIELD_ENGINE_URL"
    )
    http_timeout_seconds: float = 30.0

    # ── Worker ──────────────────────────────────────────────────
    worker_concurrency: int = 4
    worker_poll_interval_seconds: float = 2.0
    worker_lease_seconds: int = 600
    worker_backoff_base_seconds: int = 30
    worker_backoff_max_seconds: int = 1800
    worker_id: Optional[str] = None  # auto-generated if not set

    # ── Chunker tunables ────────────────────────────────────────
    chunk_target_chars: int = 1600
    chunk_min_chars: int = 200
    chunk_overlap_chars: int = 200
    max_segments_per_artifact: int = 5000

    # ── Feature flag gate ───────────────────────────────────────
    feature_key: str = "knowledge_substrate"


def load_config() -> FusionConfig:
    return FusionConfig()
