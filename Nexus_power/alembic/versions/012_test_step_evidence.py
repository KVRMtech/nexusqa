"""Add visual evidence anchors to test_case_steps — Phase 10 schema.

Each step can now reference the exact visual scene, control, and flow
edge that motivated it, enabling full traceability from Playwright
selector back to the original screen recording frame.

Columns added to test_case_steps:
  - evidence_scene_id     → visual_scenes(scene_id)       ON DELETE SET NULL
  - evidence_control_id   → evidence_controls(control_id) ON DELETE SET NULL
  - evidence_edge_id      → visual_flow_edges(edge_id)    ON DELETE SET NULL
  - proof_confidence      FLOAT DEFAULT 0.0
  - evidence_mode         VARCHAR(20) DEFAULT 'multimodal'

Revision ID: 012_test_step_evidence
Revises: 011_visual_evidence
Create Date: 2026-04-19
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "012_test_step_evidence"
down_revision: Union[str, None] = "011_visual_evidence"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "test_case_steps",
        sa.Column(
            "evidence_scene_id",
            sa.String(64),
            sa.ForeignKey("visual_scenes.scene_id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "test_case_steps",
        sa.Column(
            "evidence_control_id",
            sa.String(64),
            sa.ForeignKey("evidence_controls.control_id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "test_case_steps",
        sa.Column(
            "evidence_edge_id",
            sa.String(64),
            sa.ForeignKey("visual_flow_edges.edge_id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "test_case_steps",
        sa.Column(
            "proof_confidence",
            sa.Float,
            server_default=sa.text("0.0"),
            nullable=False,
        ),
    )
    op.add_column(
        "test_case_steps",
        sa.Column(
            "evidence_mode",
            sa.String(20),
            server_default=sa.text("'multimodal'"),
            nullable=False,
        ),
    )

    op.create_index(
        "ix_test_case_steps_evidence_scene",
        "test_case_steps",
        ["evidence_scene_id"],
    )
    op.create_index(
        "ix_test_case_steps_evidence_control",
        "test_case_steps",
        ["evidence_control_id"],
    )
    op.create_index(
        "ix_test_case_steps_evidence_edge",
        "test_case_steps",
        ["evidence_edge_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_test_case_steps_evidence_edge", "test_case_steps")
    op.drop_index("ix_test_case_steps_evidence_control", "test_case_steps")
    op.drop_index("ix_test_case_steps_evidence_scene", "test_case_steps")
    op.drop_column("test_case_steps", "evidence_mode")
    op.drop_column("test_case_steps", "proof_confidence")
    op.drop_column("test_case_steps", "evidence_edge_id")
    op.drop_column("test_case_steps", "evidence_control_id")
    op.drop_column("test_case_steps", "evidence_scene_id")
