"""QE-Central initial schema — ALL 21 qecentral-DB tables (design R-7).

Creates, with ENABLE + FORCE row-level security and the standard
``tenant_isolation`` policy on EVERY table (pattern pinned by VKPower
migrations 010_row_level_security.py + 034_page_visits.py):

  S1 (§3.1):  client_apps, qe_explorations, qe_harness_runs
  S4 (§3.4):  qec_criticality_registry, qec_scenarios, qec_approval_events,
              qec_coverage_atoms, qec_certified_invariants,
              qec_universe_baselines, qec_coverage_gaps, qec_case_tiers,
              qec_touch_events
  S5 (§3.5):  app_cycles (one-ACTIVE-cycle partial unique index),
              change_events, app_fingerprints, cost_ledger
  S3 (§3.3):  repo_connections, app_model_universes, app_model_atoms,
              crawl_seed_manifests, repo_drift_reports

The ground_truth_events lesson (a table shipped with NO migration) is
structurally impossible here: this chain is the ONLY way qecentral tables
come into existence.

Revision ID: qec_001
Revises:
Create Date: 2026-07-08
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers
revision: str = "qec_001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _ts(name: str) -> sa.Column:
    """Standard UTC timestamptz column defaulting to now()."""
    return sa.Column(
        name, sa.DateTime(timezone=True), nullable=False,
        server_default=sa.text("now()"),
    )


# Every table QE-Central owns — RLS is applied to ALL of them below.
_ALL_TABLES = [
    # S1
    "client_apps", "qe_explorations", "qe_harness_runs",
    # S4
    "qec_criticality_registry", "qec_scenarios", "qec_approval_events",
    "qec_coverage_atoms", "qec_certified_invariants", "qec_universe_baselines",
    "qec_coverage_gaps", "qec_case_tiers", "qec_touch_events",
    # S5
    "app_cycles", "change_events", "app_fingerprints", "cost_ledger",
    # S3
    "repo_connections", "app_model_universes", "app_model_atoms",
    "crawl_seed_manifests", "repo_drift_reports",
]

# app_cycles states that terminate a cycle; anything else counts as ACTIVE
# for the one-active-cycle-per-app partial unique index (§3.5 — restart-safe
# without advisory locks). blackout_deferred is deliberately NON-terminal:
# a deferred cycle still owns the app slot until it resumes or fails.
_TERMINAL_CYCLE_STATES = "('done', 'failed', 'budget_stopped')"


def upgrade() -> None:
    # ───────────────────────── S1 — core service ─────────────────────────
    op.create_table(
        "client_apps",
        sa.Column("app_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("base_url", sa.String(2000), nullable=False),
        sa.Column("canonical_host", sa.String(500), nullable=False, server_default=""),
        # KMS envelope (EnvelopeBlob.to_bytes(), AAD=app_id); NULL = no creds.
        sa.Column("creds_blob", sa.LargeBinary, nullable=True),
        sa.Column("answer_key", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("env_attestation", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("fences", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("repo_binding", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("schedule", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("budgets", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("latest_artifact_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("baseline_fingerprint_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        _ts("created_at"),
        _ts("updated_at"),
    )
    op.create_index("ix_client_apps_tenant_host", "client_apps", ["tenant_id", "canonical_host"])
    op.create_index("ix_client_apps_tenant_status", "client_apps", ["tenant_id", "status"])

    op.create_table(
        "qe_explorations",
        sa.Column("exploration_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("app_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("artifact_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("session_id", sa.String(64), nullable=False, server_default=""),
        # pending | writing | completed | failed | refused (first-class)
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("explorer_version", sa.String(100), nullable=False, server_default=""),
        # The exact string written on every substrate row: qec_live_v1@{crawl_id}
        sa.Column("extractor_version", sa.String(64), nullable=False, server_default=""),
        sa.Column("stats", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("error", sa.Text, nullable=False, server_default=""),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
    )
    op.create_index("ix_qe_explorations_tenant_app", "qe_explorations", ["tenant_id", "app_id"])
    op.create_index(
        "ix_qe_explorations_tenant_artifact", "qe_explorations", ["tenant_id", "artifact_id"],
    )

    op.create_table(
        "qe_harness_runs",
        sa.Column("harness_run_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("fixture_name", sa.String(200), nullable=False, server_default=""),
        # R1..R8 or 'baseline'
        sa.Column("rule_id", sa.String(16), nullable=False, server_default=""),
        sa.Column("expected", sa.Text, nullable=False, server_default=""),
        sa.Column("observed", sa.Text, nullable=False, server_default=""),
        # REFUSED_CORRECTLY | GREEN_WASH_DETECTED | CHAIN_ERROR | PASS_BASELINE
        sa.Column("verdict", sa.String(32), nullable=False, server_default=""),
        sa.Column("report", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        _ts("created_at"),
    )
    op.create_index(
        "ix_qe_harness_runs_tenant_created", "qe_harness_runs", ["tenant_id", "created_at"],
    )

    # ─────────────────── S4 — scenario governance ("the 1%") ─────────────
    op.create_table(
        "qec_criticality_registry",
        sa.Column("registry_version", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        # {signals: [{signal_id, band, applies_to, matchers, rationale}]}
        sa.Column("pack", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("active", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("created_by", sa.String(200), nullable=False, server_default=""),
        _ts("created_at"),
    )
    op.create_index(
        "ix_qec_criticality_registry_tenant_active",
        "qec_criticality_registry", ["tenant_id", "active"],
    )

    op.create_table(
        "qec_scenarios",
        # uuid5(app_id + journey skeleton) — stable identity across re-crawls.
        sa.Column("scenario_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("app_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("source_artifact_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("name", sa.String(500), nullable=False, server_default=""),
        # Ordered, value-free, locator-free journey skeleton (§3.4).
        sa.Column("journey", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("criticality_band", sa.String(8), nullable=False, server_default="P1"),
        sa.Column("criticality_evidence", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("registry_version", sa.String(64), nullable=False, server_default=""),
        sa.Column("fingerprint", sa.String(64), nullable=False, server_default=""),
        # new | changed | unchanged | missing
        sa.Column("diff_state", sa.String(16), nullable=False, server_default="new"),
        sa.Column("review", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("approved_snapshot", JSONB, nullable=True),
        # renders | behaves | unlabeled
        sa.Column("tier", sa.String(16), nullable=False, server_default="unlabeled"),
        sa.Column("materialized_artifact_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        _ts("created_at"),
        _ts("updated_at"),
    )
    op.create_index("ix_qec_scenarios_tenant_app", "qec_scenarios", ["tenant_id", "app_id"])
    op.create_index(
        "ix_qec_scenarios_tenant_app_diff", "qec_scenarios",
        ["tenant_id", "app_id", "diff_state"],
    )

    op.create_table(
        "qec_approval_events",
        sa.Column("event_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("subject_kind", sa.String(32), nullable=False),
        sa.Column("subject_id", sa.String(64), nullable=False),
        # submit | approve | reject | reopen | carry_forward
        sa.Column("action", sa.String(32), nullable=False, server_default=""),
        sa.Column("payload", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        # Typed e-signature (approve REQUIRES it — 422 otherwise).
        sa.Column("signature", sa.String(200), nullable=False, server_default=""),
        sa.Column("actor", sa.String(200), nullable=False, server_default=""),
        # carry_forward recorded but NOT a human touch (§3.4).
        sa.Column("carry_forward", sa.Boolean, nullable=False, server_default=sa.text("false")),
        # chain_hash = sha256(prev + canonical sorted-JSON payload) — the
        # verdict_events.py:66-113 recipe, per (tenant, subject_kind, subject_id).
        sa.Column("prev_hash", sa.String(64), nullable=False, server_default=""),
        sa.Column("chain_hash", sa.String(64), nullable=False, server_default=""),
        _ts("created_at"),
    )
    op.create_index(
        "ix_qec_approval_events_chain", "qec_approval_events",
        ["tenant_id", "subject_kind", "subject_id", "created_at"],
    )

    op.create_table(
        "qec_coverage_atoms",
        # uuid5 of canonical_key — idempotent across recomputes.
        sa.Column("atom_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("app_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("canonical_key", sa.String(1000), nullable=False, server_default=""),
        # route | api_endpoint | form_field | journey_edge | validator
        sa.Column("kind", sa.String(32), nullable=False),
        # crawl | repo | answer_key
        sa.Column("source", sa.String(32), nullable=False, server_default="crawl"),
        # G_DETERMINISTIC | G_LIVE_CONFIRMED | G_INFERRED
        sa.Column("provenance", sa.String(32), nullable=False, server_default="G_INFERRED"),
        sa.Column("evidence", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        _ts("first_seen"),
        _ts("last_seen"),
    )
    op.create_index(
        "ix_qec_coverage_atoms_tenant_app_kind", "qec_coverage_atoms",
        ["tenant_id", "app_id", "kind"],
    )

    op.create_table(
        "qec_certified_invariants",
        sa.Column("invariant_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("app_id", sa.String(64), nullable=False, server_default=""),
        # Human-authored P0 statement (e.g. underwriting ceiling).
        sa.Column("statement", sa.Text, nullable=False, server_default=""),
        sa.Column("criticality_band", sa.String(8), nullable=False, server_default="P0"),
        sa.Column("signature", sa.String(200), nullable=False, server_default=""),
        sa.Column("signed_by", sa.String(200), nullable=False, server_default=""),
        sa.Column("signed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "requires_disposable_env", sa.Boolean, nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column("linked_scenario_ids", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("status", sa.String(32), nullable=False, server_default="draft"),
        _ts("created_at"),
        _ts("updated_at"),
    )
    op.create_index(
        "ix_qec_certified_invariants_tenant_app", "qec_certified_invariants",
        ["tenant_id", "app_id"],
    )

    op.create_table(
        "qec_universe_baselines",
        sa.Column("baseline_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("app_id", sa.String(64), nullable=False, server_default=""),
        # sha256 over sorted canonical_keys — the shrinkage-guard anchor.
        sa.Column("atoms_hash", sa.String(64), nullable=False),
        sa.Column("atom_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("signature", sa.String(200), nullable=False, server_default=""),
        sa.Column("signed_by", sa.String(200), nullable=False, server_default=""),
        sa.Column("prev_hash", sa.String(64), nullable=False, server_default=""),
        sa.Column("chain_hash", sa.String(64), nullable=False, server_default=""),
        _ts("created_at"),
    )
    op.create_index(
        "ix_qec_universe_baselines_tenant_app", "qec_universe_baselines",
        ["tenant_id", "app_id", "created_at"],
    )

    op.create_table(
        "qec_coverage_gaps",
        sa.Column("gap_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("app_id", sa.String(64), nullable=False, server_default=""),
        # possible_deletion | uncovered_atom | tier_gap | unclassified_fail_up
        sa.Column("kind", sa.String(32), nullable=False),
        # possible_deletion is ALWAYS P0 and blocks the all-green verdict.
        sa.Column("band", sa.String(8), nullable=False, server_default="P0"),
        sa.Column("detail", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        # open | adjudicated | waived — waivers ANNOTATE, never delete.
        sa.Column("status", sa.String(32), nullable=False, server_default="open"),
        sa.Column("waiver", JSONB, nullable=True),
        # Waivers expire ≤90d (WaiverRow semantics, verdict_events.py:266-341).
        sa.Column("waiver_expires_at", sa.DateTime(timezone=True), nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
    )
    op.create_index(
        "ix_qec_coverage_gaps_tenant_app_status", "qec_coverage_gaps",
        ["tenant_id", "app_id", "status"],
    )

    op.create_table(
        "qec_case_tiers",
        # (tenant, artifact, test) natural PK (§3.4).
        sa.Column("tenant_id", sa.String(64), primary_key=True),
        sa.Column("artifact_id", sa.String(64), primary_key=True),
        sa.Column("test_id", sa.String(64), primary_key=True),
        # renders | behaves — suite label = MIN tier; fail-down.
        sa.Column("tier", sa.String(16), nullable=False, server_default="renders"),
        # The grounded assertions that EARNED the tier.
        sa.Column("evidence", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        _ts("computed_at"),
    )

    op.create_table(
        "qec_touch_events",
        sa.Column("touch_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("app_id", sa.String(64), nullable=False, server_default=""),
        # scenario_approve | invariant_author | gap_adjudicate | waiver_create |
        # credential_provision | answer_key_edit | heal_approve | case_review |
        # defect_confirm
        sa.Column("touch_type", sa.String(40), nullable=False),
        sa.Column("band", sa.String(8), nullable=False, server_default=""),
        # Opaque until the control plane fixes semantics (open decision 13).
        sa.Column("cycle_id", sa.String(64), nullable=False, server_default=""),
        # qec_direct | vkpower_audit
        sa.Column("source", sa.String(32), nullable=False, server_default="qec_direct"),
        # audit_log.log_id for vkpower_audit ingest (dedupe); NULL for direct.
        sa.Column("source_ref", sa.String(64), nullable=True),
        sa.Column("actor", sa.String(200), nullable=False, server_default=""),
        _ts("created_at"),
    )
    op.create_index(
        "ix_qec_touch_events_tenant_app_created", "qec_touch_events",
        ["tenant_id", "app_id", "created_at"],
    )
    # Dedupe on ingested audit rows only (NULL source_ref = direct touches).
    op.execute(
        "CREATE UNIQUE INDEX uq_qec_touch_events_source_ref "
        "ON qec_touch_events (tenant_id, source, source_ref) "
        "WHERE source_ref IS NOT NULL"
    )

    # ───────────────────────── S5 — control plane ─────────────────────────
    op.create_table(
        "app_cycles",
        sa.Column("cycle_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("app_id", sa.String(64), nullable=False),
        # webhook_repo | schedule | probe_drift | manual | full_floor
        sa.Column("trigger", sa.String(32), nullable=False),
        # pending→probing→selecting→crawling?→generating→running→healing→
        # verifying→done | failed | budget_stopped (terminal HONEST state) |
        # blackout_deferred
        sa.Column("state", sa.String(32), nullable=False, server_default="pending"),
        # {mode, changed_atoms, selected_test_ids, carried_forward, selection_reason}
        sa.Column("selected_scope", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        # {uncomputable_pages_treated_changed, vanished_pages_possible_deletion}
        sa.Column("honest_gaps", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("result", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("error", sa.Text, nullable=False, server_default=""),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
    )
    op.create_index(
        "ix_app_cycles_tenant_app_created", "app_cycles",
        ["tenant_id", "app_id", "created_at"],
    )
    # ONE ACTIVE cycle per app (§3.5) — restart-safe without advisory locks.
    op.execute(
        "CREATE UNIQUE INDEX uq_app_cycles_one_active_per_app "
        "ON app_cycles (app_id) "
        f"WHERE state NOT IN {_TERMINAL_CYCLE_STATES}"
    )

    op.create_table(
        "change_events",
        sa.Column("event_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("app_id", sa.String(64), nullable=False),
        # repo_sha | probe | manual | schedule_floor
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("payload", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("dedupe_key", sa.String(200), nullable=False),
        sa.Column("processed_cycle_id", sa.String(64), nullable=True),
        _ts("created_at"),
        sa.UniqueConstraint("app_id", "dedupe_key", name="uq_change_events_app_dedupe"),
    )
    op.create_index("ix_change_events_tenant_app", "change_events", ["tenant_id", "app_id"])

    op.create_table(
        "app_fingerprints",
        sa.Column("fingerprint_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("app_id", sa.String(64), nullable=False),
        sa.Column("graph_hash", sa.String(64), nullable=False, server_default=""),
        # {page_key → {structural_hash, control_fps, last_verified_at}}
        sa.Column("page_fingerprints", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        # Provenance: the artifact whose journey-graph seeded this baseline.
        sa.Column("source_artifact_id", sa.String(64), nullable=False, server_default=""),
        _ts("created_at"),
    )
    op.create_index(
        "ix_app_fingerprints_tenant_app_created", "app_fingerprints",
        ["tenant_id", "app_id", "created_at"],
    )

    op.create_table(
        "cost_ledger",
        sa.Column("entry_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("app_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("cycle_id", sa.String(64), nullable=False, server_default=""),
        # browser_seconds | llm_tokens_* | substrate_rows | wallclock_seconds |
        # runner_runs — RAW UNITS first; dollars only when configured.
        sa.Column("unit", sa.String(40), nullable=False),
        sa.Column("quantity", sa.Numeric(20, 6), nullable=False, server_default="0"),
        sa.Column("source_ref", sa.String(200), nullable=False, server_default=""),
        # NULLABLE by design — never invent dollars (§3.5 / open decision 5).
        sa.Column("unit_cost_usd", sa.Numeric(12, 6), nullable=True),
        _ts("created_at"),
    )
    op.create_index(
        "ix_cost_ledger_tenant_app_created", "cost_ledger",
        ["tenant_id", "app_id", "created_at"],
    )
    op.create_index("ix_cost_ledger_tenant_cycle", "cost_ledger", ["tenant_id", "cycle_id"])

    # ───────────────────────── S3 — repo-intel ────────────────────────────
    op.create_table(
        "repo_connections",
        sa.Column("connection_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("app_id", sa.String(64), nullable=False, server_default=""),
        # gitlab | github | generic_git
        sa.Column("provider", sa.String(32), nullable=False, server_default="gitlab"),
        sa.Column("base_url", sa.String(2000), nullable=False, server_default=""),
        sa.Column("project_path", sa.String(1000), nullable=False, server_default=""),
        sa.Column("default_branch", sa.String(200), nullable=False, server_default="main"),
        # EnvelopeBlob.to_bytes(), AAD=connection_id; DELETE zeroes it.
        sa.Column("encrypted_token", sa.LargeBinary, nullable=True),
        sa.Column("kek_id", sa.String(200), nullable=False, server_default=""),
        sa.Column("label", sa.String(200), nullable=False, server_default=""),
        # active | revoked | error
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("last_sync_sha", sa.String(64), nullable=False, server_default=""),
        sa.Column("last_error", sa.Text, nullable=False, server_default=""),
        _ts("created_at"),
        _ts("updated_at"),
    )
    op.create_index("ix_repo_connections_tenant", "repo_connections", ["tenant_id"])

    op.create_table(
        "app_model_universes",
        sa.Column("universe_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column(
            "connection_id", sa.String(64),
            sa.ForeignKey("repo_connections.connection_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("deployed_sha", sa.String(64), nullable=False, server_default=""),
        sa.Column("branch", sa.String(200), nullable=False, server_default=""),
        sa.Column("stack_fingerprint", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("extractor_versions", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        # PUBLISHED HONESTY: static-rule accuracy ceiling per atom kind.
        sa.Column("ceiling_bands", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        # building | ready | degraded | failed | superseded
        sa.Column("status", sa.String(32), nullable=False, server_default="building"),
        sa.Column("degraded_extractors", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("atom_count", sa.Integer, nullable=False, server_default="0"),
        _ts("created_at"),
        _ts("updated_at"),
    )
    op.create_index(
        "ix_app_model_universes_tenant_connection", "app_model_universes",
        ["tenant_id", "connection_id"],
    )
    # Idempotent re-analysis: unique (connection, sha, md5(extractor_versions)).
    op.execute(
        "CREATE UNIQUE INDEX uq_app_model_universes_identity "
        "ON app_model_universes (connection_id, deployed_sha, md5(extractor_versions::text))"
    )

    op.create_table(
        "app_model_atoms",
        sa.Column("atom_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column(
            "universe_id", sa.String(64),
            sa.ForeignKey("app_model_universes.universe_id", ondelete="CASCADE"),
            nullable=False,
        ),
        # route | api_endpoint | form | validator_rule | auth_step |
        # feature_flag | data_model | test_intent | nav_edge
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("value", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("provenance_path", sa.String(1000), nullable=False, server_default=""),
        sa.Column("provenance_line", sa.Integer, nullable=False, server_default="0"),
        sa.Column("provenance_sha", sa.String(64), nullable=False, server_default=""),
        # Verbatim, secret-scrubbed — the ONLY text the LLM lens may quote.
        sa.Column("quote", sa.String(500), nullable=False, server_default=""),
        sa.Column("extractor", sa.String(100), nullable=False, server_default=""),
        # Rule-band confidence — never LLM-scored.
        sa.Column("confidence", sa.Float, nullable=False, server_default="0.0"),
        sa.Column("source_tier", sa.String(32), nullable=False, server_default=""),
        _ts("created_at"),
    )
    op.create_index(
        "ix_app_model_atoms_tenant_universe_kind", "app_model_atoms",
        ["tenant_id", "universe_id", "kind"],
    )
    op.create_index(
        "ix_app_model_atoms_universe_kind", "app_model_atoms", ["universe_id", "kind"],
    )

    op.create_table(
        "crawl_seed_manifests",
        sa.Column("manifest_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column(
            "universe_id", sa.String(64),
            sa.ForeignKey("app_model_universes.universe_id", ondelete="CASCADE"),
            nullable=False,
        ),
        # {version:'seed-v1', ranked_routes, auth_recipe (NEVER creds), nav_edges}
        sa.Column("manifest", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        _ts("created_at"),
        _ts("updated_at"),
        # One row per universe (upsert).
        sa.UniqueConstraint("universe_id", name="uq_crawl_seed_manifests_universe"),
    )

    op.create_table(
        "repo_drift_reports",
        sa.Column("report_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column(
            "universe_id", sa.String(64),
            sa.ForeignKey("app_model_universes.universe_id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Soft ref into the nexus DB (validated at query time — cross-DB FK
        # impossible by design, R-7).
        sa.Column("artifact_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("summary", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        # [{kind route_in_code_unreachable|route_live_not_in_code|
        #   form_field_mismatch|validator_untested, code_side, live_side}]
        sa.Column("items", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        _ts("created_at"),
    )
    op.create_index(
        "ix_repo_drift_reports_tenant_universe", "repo_drift_reports",
        ["tenant_id", "universe_id"],
    )

    # ─── Row-Level Security on EVERY table (010/034 pattern) ─────────────
    # ENABLE + FORCE (owner included) + tenant_isolation policy keyed on the
    # nexus.current_tenant_id GUC; grant to the qec app role when it exists
    # (the role is created by scripts/qec_db_bootstrap.sql, R-7).
    for table in _ALL_TABLES:
        op.execute(f"""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT FROM information_schema.tables
                    WHERE table_schema = 'public' AND table_name = '{table}'
                ) THEN
                    ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
                    ALTER TABLE {table} FORCE ROW LEVEL SECURITY;

                    DROP POLICY IF EXISTS tenant_isolation ON {table};
                    CREATE POLICY tenant_isolation ON {table}
                        USING (tenant_id = current_setting('nexus.current_tenant_id', true))
                        WITH CHECK (tenant_id = current_setting('nexus.current_tenant_id', true));

                    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'qec') THEN
                        GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO qec;
                    END IF;
                END IF;
            END $$;
        """)


def downgrade() -> None:
    for table in _ALL_TABLES:
        op.execute(f"""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT FROM information_schema.tables
                    WHERE table_schema = 'public' AND table_name = '{table}'
                ) THEN
                    DROP POLICY IF EXISTS tenant_isolation ON {table};
                    ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY;
                    ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;
                END IF;
            END $$;
        """)

    # Reverse dependency order (children of FK parents first).
    op.drop_table("repo_drift_reports")
    op.drop_table("crawl_seed_manifests")
    op.drop_table("app_model_atoms")
    op.drop_table("app_model_universes")
    op.drop_table("repo_connections")
    op.drop_table("cost_ledger")
    op.drop_table("app_fingerprints")
    op.drop_table("change_events")
    op.drop_table("app_cycles")
    op.drop_table("qec_touch_events")
    op.drop_table("qec_case_tiers")
    op.drop_table("qec_coverage_gaps")
    op.drop_table("qec_universe_baselines")
    op.drop_table("qec_certified_invariants")
    op.drop_table("qec_coverage_atoms")
    op.drop_table("qec_approval_events")
    op.drop_table("qec_scenarios")
    op.drop_table("qec_criticality_registry")
    op.drop_table("qe_harness_runs")
    op.drop_table("qe_explorations")
    op.drop_table("client_apps")
