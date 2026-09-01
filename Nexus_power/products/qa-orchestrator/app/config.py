"""
QA Orchestrator — Configuration.

All environment-driven settings for the orchestrator service.
"""
from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings


class OrchestratorConfig(BaseSettings):
    """Orchestrator configuration with all engine URLs."""

    model_config = {"extra": "ignore", "populate_by_name": True}

    # ── Engine URLs ───────────────────────────────────────────
    # Aliases match Helm ConfigMap env var names for Kubernetes deployment.
    ears_url: str = Field(default="http://localhost:8002", alias="EARS_ENGINE_URL")
    eyes_url: str = Field(default="http://localhost:8003", alias="EYES_ENGINE_URL")
    heart_url: str = Field(default="http://localhost:8004", alias="HEART_ENGINE_URL")
    backbone_url: str = Field(default="http://localhost:8005", alias="BACKBONE_ENGINE_URL")
    shield_url: str = Field(default="http://localhost:8001", alias="SHIELD_ENGINE_URL")
    nerves_url: str = Field(default="http://localhost:8006", alias="NERVES_ENGINE_URL")
    legs_url: str = Field(default="http://localhost:8007", alias="LEGS_ENGINE_URL")
    hands_url: str = Field(default="http://localhost:8008", alias="HANDS_ENGINE_URL")
    spine_url: str = Field(default="http://localhost:8009", alias="SPINE_ENGINE_URL")
    mouth_url: str = Field(default="http://localhost:8010", alias="MOUTH_ENGINE_URL")
    brain_url: str = Field(default="http://localhost:8011", alias="BRAIN_ENGINE_URL")

    # ── Redis ─────────────────────────────────────────────────
    redis_url: str = Field(default="redis://localhost:6379/0")

    # ── JWT ───────────────────────────────────────────────────
    jwt_secret: str = Field(default="dev-jwt-secret-change-me", alias="NEXUS_JWT_SECRET")
