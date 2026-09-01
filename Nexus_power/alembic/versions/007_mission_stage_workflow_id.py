"""Add workflow_id column to mission_stages for orchestrator provenance.

Revision ID: 007_mission_stage_workflow_id
Revises: 006_canonical_artifacts
Create Date: 2026-03-30
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "007_mission_stage_workflow_id"
down_revision: Union[str, None] = "006_canonical_artifacts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "mission_stages",
        sa.Column(
            "workflow_id",
            sa.String(64),
            nullable=True,
            comment="Generic orchestrator workflow that executed this stage",
        ),
    )
    op.create_index(
        "ix_mission_stages_workflow",
        "mission_stages",
        ["workflow_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_mission_stages_workflow", table_name="mission_stages")
    op.drop_column("mission_stages", "workflow_id")
