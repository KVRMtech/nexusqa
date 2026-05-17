"""Visual evidence tables — all Phase 2–7 schema additions.

This migration:
  1. Extends visual_frames (Phase 2) with artifact_id, frame_asset_path,
     app_instance_id, scene_id, segmentation_confidence.
  2. Creates visual_scenes (Phase 3) — stable scene groups.
  3. Creates app_instances (Phase 5) — confidence-scored app groupings.
  4. Creates evidence_controls (Phase 6) — Playwright-ready UI controls.
  5. Creates visual_flow_edges (Phase 7) — directed transition graph.

Future migrations (012+) add test_step_evidence (Phase 10).

Revision ID: 011_visual_evidence
Revises: 010_row_level_security
Create Date: 2026-04-18
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "011_visual_evidence"
down_revision: Union[str, None] = "010_row_level_security"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ════════════════════════════════════════════════════════════
    # Phase 2 — extend visual_frames
    # ════════════════════════════════════════════════════════════

    op.add_column(
        "visual_frames",
        sa.Column("frame_asset_path", sa.String(1000), server_default="", nullable=False),
    )
    op.add_column(
        "visual_frames",
        sa.Column(
            "artifact_id",
            sa.String(64),
            sa.ForeignKey("canonical_artifacts.artifact_id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.add_column(
        "visual_frames",
        sa.Column("app_instance_id", sa.String(64), nullable=True),
    )
    op.add_column(
        "visual_frames",
        sa.Column("scene_id", sa.String(64), nullable=True),
    )
    op.add_column(
        "visual_frames",
        sa.Column("segmentation_confidence", sa.Float, server_default="0.0", nullable=False),
    )
    op.create_index("ix_visual_frames_artifact", "visual_frames", ["artifact_id", "frame_index"])
    op.create_index("ix_visual_frames_app_instance", "visual_frames", ["app_instance_id"])
    op.create_index("ix_visual_frames_scene", "visual_frames", ["scene_id"])

    # ════════════════════════════════════════════════════════════
    # Phase 3 — visual_scenes
    # ════════════════════════════════════════════════════════════

    op.create_table(
        "visual_scenes",
        sa.Column("scene_id", sa.String(64), primary_key=True),
        sa.Column(
            "artifact_id",
            sa.String(64),
            sa.ForeignKey("canonical_artifacts.artifact_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column(
            "representative_frame_id",
            sa.String(64),
            sa.ForeignKey("visual_frames.frame_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("scene_index", sa.Integer, nullable=False, server_default="0"),
        sa.Column("screen_name", sa.String(500), nullable=False, server_default=""),
        sa.Column("ocr_text", sa.Text, nullable=False, server_default=""),
        sa.Column("detected_url", sa.String(2000), nullable=False, server_default=""),
        sa.Column("start_ms", sa.Integer, nullable=False, server_default="0"),
        sa.Column("end_ms", sa.Integer, nullable=False, server_default="0"),
        # Populated in Phase 5; nullable until app segmentation runs
        sa.Column("app_instance_id", sa.String(64), nullable=True),
        sa.Column("completeness_confidence", sa.Float, nullable=False, server_default="0.0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_visual_scenes_artifact", "visual_scenes", ["artifact_id"])
    op.create_index(
        "ix_visual_scenes_tenant_session", "visual_scenes", ["tenant_id", "session_id"]
    )
    op.create_index("ix_visual_scenes_app_instance", "visual_scenes", ["app_instance_id"])

    # ════════════════════════════════════════════════════════════
    # Phase 5 — app_instances
    # ════════════════════════════════════════════════════════════

    op.create_table(
        "app_instances",
        sa.Column("instance_id", sa.String(64), primary_key=True),
        sa.Column(
            "artifact_id",
            sa.String(64),
            sa.ForeignKey("canonical_artifacts.artifact_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("app_type", sa.String(50), nullable=False, server_default="unknown"),
        sa.Column("app_name", sa.String(500), nullable=False, server_default=""),
        sa.Column("detected_url", sa.String(2000), nullable=False, server_default=""),
        sa.Column("window_title", sa.String(500), nullable=False, server_default=""),
        # How the boundary was detected — see AppSegmenter.SEGMENTATION_BASIS
        sa.Column("segmentation_basis", sa.String(50), nullable=False, server_default="no_boundary"),
        sa.Column("segmentation_confidence", sa.Float, nullable=False, server_default="1.0"),
        sa.Column("first_scene_index", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_scene_index", sa.Integer, nullable=False, server_default="0"),
        sa.Column("scene_count", sa.Integer, nullable=False, server_default="0"),
        # Future operator override hook — not used in automated pipeline
        sa.Column("operator_confirmed", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_app_instances_artifact", "app_instances", ["artifact_id"])
    op.create_index(
        "ix_app_instances_tenant_session", "app_instances", ["tenant_id", "session_id"]
    )

    # ════════════════════════════════════════════════════════════
    # Phase 6 — evidence_controls
    # ════════════════════════════════════════════════════════════

    op.create_table(
        "evidence_controls",
        sa.Column("control_id", sa.String(64), primary_key=True),
        sa.Column(
            "scene_id",
            sa.String(64),
            sa.ForeignKey("visual_scenes.scene_id", ondelete="CASCADE"),
            nullable=False,
        ),
        # frame_id nullable so rows can be inserted before frame FK is resolved
        sa.Column(
            "frame_id",
            sa.String(64),
            sa.ForeignKey("visual_frames.frame_id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "artifact_id",
            sa.String(64),
            sa.ForeignKey("canonical_artifacts.artifact_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("element_type", sa.String(50), nullable=False),
        sa.Column("label_text", sa.String(500), nullable=False, server_default=""),
        sa.Column("value_text", sa.String(500), nullable=False, server_default=""),
        sa.Column("bounding_box", sa.JSON, nullable=False, server_default="{}"),
        # "ocr" | "vision" | "hybrid" | "unknown"
        sa.Column("selector_source", sa.String(20), nullable=False, server_default="unknown"),
        # Playwright locator — empty when automation_ready=false
        sa.Column("playwright_selector", sa.String(1000), nullable=False, server_default=""),
        sa.Column("selector_confidence", sa.Float, nullable=False, server_default="0.0"),
        sa.Column("automation_ready", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_evidence_controls_scene", "evidence_controls", ["scene_id"])
    op.create_index("ix_evidence_controls_artifact", "evidence_controls", ["artifact_id"])

    # ════════════════════════════════════════════════════════════
    # Phase 7 — visual_flow_edges
    # ════════════════════════════════════════════════════════════

    op.create_table(
        "visual_flow_edges",
        sa.Column("edge_id", sa.String(64), primary_key=True),
        sa.Column(
            "artifact_id",
            sa.String(64),
            sa.ForeignKey("canonical_artifacts.artifact_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "from_scene_id",
            sa.String(64),
            sa.ForeignKey("visual_scenes.scene_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "to_scene_id",
            sa.String(64),
            sa.ForeignKey("visual_scenes.scene_id", ondelete="CASCADE"),
            nullable=False,
        ),
        # null when no control linked (Layer 1 edges)
        sa.Column(
            "trigger_control_id",
            sa.String(64),
            sa.ForeignKey("evidence_controls.control_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        # "observed_transition" | "action_confirmed_transition"
        sa.Column("edge_type", sa.String(40), nullable=False, server_default="observed_transition"),
        # "transition" | "click" | "navigate" | "type" | "select" | "submit" | "press"
        sa.Column("action_type", sa.String(50), nullable=False, server_default="transition"),
        sa.Column("action_value", sa.String(500), nullable=False, server_default=""),
        sa.Column("transition_ms", sa.Integer, nullable=False, server_default="0"),
        sa.Column("intra_app", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("evidence_confidence", sa.Float, nullable=False, server_default="1.0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_visual_flow_edges_artifact", "visual_flow_edges", ["artifact_id"])
    op.create_index(
        "ix_visual_flow_edges_scenes", "visual_flow_edges", ["from_scene_id", "to_scene_id"]
    )


def downgrade() -> None:
    # Phase 7
    op.drop_index("ix_visual_flow_edges_scenes", table_name="visual_flow_edges")
    op.drop_index("ix_visual_flow_edges_artifact", table_name="visual_flow_edges")
    op.drop_table("visual_flow_edges")

    # Phase 6
    op.drop_index("ix_evidence_controls_artifact", table_name="evidence_controls")
    op.drop_index("ix_evidence_controls_scene", table_name="evidence_controls")
    op.drop_table("evidence_controls")

    # Phase 5
    op.drop_index("ix_app_instances_tenant_session", table_name="app_instances")
    op.drop_index("ix_app_instances_artifact", table_name="app_instances")
    op.drop_table("app_instances")

    # Phase 3
    op.drop_index("ix_visual_scenes_app_instance", table_name="visual_scenes")
    op.drop_index("ix_visual_scenes_tenant_session", table_name="visual_scenes")
    op.drop_index("ix_visual_scenes_artifact", table_name="visual_scenes")
    op.drop_table("visual_scenes")

    # Phase 2 columns on visual_frames
    op.drop_index("ix_visual_frames_scene", table_name="visual_frames")
    op.drop_index("ix_visual_frames_app_instance", table_name="visual_frames")
    op.drop_index("ix_visual_frames_artifact", table_name="visual_frames")
    op.drop_column("visual_frames", "segmentation_confidence")
    op.drop_column("visual_frames", "scene_id")
    op.drop_column("visual_frames", "app_instance_id")
    op.drop_column("visual_frames", "artifact_id")
    op.drop_column("visual_frames", "frame_asset_path")

