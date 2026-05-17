"""Knowledge Echo configuration.

Every default is dev-safe; production values are injected by the
deployment platform. ``FUSION_*`` and ``ECHO_*`` env-prefixes are
distinct so the two services can coexist on a single host.
"""

from __future__ import annotations

from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class EchoConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ECHO_",
        case_sensitive=False,
        extra="ignore",
    )

    service_name: str = "knowledge-echo"
    service_version: str = "1.0.0"
    host: str = "0.0.0.0"
    port: int = 8200

    # ── Postgres ────────────────────────────────────────────────
    postgres_host: str = Field(default="localhost", alias="POSTGRES_HOST")
    postgres_port: int = Field(default=5432, alias="POSTGRES_PORT")
    postgres_db: str = Field(default="nexus", alias="POSTGRES_DB")
    postgres_user: str = Field(default="nexus", alias="POSTGRES_USER")
    postgres_password: str = Field(
        default="nexus-dev", alias="POSTGRES_PASSWORD"
    )

    @property
    def postgres_url(self) -> str:
        return (
            "postgresql+asyncpg://"
            f"{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    # ── Redis (feature-flag cache + dedup cache) ────────────────
    redis_host: str = Field(default="localhost", alias="REDIS_HOST")
    redis_port: int = Field(default=6379, alias="REDIS_PORT")
    redis_password: Optional[str] = Field(default=None, alias="REDIS_PASSWORD")
    redis_db: int = 3

    # ── Downstream services ─────────────────────────────────────
    backbone_url: str = Field(
        default="http://localhost:8005", alias="BACKBONE_ENGINE_URL"
    )

    # ── Auth ────────────────────────────────────────────────────
    jwt_secret: str = Field(
        default="test-secret-do-not-use-in-production", alias="JWT_SECRET"
    )
    jwt_algorithm: str = "HS256"
    service_account_user_id: str = "service:knowledge-echo"
    service_account_role: str = "api"
    service_account_token_ttl_seconds: int = 3600

    # ── LLM (question classifier) ───────────────────────────────
    ollama_base_url: str = Field(
        default="http://localhost:11434", alias="OLLAMA_BASE_URL"
    )
    classifier_model: str = Field(
        default="llama3.2:1b", alias="ECHO_CLASSIFIER_MODEL"
    )
    classifier_timeout_seconds: float = 12.0
    classifier_cache_ttl_seconds: int = 86400

    # ── Echo policy (defaults; per-tenant config overrides via flag) ──
    feature_key: str = "knowledge_echo"
    min_confidence_high: float = 0.85
    min_confidence_medium: float = 0.65
    dedup_window_seconds: int = 3600
    max_match_candidates: int = 5
    end_to_end_timeout_seconds: float = 30.0

    # ── Slack ───────────────────────────────────────────────────
    slack_signing_secret_env: str = "SLACK_SIGNING_SECRET"
    slack_request_max_age_seconds: int = 300
    http_timeout_seconds: float = 15.0


def load_config() -> EchoConfig:
    return EchoConfig()
