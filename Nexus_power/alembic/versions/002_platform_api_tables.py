"""Platform API tables — sessions, SME profiles, contradictions, guardrails, tests, forge, compliance

Revision ID: 002_platform_api
Revises: 001_initial
Create Date: 2026-03-23
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "002_platform_api"
down_revision: Union[str, None] = "001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── Sessions ───────────────────────────────────────────
    op.create_table(
        "sessions",
        sa.Column("session_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("title", sa.String(500), server_default=""),
        sa.Column("status", sa.String(30), server_default="scheduled"),
        sa.Column("session_type", sa.String(50), server_default="knowledge_transfer"),
        sa.Column("sme_name", sa.String(200), server_default=""),
        sa.Column("duration_minutes", sa.Integer, server_default="0"),
        sa.Column("rules_extracted", sa.Integer, server_default="0"),
        sa.Column("confidence_score", sa.Float, server_default="0.0"),
        sa.Column("events", sa.JSON, server_default="[]"),
        sa.Column("transcript", sa.JSON, server_default="[]"),
        sa.Column("metadata_json", sa.JSON, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_sessions_tenant_status", "sessions", ["tenant_id", "status"])

    # ── SME Profiles ────────────────────────────────────────
    op.create_table(
        "sme_profiles",
        sa.Column("speaker_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("role", sa.String(100), server_default=""),
        sa.Column("department", sa.String(100), server_default=""),
        sa.Column("expertise_domains", sa.JSON, server_default="[]"),
        sa.Column("total_sessions", sa.Integer, server_default="0"),
        sa.Column("total_rules", sa.Integer, server_default="0"),
        sa.Column("avg_confidence", sa.Float, server_default="0.0"),
        sa.Column("last_session_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata_json", sa.JSON, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_sme_profiles_tenant", "sme_profiles", ["tenant_id"])

    # ── Contradictions ──────────────────────────────────────
    op.create_table(
        "contradictions",
        sa.Column("contradiction_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("rule_a_id", sa.String(64), server_default=""),
        sa.Column("rule_b_id", sa.String(64), server_default=""),
        sa.Column("description", sa.Text, server_default=""),
        sa.Column("severity", sa.String(20), server_default="medium"),
        sa.Column("status", sa.String(30), server_default="open"),
        sa.Column("resolution", sa.Text, server_default=""),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata_json", sa.JSON, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_contradictions_tenant_status", "contradictions", ["tenant_id", "status"])

    # ── Guardrail Pipeline ──────────────────────────────────
    op.create_table(
        "guardrail_pipeline",
        sa.Column("step_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("step_name", sa.String(200), nullable=False),
        sa.Column("step_order", sa.Integer, server_default="0"),
        sa.Column("status", sa.String(30), server_default="active"),
        sa.Column("threshold", sa.Float, server_default="0.7"),
        sa.Column("rules_processed", sa.Integer, server_default="0"),
        sa.Column("rules_passed", sa.Integer, server_default="0"),
        sa.Column("rules_flagged", sa.Integer, server_default="0"),
        sa.Column("metadata_json", sa.JSON, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_guardrail_pipeline_tenant", "guardrail_pipeline", ["tenant_id"])

    # ── Review Queue ────────────────────────────────────────
    op.create_table(
        "review_queue",
        sa.Column("review_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("entity_type", sa.String(50), server_default="rule"),
        sa.Column("entity_id", sa.String(64), server_default=""),
        sa.Column("reason", sa.Text, server_default=""),
        sa.Column("priority", sa.String(20), server_default="medium"),
        sa.Column("status", sa.String(30), server_default="pending"),
        sa.Column("assigned_to", sa.String(200), server_default=""),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata_json", sa.JSON, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_review_queue_tenant_status", "review_queue", ["tenant_id", "status"])

    # ── Trust Trend ─────────────────────────────────────────
    op.create_table(
        "trust_trend",
        sa.Column("trend_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("period", sa.String(20), nullable=False),
        sa.Column("avg_confidence", sa.Float, server_default="0.0"),
        sa.Column("rules_total", sa.Integer, server_default="0"),
        sa.Column("rules_verified", sa.Integer, server_default="0"),
        sa.Column("rules_rejected", sa.Integer, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_trust_trend_tenant", "trust_trend", ["tenant_id"])

    # ── Traces (Traceability Matrix) ────────────────────────
    op.create_table(
        "traces",
        sa.Column("trace_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("requirement_id", sa.String(100), server_default=""),
        sa.Column("requirement_text", sa.Text, server_default=""),
        sa.Column("rule_id", sa.String(100), server_default=""),
        sa.Column("test_case_id", sa.String(100), server_default=""),
        sa.Column("status", sa.String(30), server_default="linked"),
        sa.Column("coverage", sa.Float, server_default="0.0"),
        sa.Column("metadata_json", sa.JSON, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_traces_tenant", "traces", ["tenant_id"])

    # ── Test Suites ─────────────────────────────────────────
    op.create_table(
        "test_suites",
        sa.Column("suite_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text, server_default=""),
        sa.Column("test_count", sa.Integer, server_default="0"),
        sa.Column("pass_rate", sa.Float, server_default="0.0"),
        sa.Column("status", sa.String(30), server_default="active"),
        sa.Column("tags", sa.JSON, server_default="[]"),
        sa.Column("metadata_json", sa.JSON, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_test_suites_tenant", "test_suites", ["tenant_id"])

    # ── Test Runs ───────────────────────────────────────────
    op.create_table(
        "test_runs",
        sa.Column("run_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("suite_id", sa.String(64), server_default=""),
        sa.Column("status", sa.String(30), server_default="pending"),
        sa.Column("total_tests", sa.Integer, server_default="0"),
        sa.Column("passed", sa.Integer, server_default="0"),
        sa.Column("failed", sa.Integer, server_default="0"),
        sa.Column("skipped", sa.Integer, server_default="0"),
        sa.Column("duration_seconds", sa.Float, server_default="0.0"),
        sa.Column("environment", sa.String(100), server_default=""),
        sa.Column("metadata_json", sa.JSON, server_default="{}"),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_test_runs_tenant_status", "test_runs", ["tenant_id", "status"])

    # ── Forge Configs ───────────────────────────────────────
    op.create_table(
        "forge_configs",
        sa.Column("config_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("schema_definition", sa.JSON, server_default="{}"),
        sa.Column("record_count", sa.Integer, server_default="100"),
        sa.Column("format", sa.String(20), server_default="json"),
        sa.Column("status", sa.String(30), server_default="active"),
        sa.Column("metadata_json", sa.JSON, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_forge_configs_tenant", "forge_configs", ["tenant_id"])

    # ── Forge Results ───────────────────────────────────────
    op.create_table(
        "forge_results",
        sa.Column("result_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("config_id", sa.String(64), server_default=""),
        sa.Column("records_generated", sa.Integer, server_default="0"),
        sa.Column("format", sa.String(20), server_default="json"),
        sa.Column("file_path", sa.String(500), server_default=""),
        sa.Column("file_size_bytes", sa.Integer, server_default="0"),
        sa.Column("status", sa.String(30), server_default="completed"),
        sa.Column("metadata_json", sa.JSON, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_forge_results_tenant", "forge_results", ["tenant_id"])

    # ── Jurisdictions ───────────────────────────────────────
    op.create_table(
        "jurisdictions",
        sa.Column("jurisdiction_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("code", sa.String(20), server_default=""),
        sa.Column("region", sa.String(100), server_default=""),
        sa.Column("rules_count", sa.Integer, server_default="0"),
        sa.Column("compliance_score", sa.Float, server_default="0.0"),
        sa.Column("status", sa.String(30), server_default="active"),
        sa.Column("last_audit_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata_json", sa.JSON, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_jurisdictions_tenant", "jurisdictions", ["tenant_id"])


def downgrade() -> None:
    op.drop_table("jurisdictions")
    op.drop_table("forge_results")
    op.drop_table("forge_configs")
    op.drop_table("test_runs")
    op.drop_table("test_suites")
    op.drop_table("traces")
    op.drop_table("trust_trend")
    op.drop_table("review_queue")
    op.drop_table("guardrail_pipeline")
    op.drop_table("contradictions")
    op.drop_table("sme_profiles")
    op.drop_table("sessions")
