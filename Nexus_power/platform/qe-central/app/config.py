"""QE-Central — Configuration.

Centralises all environment-driven settings (mirrors
``platform/api/app/config.py``).  Every knob is env-driven; defaults are
development-safe only and MUST be overridden in any deployed environment
(the docker-compose.qec.yml wiring does exactly that).
"""
from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings

# ─── Phase-6 safety-spine constants (shared by app.security.boot_validator and
#     app.auth) ──────────────────────────────────────────────────────────────
#: The JWT ``aud`` claim stamped on VKPower-Verdict-issued principal tokens and
#: verified by :func:`app.auth._decode_token`.  Config-overridable via
#: ``QEC_JWT_AUDIENCE`` (:attr:`Settings.qec_jwt_audience`).
AUDIENCE = "vkpower-verdict"

#: Deployed environments where the boot gate BITES (refuses to start) and the
#: JWT missing-aud transition warning fires.  Anything else — ``development``,
#: ``test``, or an unknown value — is treated as non-deployed (WARN-only), so
#: local dev and the test suite are never gated.
DEPLOYED_ENVS = frozenset({"staging", "production"})

#: The development KEK provider (no KMS envelope); fatal in a deployed env.
DEV_KEK_PROVIDER = "local"

#: ``NEXUS_JWT_SECRET`` values that are empty or a known development default —
#: the boot validator refuses any of these in a deployed environment.
DEV_DEFAULT_JWT_SECRETS = frozenset({
    "",
    "dev-jwt-secret-change-me",
    "dev-jwt-secret",
    "test-secret-do-not-use-in-production",
    "unit-test-secret-qe-central",
    "change-me",
    "changeme",
})

#: ``QEC_EXPLORER_TOKEN`` values that are empty or the known development default.
DEV_DEFAULT_EXPLORER_TOKENS = frozenset({
    "",
    "dev-explorer-token-change-me",
})

#: Lower-cased DB passwords (parsed out of a DSN) that betray a development or
#: otherwise-unsafe default; the boot validator refuses these in a deployed env.
#: NOTE: only membership is ever surfaced — the parsed password value is NEVER
#: logged or echoed into a violation message.
DEV_DEFAULT_DB_PASSWORDS = frozenset({
    "",
    "qec-dev",
    "qec-substrate-dev",
    "postgres",
    "password",
    "change-me",
    "changeme",
})


class Settings(BaseSettings):
    """QE-Central configuration loaded from environment variables.

    Env-var names are pinned by the Phase-0 shared conventions:
    ``QEC_DATABASE_URL``, ``NEXUS_DATABASE_URL_SUBSTRATE``,
    ``NEXUS_JWT_SECRET``, ``PLATFORM_API_URL``, ``NEXUS_STORAGE_BACKEND``,
    ``NEXUS_FRAME_STORAGE_PATH``, ``QE_HARNESS_ENABLED``,
    ``QEC_SERVICE_NAME``, ``QEC_LOG_LEVEL``.
    """

    model_config = {"extra": "ignore"}

    # ── Deployment environment (Phase-6 safety spine) ─────────
    #: ``development`` | ``test`` | ``staging`` | ``production``.  Read from
    #: ``NEXUS_ENV`` (the shared platform convention).  Only ``staging`` and
    #: ``production`` are "deployed": the boot gate + JWT missing-aud rejection
    #: bite there; ``development``/``test`` (the default) keep today's behavior.
    nexus_env: str = Field(default="development", alias="NEXUS_ENV")

    # ── Envelope-encryption KEK provider (surfaced for the boot gate + the
    #    /health KEK canary; the live provider is still built in main._kek_provider
    #    from the same env var) ──────────────────────────────────────────────
    #: ``local`` (dev KEK — fatal in a deployed env) | ``gcp_kms`` | ``aws_kms``.
    nexus_kek_provider: str = Field(default="local", alias="NEXUS_KEK_PROVIDER")

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
    # ── JWT audience (Phase-6: no VKPower<->Verdict token bleed) ──
    #: The ``aud`` claim VKPower-Verdict stamps on its own principal tokens and
    #: verifies on inbound tokens.  A token whose ``aud`` is PRESENT but does not
    #: match this value (e.g. a token minted for another service) is rejected;
    #: a token MISSING ``aud`` is accepted during the transition window unless
    #: :attr:`qec_require_aud` is set (see ``app.auth._decode_token``).
    qec_jwt_audience: str = Field(default=AUDIENCE, alias="QEC_JWT_AUDIENCE")
    #: When truthy, an inbound token that carries NO ``aud`` claim is REJECTED
    #: (401) instead of accepted-with-warning.  Default ``False`` so today's
    #: tokens (minted without ``aud``, incl. shared VKPower human sessions) keep
    #: working; flip to ``True`` once every issuer stamps the audience.
    qec_require_aud: bool = Field(default=False, alias="QEC_REQUIRE_AUD")

    # ── Explorer HMAC token (surfaced for the boot gate; the live dispatch value
    #    is still read by app.clients.config.Phase1Settings.explorer_token) ─────
    #: Empty or the dev default ⇒ fatal in a deployed env (the boot validator
    #: refuses to crawl a real client app with an unauthenticated explorer seam).
    qec_explorer_token: str = Field(default="", alias="QEC_EXPLORER_TOKEN")

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

    # ── Derived helpers (Phase-6) ─────────────────────────────
    @property
    def is_deployed_env(self) -> bool:
        """True when running in a deployed env (``staging``/``production``).

        The boot gate refuses to start and the JWT missing-aud path rejects/
        warns ONLY when this is True — ``development``/``test`` (and any unknown
        value) stay INERT.
        """
        return (self.nexus_env or "").strip().lower() in DEPLOYED_ENVS


# Singleton — import as ``from .config import settings`` (relative) or
# ``from app.config import settings`` (absolute, e.g. alembic/tests).
settings = Settings()
