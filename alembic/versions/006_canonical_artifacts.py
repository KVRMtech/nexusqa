"""Add canonical_artifacts table and media_fingerprint index for re-upload deduplication.

Revision ID: 006_canonical_artifacts
Revises: 005_qi_portal
Create Date: 2026-03-29
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "006_canonical_artifacts"
down_revision: Union[str, None] = "005_qi_portal"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "canonical_artifacts",
        sa.Column("artifact_id", sa.String(64), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.String(64),
            sa.ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("media_fingerprint", sa.String(128), nullable=True),
        sa.Column("status", sa.String(30), server_default="pending"),

        # Probe metadata
        sa.Column("duration_seconds", sa.Float, server_default="0.0"),

        # Counts
        sa.Column("scene_count", sa.Integer, server_default="0"),
        sa.Column("frame_count", sa.Integer, server_default="0"),

        # Transcript (PII-safe)
        sa.Column("safe_transcript_text", sa.Text, server_default=""),

        # Visual
        sa.Column("visual_summary", sa.Text, server_default=""),
        sa.Column("application_types_seen", postgresql.JSON, server_default="[]"),

        # Quality gate
        sa.Column("brain_quality_score", sa.Float, nullable=True),
        sa.Column("quality_gate_passed", sa.Boolean, server_default=sa.text("false")),

        # Full artifact JSON (complete nested result for downstream consumers)
        sa.Column("full_artifact_json", postgresql.JSON, nullable=True),

        # Processing
        sa.Column("processing_time_seconds", sa.Float, server_default="0.0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text, nullable=True),
    )

    # Composite index for tenant+session lookups
    op.create_index(
        "ix_canonical_artifacts_tenant_session",
        "canonical_artifacts",
        ["tenant_id", "session_id"],
    )

    # Fingerprint index for re-upload deduplication
    op.create_index(
        "ix_canonical_artifacts_fingerprint",
        "canonical_artifacts",
        ["media_fingerprint"],
    )


def downgrade() -> None:
    op.drop_index("ix_canonical_artifacts_fingerprint", table_name="canonical_artifacts")
    op.drop_index("ix_canonical_artifacts_tenant_session", table_name="canonical_artifacts")
    op.drop_table("canonical_artifacts")
