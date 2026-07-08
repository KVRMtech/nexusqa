"""QE-Central — Configuration.

Centralises all environment-driven settings (mirrors
``platform/api/app/config.py``).  Every knob is env-driven; defaults are
development-safe only and MUST be overridden in any deployed environment
(the docker-compose.qec.yml wiring does exactly that).
"""
from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """QE-Central configuration loaded from environment variables.

    Env-var names are pinned by the Phase-0 shared conventions:
    ``QEC_DATABASE_URL``, ``NEXUS_DATABASE_URL_SUBSTRATE``,
    ``NEXUS_JWT_SECRET``, ``PLATFORM_API_URL``, ``NEXUS_STORAGE_BACKEND``,
    ``NEXUS_FRAME_STORAGE_PATH``, ``QE_HARNESS_ENABLED``,
    ``QEC_SERVICE_NAME``, ``QEC_LOG_LEVEL``.
    """

    model_config = {"extra": "ignore"}

    # ── Server ────────────────────────────────────────────────
    host: str = Field(default="0.0.0.0", alias="HOST")
    port: int = Field(default=8093, alias="PORT")
    qec_service_name: str = Field(default="qe-central", alias="QEC_SERVICE_NAME")
    qec_log_level: str = Field(default="INFO", alias="QEC_LOG_LEVEL")

    # ── Databases (two DSNs — R-7 carve-out) ──────────────────
    # qecentral logical DB (role qec) — ALL QE-Central-owned tables.
    qec_database_url: str = Field(
        default="postgresql+asyncpg://qec:qec-dev@postgres:5432/qecentral",
        alias="QEC_DATABASE_URL",
    )
    # nexus DB (role qec_substrate) — least-privilege substrate writes only.
    nexus_database_url_substrate: str = Field(
        default="postgresql+asyncpg://qec_substrate:qec-substrate-dev@postgres:5432/nexus",
        alias="NEXUS_DATABASE_URL_SUBSTRATE",
    )

    # ── JWT (shared secret with platform-api; HS256) ──────────
    nexus_jwt_secret: str = Field(
        default="dev-jwt-secret-change-me", alias="NEXUS_JWT_SECRET",
    )
    jwt_algorithm: str = "HS256"
    # TTL for minted service tokens (mint_service_jwt).
    service_token_ttl_seconds: int = Field(
        default=3600, alias="QEC_SERVICE_TOKEN_TTL_SECONDS",
    )

    # ── VKPower factory (consumed over HTTP with a service JWT) ─
    platform_api_url: str = Field(
        default="http://platform-api:8091", alias="PLATFORM_API_URL",
    )

    # ── Storage (must match platform-api's backend so frame assets
    #    are co-readable — design §3.1 / R-5) ────────────────────
    nexus_storage_backend: str = Field(
        default="local", alias="NEXUS_STORAGE_BACKEND",
    )
    nexus_frame_storage_path: str = Field(
        default="/app/service/data/frames", alias="NEXUS_FRAME_STORAGE_PATH",
    )

    # ── Phase-0 REFUSE harness (deploy gate; default OFF) ─────
    qe_harness_enabled: bool = Field(default=False, alias="QE_HARNESS_ENABLED")


# Singleton — import as ``from .config import settings`` (relative) or
# ``from app.config import settings`` (absolute, e.g. alembic/tests).
settings = Settings()
