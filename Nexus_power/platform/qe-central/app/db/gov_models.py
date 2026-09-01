"""QE-Central — ORM for the nine S4 scenario-governance tables ("the 1%").

These bind the SAME QE-Central-private ``QecBase`` declared in
``app.db.models`` (NOT the SDK ``Base``) so the qecentral logical database
stays a single bounded context (R-7).  Every column here mirrors
``alembic_qec/versions/qec_001_initial.py`` EXACTLY — the migration is the
source of truth (no migration is written for S4; the tables already exist):

  * ``qec_criticality_registry`` → :class:`CriticalityRegistryRow`
  * ``qec_scenarios``            → :class:`ScenarioRow`
  * ``qec_approval_events``      → :class:`ApprovalEventRow`
  * ``qec_coverage_atoms``       → :class:`CoverageAtomRow`
  * ``qec_certified_invariants`` → :class:`CertifiedInvariantRow`
  * ``qec_universe_baselines``   → :class:`UniverseBaselineRow`
  * ``qec_coverage_gaps``        → :class:`CoverageGapRow`
  * ``qec_case_tiers``           → :class:`CaseTierRow`
  * ``qec_touch_events``         → :class:`TouchEventRow`

Every table carries ``tenant_id`` and is protected by ENABLE+FORCE RLS with
the standard ``tenant_isolation`` policy (see qec_001_initial).  The ``Index``
declarations reproduce the migration's index NAMES so ORM metadata is a
faithful description of the deployed schema; schema itself is owned by
Alembic — nothing here runs ``create_all`` against a real database.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .models import QecBase


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ── qec_criticality_registry (Stage-2 owner: criticality.py) ───────────────
class CriticalityRegistryRow(QecBase):
    """One immutable version of the data-driven criticality marker table.

    ``pack`` = ``{signals: [{signal_id, band, applies_to, matchers,
    rationale}]}``.  Versions are immutable (verdict_events stamping
    pattern); exactly one row per tenant carries ``active=True``.
    """

    __tablename__ = "qec_criticality_registry"

    registry_version: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    pack: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_by: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now,
    )

    __table_args__ = (
        Index(
            "ix_qec_criticality_registry_tenant_active",
            "tenant_id", "active",
        ),
    )


# ── qec_scenarios (Stage-2 owner: synthesis.py) ────────────────────────────
class ScenarioRow(QecBase):
    """One deterministic journey scenario (value-free, locator-free).

    ``scenario_id`` = ``uuid5(app_id + journey skeleton)`` — stable identity
    across re-crawls.  ``diff_state`` ∈ new | changed | unchanged | missing;
    UNCHANGED auto-carries approval (zero touch), only NEW/CHANGED enter the
    approval queue, MISSING drives the shrinkage path.
    """

    __tablename__ = "qec_scenarios"

    scenario_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    app_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    source_artifact_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    name: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    journey: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    criticality_band: Mapped[str] = mapped_column(String(8), nullable=False, default="P1")
    criticality_evidence: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    registry_version: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    diff_state: Mapped[str] = mapped_column(String(16), nullable=False, default="new")
    review: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    approved_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    tier: Mapped[str] = mapped_column(String(16), nullable=False, default="unlabeled")
    materialized_artifact_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now, onupdate=_utc_now,
    )

    __table_args__ = (
        Index("ix_qec_scenarios_tenant_app", "tenant_id", "app_id"),
        Index("ix_qec_scenarios_tenant_app_diff", "tenant_id", "app_id", "diff_state"),
    )


# ── qec_approval_events (Stage-1: approval.py) ─────────────────────────────
class ApprovalEventRow(QecBase):
    """One append-only, hash-chained approval event.

    Chain is per ``(tenant_id, subject_kind, subject_id)``:
    ``chain_hash = sha256(prev_hash + canonical sorted-JSON payload)`` — the
    verdict_events.py:66-124 recipe.  ``action`` ∈ submit | approve | reject |
    reopen | carry_forward.  ``approve`` REQUIRES a typed ``signature`` (the
    router maps a missing one to HTTP 422).  ``carry_forward`` is recorded but
    is NOT a human touch.
    """

    __tablename__ = "qec_approval_events"

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    signature: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    actor: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    carry_forward: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    chain_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now,
    )

    __table_args__ = (
        Index(
            "ix_qec_approval_events_chain",
            "tenant_id", "subject_kind", "subject_id", "created_at",
        ),
    )


# ── qec_coverage_atoms (Stage-1: coverage.py) ──────────────────────────────
class CoverageAtomRow(QecBase):
    """One enumerable coverage atom.

    ``atom_id`` = ``uuid5`` of the tenant/app-scoped ``canonical_key`` —
    idempotent across recomputes.  ``kind`` ∈ route | api_endpoint |
    form_field | journey_edge | validator; ``source`` ∈ crawl | repo |
    answer_key; ``provenance`` ∈ G_DETERMINISTIC | G_LIVE_CONFIRMED |
    G_INFERRED.  Atoms are UPSERT-only (never hard-deleted) so an approved
    baseline's membership stays reconstructable for the shrinkage guard.
    """

    __tablename__ = "qec_coverage_atoms"

    atom_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    app_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    canonical_key: Mapped[str] = mapped_column(String(1000), nullable=False, default="")
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="crawl")
    provenance: Mapped[str] = mapped_column(String(32), nullable=False, default="G_INFERRED")
    evidence: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    first_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now,
    )
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now,
    )

    __table_args__ = (
        Index(
            "ix_qec_coverage_atoms_tenant_app_kind",
            "tenant_id", "app_id", "kind",
        ),
    )


# ── qec_certified_invariants (Stage-1: coverage.py reads; router authors) ──
class CertifiedInvariantRow(QecBase):
    """One human-authored, e-signed P0 invariant — the non-enumerable half of
    coverage (e.g. an underwriting ceiling).  The product EXECUTES and
    CERTIFIES these; it never claims to auto-discover them.
    """

    __tablename__ = "qec_certified_invariants"

    invariant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    app_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    statement: Mapped[str] = mapped_column(Text, nullable=False, default="")
    criticality_band: Mapped[str] = mapped_column(String(8), nullable=False, default="P0")
    signature: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    signed_by: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    signed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    requires_disposable_env: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True,
    )
    linked_scenario_ids: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now, onupdate=_utc_now,
    )

    __table_args__ = (
        Index(
            "ix_qec_certified_invariants_tenant_app",
            "tenant_id", "app_id",
        ),
    )


# ── qec_universe_baselines (Stage-1: approval.py) ──────────────────────────
class UniverseBaselineRow(QecBase):
    """One e-signed, hash-chained approved-universe baseline.

    ``atoms_hash`` = sha256 over the sorted canonical_keys of the approved
    universe — the shrinkage-guard anchor.  Chain is per ``(tenant_id,
    app_id)`` (same recipe as :class:`ApprovalEventRow`).  A baseline without
    a ``signature`` has no honest meaning → the appender refuses (422).
    """

    __tablename__ = "qec_universe_baselines"

    baseline_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    app_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    atoms_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    atom_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    signature: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    signed_by: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    chain_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now,
    )

    __table_args__ = (
        Index(
            "ix_qec_universe_baselines_tenant_app",
            "tenant_id", "app_id", "created_at",
        ),
    )


# ── qec_coverage_gaps (Stage-1: coverage.py) ───────────────────────────────
class CoverageGapRow(QecBase):
    """One named coverage gap.

    ``kind`` ∈ possible_deletion | uncovered_atom | tier_gap |
    unclassified_fail_up.  ``possible_deletion`` is ALWAYS band P0 and blocks
    the "all green" verdict until adjudicated or waived.  ``status`` ∈ open |
    adjudicated | waived — a waiver ANNOTATES (fills ``waiver`` +
    ``waiver_expires_at``, flips ``status``), it never deletes the row; and it
    stops applying automatically at ``waiver_expires_at`` (≤ 90d).
    """

    __tablename__ = "qec_coverage_gaps"

    gap_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    app_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    band: Mapped[str] = mapped_column(String(8), nullable=False, default="P0")
    detail: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="open")
    waiver: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    waiver_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now, onupdate=_utc_now,
    )

    __table_args__ = (
        Index(
            "ix_qec_coverage_gaps_tenant_app_status",
            "tenant_id", "app_id", "status",
        ),
    )


# ── qec_case_tiers (Stage-2 owner: tier_label.py; coverage.py reads) ───────
class CaseTierRow(QecBase):
    """One RENDERS-vs-BEHAVES tier label for one test case.

    Natural PK ``(tenant_id, artifact_id, test_id)``.  ``tier`` ∈ renders |
    behaves; the suite label is the MIN tier (fail-down).  Lives here because
    HONEST-10 has no behavioral axis and VKPower stays untouched.
    """

    __tablename__ = "qec_case_tiers"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    artifact_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    test_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tier: Mapped[str] = mapped_column(String(16), nullable=False, default="renders")
    evidence: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now,
    )


# ── qec_touch_events (Stage-2 owner: touch_meter.py) ───────────────────────
class TouchEventRow(QecBase):
    """One typed human touch — the autonomy-KPI numerator.

    ``source`` ∈ qec_direct | vkpower_audit; ``source_ref`` = ``audit_log``
    log_id for ingested rows (NULL for direct touches) — the partial unique
    index ``uq_qec_touch_events_source_ref`` dedupes ingest.
    """

    __tablename__ = "qec_touch_events"

    touch_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    app_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    touch_type: Mapped[str] = mapped_column(String(40), nullable=False)
    band: Mapped[str] = mapped_column(String(8), nullable=False, default="")
    cycle_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="qec_direct")
    source_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    actor: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now,
    )

    __table_args__ = (
        Index(
            "ix_qec_touch_events_tenant_app_created",
            "tenant_id", "app_id", "created_at",
        ),
        # Dedupe ingested VKPower audit rows only (NULL source_ref = direct
        # touches, never deduped) — the migration's partial unique index.
        Index(
            "uq_qec_touch_events_source_ref",
            "tenant_id", "source", "source_ref",
            unique=True,
            postgresql_where=text("source_ref IS NOT NULL"),
        ),
    )
