"""Add visual_flows table and flow_id columns — Phase 5b schema.

Introduces the visual_flows table for business-process segmentation.
Each flow groups a contiguous set of scenes that belong to the same
testable business journey (e.g., "guardianlife.com — Get a Quote").

Columns added to existing tables:
  - visual_scenes.flow_id    → visual_flows(flow_id)  ON DELETE SET NULL
  - visual_flow_edges.flow_id → visual_flows(flow_id) ON DELETE SET NULL

Revision ID: 013_visual_flows
Revises: 012_test_step_evidence
Create Date: 2026-04-19
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "013_visual_flows"
down_revision: Union[str, None] = "012_test_step_evidence"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── New table: visual_flows ────────────────────────────────
    op.create_table(
        "visual_flows",
        sa.Column("flow_id", sa.String(64), primary_key=True),
        sa.Column(
            "artifact_id",
            sa.String(64),
            sa.ForeignKey("canonical_artifacts.artifact_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("flow_index", sa.Integer, nullable=False, server_default="0"),
        sa.Column("flow_label", sa.String(500), nullable=False, server_default=""),
        sa.Column("domain", sa.String(500), nullable=False, server_default=""),
        sa.Column("app_type", sa.String(50), nullable=False, server_default="unknown"),
        sa.Column("first_scene_index", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_scene_index", sa.Integer, nullable=False, server_default="0"),
        sa.Column("scene_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("is_noise", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("confidence", sa.Float, nullable=False, server_default="1.0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    op.create_index("ix_visual_flows_artifact", "visual_flows", ["artifact_id"])
    op.create_index(
        "ix_visual_flows_tenant_session",
        "visual_flows",
        ["tenant_id", "session_id"],
    )

    # ── Add flow_id FK to visual_scenes ────────────────────────
    op.add_column(
        "visual_scenes",
        sa.Column("flow_id", sa.String(64), nullable=True),
    )
    op.create_foreign_key(
        "fk_visual_scenes_flow_id",
        "visual_scenes",
        "visual_flows",
        ["flow_id"],
        ["flow_id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_visual_scenes_flow", "visual_scenes", ["flow_id"])

    # ── Add flow_id FK to visual_flow_edges ────────────────────
    op.add_column(
        "visual_flow_edges",
        sa.Column("flow_id", sa.String(64), nullable=True),
    )
    op.create_foreign_key(
        "fk_visual_flow_edges_flow_id",
        "visual_flow_edges",
        "visual_flows",
        ["flow_id"],
        ["flow_id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_visual_flow_edges_flow", "visual_flow_edges", ["flow_id"])


def downgrade() -> None:
    op.drop_index("ix_visual_flow_edges_flow", table_name="visual_flow_edges")
    op.drop_constraint("fk_visual_flow_edges_flow_id", "visual_flow_edges", type_="foreignkey")
    op.drop_column("visual_flow_edges", "flow_id")

    op.drop_index("ix_visual_scenes_flow", table_name="visual_scenes")
    op.drop_constraint("fk_visual_scenes_flow_id", "visual_scenes", type_="foreignkey")
    op.drop_column("visual_scenes", "flow_id")

    op.drop_index("ix_visual_flows_tenant_session", table_name="visual_flows")
    op.drop_index("ix_visual_flows_artifact", table_name="visual_flows")
    op.drop_table("visual_flows")
