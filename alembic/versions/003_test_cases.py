"""Test case data model — test_cases, steps, preconditions, data workbook, export jobs

Revision ID: 003_test_cases
Revises: 002_platform_api
Create Date: 2026-03-24
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "003_test_cases"
down_revision: Union[str, None] = "002_platform_api"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── Test Cases ─────────────────────────────────────────
    op.create_table(
        "test_cases",
        sa.Column("test_case_id", sa.String(64), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.String(64),
            sa.ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "suite_id",
            sa.String(64),
            sa.ForeignKey("test_suites.suite_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("description", sa.Text, server_default=""),
        sa.Column("test_type", sa.String(30), server_default="e2e"),
        sa.Column("priority", sa.String(20), server_default="medium"),
        sa.Column("status", sa.String(30), server_default="draft"),
        sa.Column("version", sa.Integer, server_default="1"),
        sa.Column("target_systems", sa.JSON, server_default="[]"),
        sa.Column("validates_rules", sa.JSON, server_default="[]"),
        sa.Column("tags", sa.JSON, server_default="[]"),
        sa.Column("source_session_id", sa.String(64), nullable=True),
        sa.Column("source_speaker_id", sa.String(64), nullable=True),
        sa.Column("generated_by", sa.String(50), server_default="system"),
        sa.Column("approved_by", sa.String(200), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata_json", sa.JSON, server_default="{}"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_test_cases_tenant_status", "test_cases", ["tenant_id", "status"])
    op.create_index("ix_test_cases_tenant_type", "test_cases", ["tenant_id", "test_type"])
    op.create_index("ix_test_cases_suite", "test_cases", ["suite_id"])

    # ── Test Case Steps ────────────────────────────────────
    op.create_table(
        "test_case_steps",
        sa.Column("step_id", sa.String(64), primary_key=True),
        sa.Column(
            "test_case_id",
            sa.String(64),
            sa.ForeignKey("test_cases.test_case_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("step_number", sa.Integer, nullable=False),
        sa.Column("action", sa.Text, nullable=False),
        sa.Column("expected_result", sa.Text, server_default=""),
        sa.Column("target_system", sa.String(50), server_default="web"),
        sa.Column("target_element", sa.String(500), server_default=""),
        sa.Column("input_data_refs", sa.JSON, server_default="[]"),
        sa.Column("verification", sa.Text, server_default=""),
        sa.Column("screenshot_required", sa.Boolean, server_default=sa.text("false")),
        sa.Column("metadata_json", sa.JSON, server_default="{}"),
    )
    op.create_index(
        "ix_test_case_steps_case_num",
        "test_case_steps",
        ["test_case_id", "step_number"],
        unique=True,
    )

    # ── Test Case Preconditions ────────────────────────────
    op.create_table(
        "test_case_preconditions",
        sa.Column("precondition_id", sa.String(64), primary_key=True),
        sa.Column(
            "test_case_id",
            sa.String(64),
            sa.ForeignKey("test_cases.test_case_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sort_order", sa.Integer, server_default="0"),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("is_verified", sa.Boolean, server_default=sa.text("false")),
    )
    op.create_index("ix_preconditions_case", "test_case_preconditions", ["test_case_id"])

    # ── Data Workbook Entries ──────────────────────────────
    op.create_table(
        "data_workbook_entries",
        sa.Column("entry_id", sa.String(64), primary_key=True),
        sa.Column(
            "test_case_id",
            sa.String(64),
            sa.ForeignKey("test_cases.test_case_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sort_order", sa.Integer, server_default="0"),
        sa.Column("field_name", sa.String(200), nullable=False),
        sa.Column("field_value", sa.Text, server_default=""),
        sa.Column("field_type", sa.String(30), server_default="string"),
        sa.Column("is_sensitive", sa.Boolean, server_default=sa.text("false")),
        sa.Column("generator_hint", sa.String(100), server_default=""),
        sa.Column("metadata_json", sa.JSON, server_default="{}"),
    )
    op.create_index("ix_data_workbook_case", "data_workbook_entries", ["test_case_id"])
    op.create_index(
        "ix_data_workbook_field",
        "data_workbook_entries",
        ["test_case_id", "field_name"],
        unique=True,
    )

    # ── Export Jobs ────────────────────────────────────────
    op.create_table(
        "export_jobs",
        sa.Column("job_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("export_type", sa.String(20), server_default="excel"),
        sa.Column("scope", sa.String(30), server_default="test_case"),
        sa.Column("scope_id", sa.String(64), server_default=""),
        sa.Column("file_path", sa.String(500), server_default=""),
        sa.Column("file_size_bytes", sa.Integer, server_default="0"),
        sa.Column("record_count", sa.Integer, server_default="0"),
        sa.Column("status", sa.String(30), server_default="pending"),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("created_by", sa.String(200), server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_export_jobs_tenant", "export_jobs", ["tenant_id", "status"])


def downgrade() -> None:
    op.drop_table("export_jobs")
    op.drop_table("data_workbook_entries")
    op.drop_table("test_case_preconditions")
    op.drop_table("test_case_steps")
    op.drop_table("test_cases")
