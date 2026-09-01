"""Add provenance and lifecycle fields to canonical_artifacts.

Phase 1.1: Canonical Contract Freeze — workflow_id, source_type,
source_filename, created_by, quality_gate_outcome.

Revision ID: 008_canonical_provenance
Revises: 007_mission_stage_workflow_id
Create Date: 2026-03-31
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "008_canonical_provenance"
down_revision: Union[str, None] = "007_mission_stage_workflow_id"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Provenance fields
    op.add_column(
        "canonical_artifacts",
        sa.Column("workflow_id", sa.String(64), nullable=True),
    )
    op.add_column(
        "canonical_artifacts",
        sa.Column("source_type", sa.String(50), nullable=True),
    )
    op.add_column(
        "canonical_artifacts",
        sa.Column("source_filename", sa.String(500), nullable=True),
    )
    op.add_column(
        "canonical_artifacts",
        sa.Column("created_by", sa.String(200), nullable=True),
    )
    # Quality gate outcome (pass / fail / needs_review)
    op.add_column(
        "canonical_artifacts",
        sa.Column("quality_gate_outcome", sa.String(30), nullable=True),
    )

    # Index on workflow_id for fast workflow→artifact resolution
    op.create_index(
        "ix_canonical_artifacts_workflow",
        "canonical_artifacts",
        ["workflow_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_canonical_artifacts_workflow", table_name="canonical_artifacts")
    op.drop_column("canonical_artifacts", "quality_gate_outcome")
    op.drop_column("canonical_artifacts", "created_by")
    op.drop_column("canonical_artifacts", "source_filename")
    op.drop_column("canonical_artifacts", "source_type")
    op.drop_column("canonical_artifacts", "workflow_id")
