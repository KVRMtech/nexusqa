"""Add semantic completeness fields to canonical_artifacts.

Phase 2.3: Enrich Canonical Artifact Schema — has_real_transcript,
has_visual_semantics, semantic_completeness_score.

Enables consumers to check whether an artifact contains real
semantic content versus placeholder/stub output.

Revision ID: 009_semantic_completeness
Revises: 008_canonical_provenance
Create Date: 2026-03-31
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "009_semantic_completeness"
down_revision: Union[str, None] = "008_canonical_provenance"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "canonical_artifacts",
        sa.Column(
            "has_real_transcript",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "canonical_artifacts",
        sa.Column(
            "has_visual_semantics",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "canonical_artifacts",
        sa.Column(
            "semantic_completeness_score",
            sa.Float(),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("canonical_artifacts", "semantic_completeness_score")
    op.drop_column("canonical_artifacts", "has_visual_semantics")
    op.drop_column("canonical_artifacts", "has_real_transcript")
