"""Add test_run + test_run_step tables — P6 Execution Feedback Loop.

CI/CD systems post one row per execution to ``test_run``, with one
``test_run_step`` per step inside each scenario.  Together they give the
Test Studio:

  * Per-scenario last-run status, duration, error
  * Flake detection (pass-then-fail-then-pass within a window)
  * Selector drift detection — compare ``resolved_selector`` and
    ``resolved_bbox_json`` against the live visual evidence graph
  * Root-cause hints — derived from the diff between failing-run and
    last-passing-run state

A separate ``selector_resolution_history`` table is intentionally NOT
created — drift is derived on the fly from ``test_run_step`` rows.

Scenario linkage:
    Test names emitted by the Playwright / Cypress exporters embed the
    scenario_id (e.g. ``test('vs_001 — Happy path', ...)``).  The ingest
    webhook extracts it via regex so CI doesn't need to know any internal
    IDs.

Revision ID: 023_test_runs
Revises: 022_e2e_scenario_state
Create Date: 2026-05-13
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "023_test_runs"
down_revision: Union[str, None] = "022_e2e_scenario_state"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TENANT_TABLES_THIS_MIGRATION: tuple[str, ...] = (
    "test_run",
    "test_run_step",
)


def upgrade() -> None:
    # ── test_run ──────────────────────────────────────────────────────
    op.create_table(
        "test_run",
        sa.Column("run_id", sa.String(64), primary_key=True),
        sa.Column(
            "artifact_id",
            sa.String(64),
            sa.ForeignKey("canonical_artifacts.artifact_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        # External CI identifiers — opaque to us, used for traceability.
        sa.Column("ci_run_id", sa.String(200), nullable=False, server_default=""),
        sa.Column("ci_commit_sha", sa.String(64), nullable=False, server_default=""),
        sa.Column("ci_pipeline_url", sa.String(1000), nullable=False, server_default=""),
        sa.Column("environment", sa.String(64), nullable=False, server_default="ci"),
        # Aggregate run status. One of:
        #   running | passed | failed | error | cancelled
        sa.Column("status", sa.String(20), nullable=False, server_default="running"),
        sa.Column("total_steps", sa.Integer, nullable=False, server_default="0"),
        sa.Column("passed_steps", sa.Integer, nullable=False, server_default="0"),
        sa.Column("failed_steps", sa.Integer, nullable=False, server_default="0"),
        sa.Column("skipped_steps", sa.Integer, nullable=False, server_default="0"),
        sa.Column("duration_ms", sa.Integer, nullable=False, server_default="0"),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "completed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "metadata_json",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.create_index(
        "ix_test_run_artifact_started",
        "test_run",
        ["artifact_id", "started_at"],
    )
    op.create_index(
        "ix_test_run_tenant_started",
        "test_run",
        ["tenant_id", "started_at"],
    )
    op.create_index(
        "ix_test_run_ci_run_id",
        "test_run",
        ["tenant_id", "ci_run_id"],
    )

    # ── test_run_step ─────────────────────────────────────────────────
    op.create_table(
        "test_run_step",
        sa.Column("step_run_id", sa.String(64), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(64),
            sa.ForeignKey("test_run.run_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "artifact_id",
            sa.String(64),
            sa.ForeignKey("canonical_artifacts.artifact_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        # The scenario_id is what we embed in exported test names
        # (e.g. "vs_001"). We do NOT FK to a relational table because
        # scenarios live in e2e_architect_cache JSON.
        sa.Column("scenario_id", sa.String(64), nullable=False),
        sa.Column("step_number", sa.Integer, nullable=False, server_default="0"),
        # passed | failed | skipped | timed_out | broken
        sa.Column("status", sa.String(20), nullable=False, server_default="passed"),
        sa.Column("duration_ms", sa.Integer, nullable=False, server_default="0"),
        # Selector drift fields — populated by the test reporter when
        # available, blank otherwise. Drift is computed by comparing these
        # to the live visual evidence graph.
        sa.Column("expected_selector", sa.String(1000), nullable=False, server_default=""),
        sa.Column("resolved_selector", sa.String(1000), nullable=False, server_default=""),
        sa.Column(
            "resolved_bbox_json",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        # Evidence linkage — copied verbatim from the test step at
        # execution time so we can detect when the graph has shifted
        # since the run.
        sa.Column("evidence_scene_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("evidence_control_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("evidence_edge_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("error_message", sa.Text, nullable=False, server_default=""),
        sa.Column("screenshot_url", sa.String(2000), nullable=False, server_default=""),
        sa.Column(
            "metadata_json",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_test_run_step_run",
        "test_run_step",
        ["run_id"],
    )
    op.create_index(
        "ix_test_run_step_scenario",
        "test_run_step",
        ["artifact_id", "scenario_id", "step_number"],
    )
    op.create_index(
        "ix_test_run_step_tenant",
        "test_run_step",
        ["tenant_id"],
    )

    # ── RLS ───────────────────────────────────────────────────────────
    for table in _TENANT_TABLES_THIS_MIGRATION:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;")
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table};")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
                USING (tenant_id = current_setting('nexus.current_tenant_id', true))
                WITH CHECK (tenant_id = current_setting('nexus.current_tenant_id', true));
            """
        )
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO nexus_app;")


def downgrade() -> None:
    for table in reversed(_TENANT_TABLES_THIS_MIGRATION):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table};")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY;")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;")

    op.drop_index("ix_test_run_step_tenant", table_name="test_run_step")
    op.drop_index("ix_test_run_step_scenario", table_name="test_run_step")
    op.drop_index("ix_test_run_step_run", table_name="test_run_step")
    op.drop_table("test_run_step")

    op.drop_index("ix_test_run_ci_run_id", table_name="test_run")
    op.drop_index("ix_test_run_tenant_started", table_name="test_run")
    op.drop_index("ix_test_run_artifact_started", table_name="test_run")
    op.drop_table("test_run")
