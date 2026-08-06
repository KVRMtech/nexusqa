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

    # ── E2E advance-memory recall thresholds (Release B Phase 4) ─────────
    #: A pooled advance-label prior is trusted for Tier-2.5 recall only when
    #: proven at least this many times…
    advance_prior_min_proofs: int = Field(
        default=3, alias="QEC_ADVANCE_PRIOR_MIN_PROOFS")
    #: …by at least this many distinct tenants (counted pseudonymously).
    advance_prior_min_tenants: int = Field(
        default=2, alias="QEC_ADVANCE_PRIOR_MIN_TENANTS")

    # ── Journey Graph (Release C) ────────────────────────────────────────
    #: Path products are enumerated exactly only up to this cap; larger
    #: spaces are reported as "not enumerated" with per-option coverage —
    #: never an extrapolated percentage.
    #: Raised well above any real question's option count (a US state picker is
    #: 50, a coverage-amount list ~20) so an E2E crawl enumerates every answer
    #: rather than deferring the tail. Excess beyond this is still DEFERRED with
    #: an honest count — never silently dropped, never extrapolated.
    journey_path_enum_cap: int = Field(
        default=512, alias="QEC_JOURNEY_PATH_ENUM_CAP")
    #: Env-level switch for planned branch walks (C4). BOTH this AND the
    #: tenant's ``branch_walks_enabled`` flag must be on (fail-closed).
    branch_walks_enabled: bool = Field(
        default=False, alias="QEC_BRANCH_WALKS_ENABLED")
    #: Env-level switch for the autowalk loop (C5). BOTH this AND the
    #: tenant's ``journey_autowalk`` flag must be on (fail-closed).
    journey_autowalk_enabled: bool = Field(
        default=False, alias="QEC_JOURNEY_AUTOWALK_ENABLED")
    #: Planned branch walks dispatched per completion cycle, at most.
    branch_walks_per_cycle: int = Field(
        default=4, alias="QEC_BRANCH_WALKS_PER_CYCLE")
    #: E1: maximum recursive autowalk depth. A branch walk that reveals new
    #: branches triggers further autowalks up to this depth. 0 = fire-once
    #: (pre-E1 behavior).
    #:
    #: This is a RUNAWAY BACKSTOP, not a coverage policy. The autowalk's real
    #: terminating condition is ``plan_walks`` returning nothing — i.e. the
    #: branch backlog is empty and there is honestly nothing left to walk. A low
    #: number here silently caps coverage instead: a funnel five decisions deep
    #: would report "complete" having walked one of them. Set high enough that
    #: the backlog, not the counter, is what ends the sweep.
    autowalk_max_depth: int = Field(
        default=200, alias="QEC_AUTOWALK_MAX_DEPTH")
    #: E2: pairwise combination walks dispatched per completion cycle.
    pairwise_walks_per_cycle: int = Field(
        default=8, alias="QEC_PAIRWISE_WALKS_PER_CYCLE")

    # ── R5 Vision Medic ───────────────────────────────────────────────────
    #: Master switch for the vision-escalation pathway. OFF by default —
    #: the deterministic ladder + text medic handle >99% of controls;
    #: vision fires only for genuinely DOM-opaque surfaces (canvas, svg,
    #: cross-origin iframe, unlabeled widgets). Turning this on requires
    #: a multimodal-capable model tier on the ``vision_medic`` LLM task.
    crawl_vision_enabled: bool = Field(
        default=False, alias="QEC_CRAWL_VISION_ENABLED")
    #: Per-crawl cap on vision oracle calls (each is a multimodal LLM
    #: request — more expensive than text). The explorer's own cap
    #: (``QEC_MEDIC_ORACLE_MAX_CALLS``) is a separate, tighter backstop.
    vision_max_calls: int = Field(
        default=20, alias="QEC_VISION_MAX_CALLS")
    #: Circuit breaker threshold — consecutive vision failures before the
    #: breaker opens and all subsequent calls return ``unavailable``.
    vision_breaker_threshold: int = Field(
        default=3, alias="QEC_VISION_BREAKER")

    # ── Runnable Journeys (Release D) ────────────────────────────────────
    #: Minimum coverage percent for a case to LINK to a journey.
    journey_link_min_score: int = Field(
        default=50, alias="QEC_JOURNEY_LINK_MIN_SCORE")
    #: Poll cadence for a journey-dispatched runner job.
    journey_run_poll_s: float = Field(
        default=5.0, alias="QEC_JOURNEY_RUN_POLL_S")
    #: Ceiling on how long a journey run is polled before it is honestly
    #: recorded ``timed_out``.
    journey_run_timeout_s: float = Field(
        default=900.0, alias="QEC_JOURNEY_RUN_TIMEOUT_S")

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

    # ── Phase-7 fleet provisioning (OPT-IN; defaults preserve today's behavior —
    #    the fleet router is a NEW additive surface, so these tune it without
    #    touching any existing code path) ──────────────────────────────────────
    #: The reserved tenant scope stamped on a PLATFORM SUPER-ADMIN (operator) token
    #: so it satisfies the mandatory-tenant JWT gate while operating cross-tenant.
    #: It never names a real tenant (provisioning targets come from the request),
    #: so it can match no tenant's RLS scope.
    qec_platform_tenant_id: str = Field(
        default="__platform__", alias="QEC_PLATFORM_TENANT_ID",
    )
    #: TTL (seconds) for the tenant's FIRST admin principal token minted at
    #: onboarding — the client's bootstrap credential.  Default 24h so it survives
    #: a hand-off; override per deploy.
    qec_onboarding_token_ttl_seconds: int = Field(
        default=86400, alias="QEC_ONBOARDING_TOKEN_TTL_SECONDS",
    )
    #: Default offboarding data-retention window (days) when a caller does not
    #: specify one.  Evidence is RETAINED for at least this long — it is never
    #: hard-deleted by offboarding itself (a retention job is a separate seam).
    qec_offboard_retention_days: int = Field(
        default=30, alias="QEC_OFFBOARD_RETENTION_DAYS",
    )

    # ── Phase-8 pluggable auth provider (SSO/OIDC/SAML seam; OPT-IN) ──────
    # The seam is DEFAULT-OFF: ``QEC_AUTH_PROVIDER=jwt`` (the default) authenticates
    # exactly as today (first-party HS256 JWT); ``oidc``/``saml`` are ACTIVE only
    # when explicitly selected AND configured.  Every knob below defaults empty so
    # an unset environment is byte-identical to the pre-Phase-8 posture.
    #: Active provider: ``jwt`` (default) | ``oidc`` | ``saml``.  An unknown value
    #: is fail-closed (every request is DENIED, never allowed through).
    qec_auth_provider: str = Field(default="jwt", alias="QEC_AUTH_PROVIDER")

    # ── OIDC (verify an IdP-issued ID token via JWKS) ──
    #: The IdP issuer (``iss``) the ID token must carry (e.g.
    #: ``https://your-org.okta.com``).  Required when ``QEC_AUTH_PROVIDER=oidc``.
    qec_oidc_issuer: str = Field(default="", alias="QEC_OIDC_ISSUER")
    #: The IdP JWKS URL (public signing keys).  Required for ``oidc`` unless an
    #: out-of-band signing-key resolver is injected.
    qec_oidc_jwks_url: str = Field(default="", alias="QEC_OIDC_JWKS_URL")
    #: The audience (``aud``) the ID token must contain — the Verdict client id
    #: registered at the IdP.  Required when ``QEC_AUTH_PROVIDER=oidc``.
    qec_oidc_audience: str = Field(default="", alias="QEC_OIDC_AUDIENCE")
    #: Comma-separated accepted signing algorithms (asymmetric only).
    qec_oidc_algorithms: str = Field(default="RS256", alias="QEC_OIDC_ALGORITHMS")
    #: The ID-token claim that maps to the Verdict RLS tenant.
    qec_oidc_tenant_claim: str = Field(
        default="tenant_id", alias="QEC_OIDC_TENANT_CLAIM",
    )
    #: The ID-token claim that maps to the Verdict role (falls back to the default
    #: role when absent).
    qec_oidc_role_claim: str = Field(default="role", alias="QEC_OIDC_ROLE_CLAIM")
    #: The ID-token claim used for the principal email.
    qec_oidc_email_claim: str = Field(default="email", alias="QEC_OIDC_EMAIL_CLAIM")
    #: Role assigned when the role claim is absent from the ID token.
    qec_oidc_default_role: str = Field(
        default="viewer", alias="QEC_OIDC_DEFAULT_ROLE",
    )
    #: OPTIONAL MFA defense-in-depth: comma-separated ``acr`` values the ID token
    #: must assert (empty ⇒ not enforced; MFA is primarily enforced at the IdP).
    qec_oidc_required_acr: str = Field(default="", alias="QEC_OIDC_REQUIRED_ACR")
    #: Clock-skew leeway (seconds) allowed on ID-token ``exp``/``iat`` validation.
    qec_oidc_leeway_seconds: int = Field(
        default=60, alias="QEC_OIDC_LEEWAY_SECONDS",
    )

    # ── SAML (assertion → session → internal principal token) ──
    #: The IdP EntityID.  Required when ``QEC_AUTH_PROVIDER=saml``.
    qec_saml_idp_entity_id: str = Field(
        default="", alias="QEC_SAML_IDP_ENTITY_ID",
    )
    #: The Verdict Service-Provider EntityID (metadata / audience restriction).
    qec_saml_sp_entity_id: str = Field(default="", alias="QEC_SAML_SP_ENTITY_ID")
    #: The Assertion Consumer Service URL the IdP POSTs the signed assertion to.
    qec_saml_acs_url: str = Field(default="", alias="QEC_SAML_ACS_URL")
    #: The assertion attribute mapped to the Verdict RLS tenant.
    qec_saml_tenant_attribute: str = Field(
        default="tenant_id", alias="QEC_SAML_TENANT_ATTRIBUTE",
    )
    #: The assertion attribute mapped to the Verdict role.
    qec_saml_role_attribute: str = Field(
        default="role", alias="QEC_SAML_ROLE_ATTRIBUTE",
    )
    #: The assertion attribute mapped to the principal email.
    qec_saml_email_attribute: str = Field(
        default="email", alias="QEC_SAML_EMAIL_ATTRIBUTE",
    )
    #: Role assigned when the role attribute is absent from the assertion.
    qec_saml_default_role: str = Field(
        default="viewer", alias="QEC_SAML_DEFAULT_ROLE",
    )
    #: Lifetime (seconds) of the internal principal (session) token minted at ACS.
    qec_saml_session_ttl_seconds: int = Field(
        default=3600, alias="QEC_SAML_SESSION_TTL_SECONDS",
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
