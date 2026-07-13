"""QE-Central — ORM models for the Phase-0 (S1) qecentral-DB tables.

Only the three S1 tables get ORM here (design §3.1 data model + §3.5
``client_apps`` detail): ``client_apps``, ``qe_explorations``,
``qe_harness_runs``.  The S3/S4/S5 tables are created by migration
``qec_001_initial`` but receive their ORM in their own phases.

Models bind a QE-Central-private ``QecBase`` — NOT the SDK ``Base`` —
because the qecentral logical database is a separate bounded context
(R-7): sharing the SDK metadata would pollute autogenerate/create_all
with every VKPower table.

All tables carry ``tenant_id`` and are protected by ENABLE+FORCE RLS
with the standard ``tenant_isolation`` policy (see qec_001_initial).
Timestamps are UTC timezone-aware.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Index,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class QecBase(DeclarativeBase):
    """Declarative base for ALL qecentral-DB models (kept separate from
    ``nexus_sdk.db.Base`` — engine-enforced bounded context, R-7)."""


class ClientAppRow(QecBase):
    """One registered client application (merged registry, R-8).

    ``creds_blob`` is an ``EnvelopeBlob.to_bytes()`` KMS envelope with
    AAD = ``app_id`` — never plaintext at rest (auth_profiles.py:73-75
    discipline); NULL when no credentials were provided.
    """

    __tablename__ = "client_apps"

    app_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    base_url: Mapped[str] = mapped_column(String(2000), nullable=False)
    # Politeness / fence key (registrable domain of base_url).
    canonical_host: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    # KMS envelope (AAD=app_id); NULL = no creds registered.
    creds_blob: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    answer_key: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # {attested_by, attested_at, env_kind prod|staging|disposable,
    #  reset_procedure, expires_at} — submit tier is FAIL-CLOSED on this.
    env_attestation: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # {allowed_hosts, blocked_path_globs, irreversible_verbs_extra, max_rps,
    #  max_crawl_workers, blackout_windows, allow_submit}
    fences: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    repo_binding: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    schedule: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    budgets: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    latest_artifact_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    baseline_fingerprint_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now, onupdate=_utc_now,
    )

    __table_args__ = (
        Index("ix_client_apps_tenant_host", "tenant_id", "canonical_host"),
        Index("ix_client_apps_tenant_status", "tenant_id", "status"),
    )


class ClientAppEnvironmentRow(QecBase):
    """One named Environment Profile for a client app (multi-env, crawl-once/run-many).

    An app is crawled ONCE against a reference env → one env-invariant flow + one
    approved baseline.  Each environment (dev/test/uat/prod) is then a run-time
    REBIND of that same flow: this row carries the env-specific ``base_url`` +
    routing ``cookies``/``headers`` + ``data_overrides`` + per-env ``fences``
    (``allow_submit`` etc.) + an ``env_assertion`` that fails a run CLOSED-to-RED
    if it landed on the wrong environment.

    ``creds_blob`` is an ``EnvelopeBlob.to_bytes()`` KMS envelope (AAD =
    ``environment_id``) folding the env's login credentials + HTTP basic-auth +
    any SECRET cookies/headers — never plaintext at rest, NULL when none.  Non-
    secret routing cookies/headers live in the ``cookies``/``headers`` columns.
    Soft-refs ``app_id`` (no hard FK — qec child convention).
    """

    __tablename__ = "app_environments"

    environment_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    app_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    base_url: Mapped[str] = mapped_column(String(2000), nullable=False, default="")
    canonical_host: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    # Non-secret routing cookies [{name,value,domain,path}] + headers {name:value}.
    cookies: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    headers: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # {data_key: value} run-time D['key'] overrides (env-specific valid test data).
    data_overrides: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # Per-env fence overrides (same shape as client_apps.fences); resolved over the
    # app's fences + this env's env_kind at crawl/run time (prod ⇒ allow_submit off).
    fences: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # {env_kind prod|staging|disposable, attested_by, expires_at, ...} per env.
    env_attestation: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # {selector, expect_text} and/or {url_pattern} — the HARD proof a run landed on
    # THIS env; a mismatch fails CLOSED to a RED real_regression (never green-wash).
    env_assertion: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # KMS envelope (AAD=environment_id): login creds + basic_auth + secret cookies/
    # headers; NULL = none.
    creds_blob: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now, onupdate=_utc_now,
    )

    __table_args__ = (
        Index("ix_app_environments_tenant_app", "tenant_id", "app_id"),
        UniqueConstraint("tenant_id", "app_id", "name", name="uq_app_environments_name"),
    )


class QEExplorationRow(QecBase):
    """One substrate-write attempt (design §3.1 ``qe_explorations``).

    ``status`` ∈ pending | writing | completed | failed | refused —
    refusal is FIRST-CLASS, never a silently-empty artifact.
    """

    __tablename__ = "qe_explorations"

    exploration_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    app_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    artifact_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    session_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    explorer_version: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    # The EXACT string written on every substrate row: 'qec_live_v1@{crawl_id}'.
    extractor_version: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    # {visits, actions, screenshots, redactions, ...} from WriteStats.
    stats: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # Honest failure / refusal reason (empty when completed).
    error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now, onupdate=_utc_now,
    )

    __table_args__ = (
        Index("ix_qe_explorations_tenant_app", "tenant_id", "app_id"),
        Index("ix_qe_explorations_tenant_artifact", "tenant_id", "artifact_id"),
    )


class QEHarnessRunRow(QecBase):
    """One REFUSE-matrix rule evaluation (design §3.1 ``qe_harness_runs``).

    ``verdict`` ∈ REFUSED_CORRECTLY | GREEN_WASH_DETECTED | CHAIN_ERROR |
    PASS_BASELINE.  GREEN_WASH_DETECTED on ANY rule is a deploy-gate
    failure; honesty results are themselves auditable evidence, so the
    full per-rule HTTP request/response evidence is persisted in
    ``report``.
    """

    __tablename__ = "qe_harness_runs"

    harness_run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    fixture_name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    # R1..R8 (or 'baseline' for the golden-fixture PASS_BASELINE run).
    rule_id: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    expected: Mapped[str] = mapped_column(Text, nullable=False, default="")
    observed: Mapped[str] = mapped_column(Text, nullable=False, default="")
    verdict: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    report: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now,
    )

    __table_args__ = (
        Index("ix_qe_harness_runs_tenant_created", "tenant_id", "created_at"),
    )
