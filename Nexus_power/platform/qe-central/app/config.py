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
    # ArtifactStore local root. MUST be writable by the container's non-root
    # user (the Dockerfile chowns /app/service/data to `nexus`). Passed
    # EXPLICITLY into StorageConfig(local_root=...) so it never silently falls
    # back to the SDK's hardcoded /data/nexus default (which the container
    # cannot create — the live-VM REFUSE-matrix [Errno 13] on '/data').
    nexus_storage_path: str = Field(
        default="/app/service/data", alias="NEXUS_STORAGE_PATH",
    )

    # ── Phase-0 REFUSE harness (deploy gate; default OFF) ─────
    qe_harness_enabled: bool = Field(default=False, alias="QE_HARNESS_ENABLED")

    # ── Phase-5.5 distributed scale-out (OPT-IN; the default of EVERY knob
    #    preserves today's single-instance behavior — never change a default
    #    here without re-checking the existing test outcomes) ─────────────
    #: Admission-limiter backend: ``memory`` (default — the process-local
    #: :data:`app.controlplane.scheduling.admission.ADMISSION` singleton) or
    #: ``redis`` (one shared, atomic, POLITENESS-FIRST fail-closed limiter so
    #: N replicas enforce ONE customer-facing rate instead of N×).
    qec_admission_backend: str = Field(default="memory", alias="QEC_ADMISSION_BACKEND")
    #: Redis DSN for the distributed limiter (``redis.asyncio.from_url``). Empty
    #: (default) ⇒ the redis backend fail-closes EVERY admit (deny/wait, never
    #: fail-open and burst a customer's app).
    qec_redis_url: str = Field(default="", alias="QEC_REDIS_URL")
    #: Key namespace for the redis limiter (isolates fleets sharing one redis).
    qec_admission_redis_namespace: str = Field(
        default="qec:adm", alias="QEC_ADMISSION_REDIS_NAMESPACE",
    )
    #: Safety TTL (seconds) on a distributed lease + per-host mutex. MUST exceed
    #: ``QEC_CYCLE_MAX_WALLCLOCK_SECONDS`` so a live cycle is never prematurely
    #: reaped; a crashed replica's slot then self-heals after this bound.
    qec_admission_lease_ttl_seconds: int = Field(
        default=7200, alias="QEC_ADMISSION_LEASE_TTL_SECONDS",
    )
    #: Backoff hint (seconds) returned when the limiter store is unreachable —
    #: the driver defers the cycle by roughly this long before re-attempting.
    qec_admission_unavailable_backoff_seconds: float = Field(
        default=1.0, alias="QEC_ADMISSION_UNAVAILABLE_BACKOFF_SECONDS",
    )

    #: Cycle-daemon leader election: ``none`` (default — the daemon is always
    #: leader, today's single-daemon behavior) or ``advisory_lock`` (Postgres
    #: ``pg_advisory_lock`` — exactly ONE replica scans the fleet; on leader
    #: death the session lock auto-releases and a follower takes over).
    qec_daemon_leader_election: str = Field(
        default="none", alias="QEC_DAEMON_LEADER_ELECTION",
    )
    #: Stable string hashed to the fixed 64-bit advisory-lock key (one logical
    #: lock shared by every replica of this fleet).
    qec_leader_lock_key: str = Field(
        default="qec-cycle-driver-leader", alias="QEC_LEADER_LOCK_KEY",
    )
    #: A follower re-attempts leadership acquisition on this interval (seconds).
    qec_leader_retry_interval_seconds: float = Field(
        default=15.0, alias="QEC_LEADER_RETRY_INTERVAL_SECONDS",
    )


# Singleton — import as ``from .config import settings`` (relative) or
# ``from app.config import settings`` (absolute, e.g. alembic/tests).
settings = Settings()
