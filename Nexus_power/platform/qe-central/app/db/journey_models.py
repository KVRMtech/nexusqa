"""ORM rows for the Journey Graph (alembic ``qec_005`` — Release C).

The graph is the EVOLUTION of the flow ledger, not a parallel model: a
``journeys`` row is keyed by the ledger's own entry fingerprint (its
``flow_id`` stays EQUAL to ``flow_ledger.flow_id_for`` output, so history
joins for free); each walked flow becomes a ``journey_traversals`` row; the
steps contribute ``journey_nodes`` and ``journey_edges``; every enumerated
option at a decision point becomes a ``journey_branches`` row — walked or
NOT. Unwalked is a record, not an absence.

All five tables are tenant+app scoped with RLS FORCED in the migration.
Rows carry labels, kinds, signatures, titles — UI shape. Values never enter
the graph; nothing here is cross-tenant.

Schema is managed by Alembic — these classes exist for typed queries (and
``QecBase.metadata.create_all`` in DB-backed tests).
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Index, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .models import QecBase


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class JourneyRow(QecBase):
    """One business journey per entry point (the ledger's flow identity)."""

    __tablename__ = "journeys"

    journey_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    app_id: Mapped[str] = mapped_column(String(64), nullable=False)
    entry_fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    #: EQUAL to ``flow_ledger.flow_id_for(entry_fingerprint)`` — the ledger's
    #: own id, so every historical flow joins the graph without migration.
    flow_id: Mapped[str] = mapped_column(String(64), nullable=False)
    entry_url: Mapped[str] = mapped_column(String(2000), nullable=False, default="")
    entry_title: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    #: Business name: agent-proposed, operator-owned. ``name_source`` walks
    #: fallback → agent → operator; an operator name is NEVER overwritten.
    business_name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    name_source: Mapped[str] = mapped_column(String(16), nullable=False, default="fallback")
    named_by: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    name_description: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    deepest_steps: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_proven_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now, onupdate=_utc_now)

    __table_args__ = (
        Index("uq_journeys_tenant_app_entry", "tenant_id", "app_id",
              "entry_fingerprint", unique=True),
    )


class JourneyNodeRow(QecBase):
    """One state the graph knows, keyed by its crawl fingerprint."""

    __tablename__ = "journey_nodes"

    node_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    app_id: Mapped[str] = mapped_column(String(64), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    url: Mapped[str] = mapped_column(String(2000), nullable=False, default="")
    title: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    #: The step offered at least one enumerable path-selecting control.
    is_decision: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: A commit-labeled/danger control was present — the submit boundary.
    is_boundary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: Outcome values (currency/decision/percent) were displayed here.
    has_outcome: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: Not observed by the app's latest fold — kept (history), marked, and
    #: excluded from active planning. Never deleted.
    stale: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now)

    __table_args__ = (
        Index("uq_journey_nodes_tenant_app_fp", "tenant_id", "app_id",
              "fingerprint", unique=True),
    )


class JourneyEdgeRow(QecBase):
    """One observed distinct transition (from → to via a named trigger)."""

    __tablename__ = "journey_edges"

    edge_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    app_id: Mapped[str] = mapped_column(String(64), nullable=False)
    from_fp: Mapped[str] = mapped_column(String(128), nullable=False)
    to_fp: Mapped[str] = mapped_column(String(128), nullable=False)
    trigger_label_norm: Mapped[str] = mapped_column(String(200), nullable=False)
    #: WHO decided the advance that walked this edge (Release B P3 evidence):
    #: 1/2 = deterministic regex, 3 = agent oracle, 0 = pre-evidence manifest.
    advance_tier: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    walk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    first_walked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now)
    last_walked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now)

    __table_args__ = (
        Index("uq_journey_edges_identity", "tenant_id", "app_id", "from_fp",
              "to_fp", "trigger_label_norm", unique=True),
    )


class JourneyTraversalRow(QecBase):
    """One walked path — the graph's index into the crawl's manifest evidence."""

    __tablename__ = "journey_traversals"

    traversal_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    app_id: Mapped[str] = mapped_column(String(64), nullable=False)
    journey_id: Mapped[str] = mapped_column(String(64), nullable=False)
    exploration_id: Mapped[str] = mapped_column(String(64), nullable=False)
    terminal: Mapped[str] = mapped_column(String(40), nullable=False)
    #: Copied from the ledger flow — still DERIVED at source, never asserted.
    completed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    fully_answered: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: Ordered fingerprints of the walked path.
    path_fps: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    #: Hash over path_fps — the traversal's dedup identity within a crawl.
    path_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    #: The identity that walked (member ref / synthetic seed label; C4 fills
    #: it for planned branch walks) and the environment it ran against.
    identity_ref: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    env_ref: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    #: Outcome values the funnel produced (label/value/value_type — evidence
    #: the branch proof compares: a different premium IS a different path).
    outcome_values: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    #: Folded from a manifest produced before the Release-A hardening gates.
    pre_hardening: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now)

    __table_args__ = (
        Index("uq_journey_traversals_identity", "tenant_id", "app_id",
              "exploration_id", "journey_id", "path_hash", unique=True),
        Index("ix_journey_traversals_journey", "tenant_id", "app_id", "journey_id"),
    )


class JourneyBranchRow(QecBase):
    """One enumerated option at a decision node — walked or NOT (first-class)."""

    __tablename__ = "journey_branches"

    branch_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    app_id: Mapped[str] = mapped_column(String(64), nullable=False)
    node_fp: Mapped[str] = mapped_column(String(128), nullable=False)
    control_signature: Mapped[str] = mapped_column(String(64), nullable=False)
    control_label_norm: Mapped[str] = mapped_column(String(200), nullable=False)
    option_label_norm: Mapped[str] = mapped_column(String(200), nullable=False)
    #: walked | discovered | planned | blocked. ``walked`` never downgrades;
    #: ``blocked`` carries its attributed reason and is surfaced, never
    #: silently retried.
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="discovered")
    blocked_reason: Mapped[str] = mapped_column(String(400), nullable=False, default="")
    walked_in_traversal: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    last_status_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now)

    __table_args__ = (
        Index("uq_journey_branches_identity", "tenant_id", "app_id", "node_fp",
              "control_signature", "option_label_norm", unique=True),
        Index("ix_journey_branches_status", "tenant_id", "app_id", "status"),
    )


__all__ = [
    "JourneyRow", "JourneyNodeRow", "JourneyEdgeRow",
    "JourneyTraversalRow", "JourneyBranchRow",
]
