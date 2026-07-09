"""repo-intel — Configuration (Settings singleton, env-driven).

repo-intel subclasses the SDK ``EngineConfig`` (design §3.3: NexusEngine
scaffold, ``EngineConfig(engine_name='repo-intel', engine_port=8014)``) and
extends it with the repo-intel-specific knobs.  ONE config object serves
both roles — the ``EngineConfig`` the ``NexusEngine`` base needs AND the
``settings`` singleton the application modules import.

Two DB DSNs (mirrors qe-central, design R-7):
  * ``QEC_DATABASE_URL``            → the carved-out ``qecentral`` logical DB
                                      (role ``qec``; repo-intel's 5 tables).
  * ``NEXUS_DATABASE_URL_SUBSTRATE``→ the ``nexus`` DB (READ-ONLY use:
                                      ``page_visits`` for drift reports).

Every knob is env-driven; the defaults are development-safe only and MUST
be overridden in any deployed environment (docker-compose.qec.yml does
exactly that).
"""
from __future__ import annotations

from pydantic import Field

from nexus_sdk.config import EngineConfig


class RepoIntelConfig(EngineConfig):
    """repo-intel engine configuration.

    Inherits the platform knobs (JWT secret, Redis, CORS, observability)
    from ``EngineConfig`` and pins the repo-intel identity plus the two
    database DSNs and the git-connector operational caps.
    """

    # ── Engine identity (design §3.3) ─────────────────────────
    engine_name: str = "repo-intel"
    engine_version: str = "0.1.0"
    engine_port: int = 8014

    # ── Databases (two DSNs — R-7 carve-out) ──────────────────
    # qecentral logical DB (role qec) — repo-intel's 5 tables.
    qec_database_url: str = Field(
        default="postgresql+asyncpg://qec:qec-dev@postgres:5432/qecentral",
        alias="QEC_DATABASE_URL",
    )
    # nexus DB — READ-ONLY page_visits for drift (least-privilege role).
    nexus_database_url_substrate: str = Field(
        default="postgresql+asyncpg://qec_substrate:qec-substrate-dev@postgres:5432/nexus",
        alias="NEXUS_DATABASE_URL_SUBSTRATE",
    )

    # ── Git connector operational caps (design §3.3 connectors/git) ──
    # Per-tenant workdir root; each clone lands under
    # {root}/{tenant_id}/{connection_id} (isolation, connectors/git.py).
    repo_workdir_root: str = Field(
        default="/app/service/data/repos", alias="REPO_INTEL_WORKDIR_ROOT",
    )
    # Hard size cap enforced AFTER clone — a repo over this is refused and
    # its workdir removed (a huge client repo can never fill the disk).
    max_repo_bytes: int = Field(
        default=2_000_000_000, alias="REPO_INTEL_MAX_REPO_BYTES",
    )
    clone_timeout_seconds: int = Field(
        default=300, alias="REPO_INTEL_CLONE_TIMEOUT_SECONDS",
    )
    ls_remote_timeout_seconds: int = Field(
        default=30, alias="REPO_INTEL_LS_REMOTE_TIMEOUT_SECONDS",
    )
    git_binary: str = Field(default="git", alias="GIT_BINARY")

    # ── Service JWT (repo-intel /diff is consumed by S5; minted later) ──
    jwt_algorithm: str = Field(default="HS256", alias="QEC_JWT_ALGORITHM")
    service_token_ttl_seconds: int = Field(
        default=3600, alias="QEC_SERVICE_TOKEN_TTL_SECONDS",
    )

    # ``populate_by_name`` lets callers build the config by field name in
    # tests while env-loading still uses the aliases above.
    model_config = {**EngineConfig.model_config, "populate_by_name": True}


# Singleton — import as ``from app.config import settings``.
settings = RepoIntelConfig()
