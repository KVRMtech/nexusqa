"""Initial schema — users, tenants, workflows, audit

Revision ID: 001_initial
Revises: None
Create Date: 2026-03-22
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── Tenants ────────────────────────────────────────────
    op.create_table(
        "tenants",
        sa.Column("tenant_id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("domain", sa.String(200), nullable=False, unique=True),
        sa.Column("plan", sa.String(50), server_default="starter"),
        sa.Column("status", sa.String(20), server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # ── Users ──────────────────────────────────────────────
    op.create_table(
        "users",
        sa.Column("user_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.tenant_id", ondelete="CASCADE"), nullable=False),
        sa.Column("email", sa.String(320), nullable=False, unique=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("role", sa.String(20), server_default="viewer"),
        sa.Column("permissions", sa.JSON, server_default="[]"),
        sa.Column("password_hash", sa.String(256), nullable=False),
        sa.Column("is_active", sa.Boolean, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("last_login", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_users_tenant_email", "users", ["tenant_id", "email"])

    # ── Workflow Instances ─────────────────────────────────
    op.create_table(
        "workflow_instances",
        sa.Column("workflow_id", sa.String(64), primary_key=True),
        sa.Column("chain_id", sa.String(100), nullable=False),
        sa.Column("chain_name", sa.String(200), server_default=""),
        sa.Column("tenant_id", sa.String(64), nullable=False, index=True),
        sa.Column("session_id", sa.String(64), server_default=""),
        sa.Column("created_by", sa.String(200), server_default=""),
        sa.Column("status", sa.String(30), server_default="pending"),
        sa.Column("input_data", sa.JSON, server_default="{}"),
        sa.Column("stages", sa.JSON, server_default="{}"),
        sa.Column("timeline", sa.JSON, server_default="[]"),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text, nullable=True),
    )
    op.create_index("ix_workflow_tenant_status", "workflow_instances", ["tenant_id", "status"])

    # ── Workflow Contexts ──────────────────────────────────
    op.create_table(
        "workflow_contexts",
        sa.Column("workflow_id", sa.String(64), primary_key=True),
        sa.Column("snapshot", sa.JSON, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # ── Audit Log ──────────────────────────────────────────
    op.create_table(
        "audit_log",
        sa.Column("log_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False, index=True),
        sa.Column("engine", sa.String(30), nullable=False),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("entity_type", sa.String(50), server_default=""),
        sa.Column("entity_id", sa.String(100), server_default=""),
        sa.Column("user_id", sa.String(64), server_default=""),
        sa.Column("details", sa.JSON, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_audit_tenant_engine", "audit_log", ["tenant_id", "engine"])
    op.create_index("ix_audit_created", "audit_log", ["created_at"])

    # ── Reports ────────────────────────────────────────────
    op.create_table(
        "reports",
        sa.Column("report_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False, index=True),
        sa.Column("report_type", sa.String(50), nullable=False),
        sa.Column("title", sa.String(500), server_default=""),
        sa.Column("format", sa.String(10), server_default="html"),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("checksum", sa.String(128), server_default=""),
        sa.Column("metadata_json", sa.JSON, server_default="{}"),
        sa.Column("created_by", sa.String(200), server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_reports_tenant_type", "reports", ["tenant_id", "report_type"])

    # ── Shield Audit ───────────────────────────────────────
    op.create_table(
        "shield_audit",
        sa.Column("audit_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False, index=True),
        sa.Column("action", sa.String(30), nullable=False),
        sa.Column("entity_count", sa.Integer, server_default="0"),
        sa.Column("pii_types", sa.JSON, server_default="[]"),
        sa.Column("text_length", sa.Integer, server_default="0"),
        sa.Column("user_id", sa.String(64), server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("shield_audit")
    op.drop_table("reports")
    op.drop_table("audit_log")
    op.drop_table("workflow_contexts")
    op.drop_table("workflow_instances")
    op.drop_table("users")
    op.drop_table("tenants")
