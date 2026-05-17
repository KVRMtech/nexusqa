"""
Platform API — Configuration.

Centralises all environment-driven settings.
"""
from __future__ import annotations

import os
from pydantic_settings import BaseSettings
from pydantic import Field


class PlatformAPIConfig(BaseSettings):
    """Platform API configuration loaded from environment variables."""

    model_config = {"extra": "ignore"}

    # ── Server ────────────────────────────────────────────────
    host: str = Field(default="0.0.0.0", alias="HOST")
    port: int = Field(default=8091, alias="PORT")

    # ── JWT ───────────────────────────────────────────────────
    jwt_secret: str = Field(default="dev-jwt-secret-change-me", alias="NEXUS_JWT_SECRET")
    jwt_algorithm: str = "HS256"

    # ── Engine URLs ───────────────────────────────────────────
    ears_engine_url: str = Field(default="http://localhost:8002")
    eyes_engine_url: str = Field(default="http://localhost:8003")
    heart_engine_url: str = Field(default="http://localhost:8004")
    backbone_engine_url: str = Field(default="http://localhost:8005")
    shield_engine_url: str = Field(default="http://localhost:8001")
    nerves_engine_url: str = Field(default="http://localhost:8006")
    hands_engine_url: str = Field(default="http://localhost:8008")
    legs_engine_url: str = Field(default="http://localhost:8007")
    spine_engine_url: str = Field(default="http://localhost:8009")
    mouth_engine_url: str = Field(default="http://localhost:8010")
    brain_engine_url: str = Field(default="http://localhost:8011")

    # ── Control Plane URLs ────────────────────────────────────
    gateway_url: str = Field(default="http://localhost:8080")
    orchestrator_url: str = Field(default="http://localhost:8100")
    auth_service_url: str = Field(default="http://localhost:8000")

    # ── PostgreSQL ────────────────────────────────────────────
    postgres_host: str = Field(default="localhost")
    postgres_port: int = Field(default=5432)
    postgres_user: str = Field(default="nexus")
    postgres_password: str = Field(default="nexus-dev")
    postgres_db: str = Field(default="nexus")

    # ── Redis ─────────────────────────────────────────────────
    redis_host: str = Field(default="localhost")
    redis_port: int = Field(default=6379)
    redis_password: str = Field(default="")

    @property
    def postgres_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )
