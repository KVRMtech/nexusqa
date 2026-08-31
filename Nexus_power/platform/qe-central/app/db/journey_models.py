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

from sqlalchemy import Boolean, DateTime, Index, Integer, String, text
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
    #: O0 baseline lifecycle: captured → approved → validated → drifted.
    #: Everything without an approved baseline wears "captured" openly.
    baseline_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="captured")
    baseline_traversal_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default="")
    baseline_outcome_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, default="")
    baseline_snapshot: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True)
    baseline_approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    baseline_approved_by: Mapped[str] = mapped_column(
        String(200), nullable=False, default="")
    drift_traversal_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default="")
    drift_detected_at: Mapped[datetime | None] = mapped_column(
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
    #: E3 catalog — the full control inventory observed at this node.  Each
    #: entry: {name, type, signature, options, required, depends_on,
    #: semantic_type}.  Merged across visits; provenance is query-time.
    controls_inventory: Mapped[list | None] = mapped_column(
        JSONB, nullable=True)
    #: E3 catalog — outcome display locations observed at this node.  Each
    #: entry: {label, selector, value_type}.
    displayed_outcomes: Mapped[list | None] = mapped_column(
        JSONB, nullable=True)
    #: M2.4 / T-GEN-03 (qec_021) — THE ENDPOINT MAP for this state: the
    #: value-free ``[{method, path, status, response_mime}]`` of the API calls
    #: observed while it was open, 2xx only. Narrow ON PURPOSE — this is what a
    #: compiler turns into an assertion, and compiling an observed 5xx would
    #: freeze the application's bug into the regression suite as the behaviour
    #: it demands. The FULL account (every status, every retry, the auth pattern,
    #: the response shape) is the M2.5 endpoint inventory, which is a different
    #: artifact for a different reader.
    observed_endpoints: Mapped[list | None] = mapped_column(JSONB, nullable=True)
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
    #: M2.4 / T-GEN-03 (qec_021) — the endpoints the crawl RECORDED this
    #: trigger firing (M2.5 stamps the in-flight UI action on every network
    #: event; the fold joins on it here). An EDGE is the right home because an
    #: edge IS the UI step: "which click caused this POST" is a property of the
    #: transition, not of either state it connects. Empty when the crawl
    #: predates the stamp — the compiler then falls back to the state-difference
    #: inference and says which rule it used.
    observed_endpoints: Mapped[list | None] = mapped_column(JSONB, nullable=True)
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
    #: P1 trigger→child (qec_011): value-free control identities (``kind:name``)
    #: this option ACTIVATED when walked — a "Yes" that reveals a detail block
    #: stores them, a "No" that reveals nothing stores none. Merged (union) across
    #: crawls so the Yes-side and No-side reveals both accumulate. Null until a
    #: discovery walk records it.
    reveals: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    last_status_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now)

    # ONE BRANCH ROW IS ONE ANSWER of a questionnaire question, and every choice
    # question in the Master Catalog is built from these rows. The lifecycle
    # lives here as well as on the node inventory because without it a withdrawn
    # Yes/No would be resurrected as active by the branch fold-in the moment the
    # node side retired it. Kept on a SEPARATE axis from ``status``: ``walked``
    # is a fact about a past crawl and must never downgrade, while retirement is
    # a statement about the application today.
    # ── M2.3 · LIFECYCLE (qec_020) ───────────────────────────────────────
    #: Previously known, NOT observed by the crawl that last looked for it.
    #: Reversible: an application that asks the question again clears it.
    stale: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: When the absence became CONCLUSIVE. NULL while live — the presence of
    #: this value IS the retirement, so there is one place to look and no way
    #: for a flag and a timestamp to disagree.
    retired_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    #: WHICH crawl retired it — the audit answer to "on whose evidence?".
    retired_in_crawl: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    #: ``conclusive_absence`` | ``repeated_absence``. See
    #: :func:`app.services.catalog.apply_control_lifecycle`.
    retire_reason: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    #: How many crawls have looked and not found it. Kept after retirement: it
    #: is the evidence trail behind the stamp.
    missed_crawls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: The last crawl that DID observe it. Distinct from ``last_seen_artifact``,
    #: which the pre-M2.3 upsert bumped on every fold whether it saw the question
    #: or not, and which therefore cannot answer "when did we last SEE it".
    last_seen_crawl: Mapped[str] = mapped_column(String(64), nullable=False, default="")

    __table_args__ = (
        Index("uq_journey_branches_identity", "tenant_id", "app_id", "node_fp",
              "control_signature", "option_label_norm", unique=True),
        Index("ix_journey_branches_status", "tenant_id", "app_id", "status"),
    )


class CatalogQuestionRow(QecBase):
    """One question in the app-scoped Master Catalog (qec_012, P2).

    Deduped by the stable value-free ``question_id`` across every journey/node —
    the 400 questions live once, not per journey. Holds question TEXT (``name``,
    ``business_rule``) so the table is RLS-forced in the migration."""

    __tablename__ = "catalog_questions"

    cq_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    app_id: Mapped[str] = mapped_column(String(64), nullable=False)
    question_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    answer_type: Mapped[str] = mapped_column(String(40), nullable=False, default="text")
    required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    options: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    validation: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    business_rule: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    # ── M2.2 (qec_019) — the evidence the catalogue could not previously hold ──
    #: ``observed`` only when a crawl EXPERIMENT proved the rule; ``UNVERIFIED``
    #: otherwise, written explicitly so an empty ``business_rule`` is never
    #: ambiguous between "this question gates nothing" and "nobody looked".
    business_rule_state: Mapped[str] = mapped_column(
        String(24), nullable=False, default="UNVERIFIED")
    #: Which experiment proved it and which control it gates. Kept beside the
    #: sentence, never inside it: the sentence is the record of what was observed
    #: and must stay verbatim.
    business_rule_evidence: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    #: The question whose answer this one hangs off (ACT-THEN-DIFF proven).
    depends_on: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # ── Tier 2 (qec_024) — WHERE THAT DEPENDENCY CAME FROM ──────────────
    #: ``declared`` (the page states it) | ``proven_reveal`` (a crawl answered
    #: the trigger and the child appeared) | ``""`` (nothing observed). Two
    #: columns rather than one because the two are not the same claim, and a
    #: declared dependency is never overwritten by a reveal — they can disagree,
    #: and the disagreement is the part worth reading.
    depends_on_source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="")
    #: The evidence behind ``proven_reveal``: which questions, answered with
    #: which options, were observed to reveal this one. Bounded by
    #: ``catalog.MAX_REVEALED_BY``; ``revealed_by`` on the built catalogue also
    #: carries a total so a clipped list stays visibly clipped.
    revealed_by: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    #: The handle the page declared for the control + whether it resolves to one
    #: element. Never a selector this service composed.
    locator: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    #: How many answers the control offers in the page; greater than
    #: ``len(options)`` exactly when the read was clipped.
    options_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expected_next_page: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    semantic_type: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    provenance: Mapped[str] = mapped_column(String(24), nullable=False, default="observed")
    pages: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    first_seen_artifact: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    last_seen_artifact: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now)

    # A RETIRED QUESTION KEEPS EVERYTHING ABOVE. Its id, its content, its
    # first-seen record and its pages all survive retirement untouched, because
    # the whole point of retiring rather than deleting is that "what did this
    # application used to ask, and when did it stop?" stays answerable.
    # ── M2.3 · LIFECYCLE (qec_020) ───────────────────────────────────────
    #: Previously known, NOT observed by the crawl that last looked for it.
    #: Reversible: an application that asks the question again clears it.
    stale: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: When the absence became CONCLUSIVE. NULL while live — the presence of
    #: this value IS the retirement, so there is one place to look and no way
    #: for a flag and a timestamp to disagree.
    retired_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    #: WHICH crawl retired it — the audit answer to "on whose evidence?".
    retired_in_crawl: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    #: ``conclusive_absence`` | ``repeated_absence``. See
    #: :func:`app.services.catalog.apply_control_lifecycle`.
    retire_reason: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    #: How many crawls have looked and not found it. Kept after retirement: it
    #: is the evidence trail behind the stamp.
    missed_crawls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: The last crawl that DID observe it. Distinct from ``last_seen_artifact``,
    #: which the pre-M2.3 upsert bumped on every fold whether it saw the question
    #: or not, and which therefore cannot answer "when did we last SEE it".
    last_seen_crawl: Mapped[str] = mapped_column(String(64), nullable=False, default="")

    __table_args__ = (
        Index("uq_catalog_questions_identity", "tenant_id", "app_id",
              "question_id", unique=True),
        # Active planning asks for the NON-retired questions of one app; the
        # partial index is what keeps that off a full scan of every question the
        # tenant has ever catalogued. Mirrors qec_020.
        Index("ix_catalog_questions_active", "tenant_id", "app_id",
              postgresql_where=text("retired_at IS NULL")),
    )


class CatalogVersionRow(QecBase):
    """A Master Catalog snapshot per crawl (qec_012, P2) — what P6 diffs.

    A re-crawl mints a new ``artifact_id``; snapshotting the deduped catalog per
    artifact lets a later crawl diff by stable ``question_id`` to flag an added
    question, a moved branch, or a broken rule."""

    __tablename__ = "catalog_versions"

    version_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    app_id: Mapped[str] = mapped_column(String(64), nullable=False)
    artifact_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    question_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now)

    __table_args__ = (
        Index("uq_catalog_versions_identity", "tenant_id", "app_id",
              "artifact_id", unique=True),
    )


class PersonaRow(QecBase):
    """A declared answer profile (qec_013, P3) — references the platform-api
    ``tp_personas`` row. RLS-forced (persona names can carry business context)."""

    __tablename__ = "personas"

    persona_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    app_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    source_ref: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    #: P3 (qec_014): the persona's declared answers ({question_id|name: option})
    #: — decision-level option labels the projector consumes. Values stay in the
    #: tenant; no cross-service egress.
    answers: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now)

    __table_args__ = (
        Index("uq_personas_identity", "tenant_id", "app_id", "persona_id",
              unique=True),
    )


class PersonaJourneyRow(QecBase):
    """One persona's projected (and optionally proven) journey over the Master
    Catalog (qec_013, P3). ``provenance`` is ``inferred`` until a verifying
    traversal confirms it, then ``live_confirmed`` — never green-washed."""

    __tablename__ = "persona_journeys"

    persona_journey_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    app_id: Mapped[str] = mapped_column(String(64), nullable=False)
    persona_id: Mapped[str] = mapped_column(String(64), nullable=False)
    journey_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    path_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    executed: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    activated: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    skipped: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    provenance: Mapped[str] = mapped_column(String(24), nullable=False, default="inferred")
    verified_traversal_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now)

    __table_args__ = (
        Index("uq_persona_journeys_identity", "tenant_id", "app_id",
              "persona_id", "journey_id", unique=True),
    )


__all__ = [
    "JourneyRow", "JourneyNodeRow", "JourneyEdgeRow",
    "JourneyTraversalRow", "JourneyBranchRow",
    "CatalogQuestionRow", "CatalogVersionRow",
    "PersonaRow", "PersonaJourneyRow",
]
