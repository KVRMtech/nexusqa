"""
API Gateway — Configuration.

All environment-driven settings for the reverse proxy / gateway.
"""
from __future__ import annotations

from pydantic_settings import BaseSettings
from pydantic import Field


class GatewayConfig(BaseSettings):
    """Gateway configuration loaded from environment variables."""

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
        "populate_by_name": True,
    }

    # ── Server ────────────────────────────────────────────────
    gateway_port: int = Field(default=8080, alias="GATEWAY_PORT")

    # ── Engine URLs ───────────────────────────────────────────
    # Aliases match Helm ConfigMap env var names (e.g. SHIELD_ENGINE_URL).
    # populate_by_name=True allows field names to work in local .env files.
    auth_url: str = Field(default="http://localhost:8000", alias="AUTH_SERVICE_URL")
    shield_url: str = Field(default="http://localhost:8001", alias="SHIELD_ENGINE_URL")
    ears_url: str = Field(default="http://localhost:8002", alias="EARS_ENGINE_URL")
    eyes_url: str = Field(default="http://localhost:8003", alias="EYES_ENGINE_URL")
    heart_url: str = Field(default="http://localhost:8004", alias="HEART_ENGINE_URL")
    backbone_url: str = Field(default="http://localhost:8005", alias="BACKBONE_ENGINE_URL")
    nerves_url: str = Field(default="http://localhost:8006", alias="NERVES_ENGINE_URL")
    legs_url: str = Field(default="http://localhost:8007", alias="LEGS_ENGINE_URL")
    hands_url: str = Field(default="http://localhost:8008", alias="HANDS_ENGINE_URL")
    spine_url: str = Field(default="http://localhost:8009", alias="SPINE_ENGINE_URL")
    mouth_url: str = Field(default="http://localhost:8010", alias="MOUTH_ENGINE_URL")
    brain_url: str = Field(default="http://localhost:8011", alias="BRAIN_ENGINE_URL")
    orchestrator_url: str = Field(default="http://localhost:8100", alias="ORCHESTRATOR_URL")
    platform_api_url: str = Field(default="http://localhost:8091", alias="PLATFORM_API_URL")

    # ── CORS ──────────────────────────────────────────────────
    cors_origins: str = Field(
        default="http://localhost:3000,http://localhost:5173",
        alias="CORS_ALLOWED_ORIGINS",
    )

    # ── Rate Limiting ─────────────────────────────────────────
    rate_limit_per_minute: int = Field(default=6000)

    # ── JWT ───────────────────────────────────────────────────
    nexus_jwt_secret: str = Field(default="dev-jwt-secret-change-me")
    jwt_algorithm: str = "HS256"

    # ── Public Paths (skip JWT validation) ────────────────────
    public_path_prefixes: str = Field(
        default="/api/v1/auth/login,/api/v1/auth/refresh,/api/v1/auth/self-signup",
        alias="GATEWAY_PUBLIC_PATHS",
    )

    # ── Upload Size ────────────────────────────────────────────
    gateway_max_request_body_mb: int = Field(
        default=500,
        description="Maximum request body size in MB for proxied requests",
    )
