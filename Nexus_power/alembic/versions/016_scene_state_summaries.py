"""Add scene_state_summary, duration_quality, scene_quality to visual_scenes;
primary_action_summary, action_quality, action_confidence to visual_flow_edges.

These structured JSON fields are produced by the evidence pipeline (build_scenes,
flow_builder) and consumed by the API readiness endpoint and the client renderer.
They replace heuristic string-matching in the frontend with persisted, confidence-
aware evidence objects.

Revision ID: 016_scene_state_summaries
Revises: 015_control_action_semantics
Create Date: 2026-04-22
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "016_scene_state_summaries"
down_revision: Union[str, None] = "015_control_action_semantics"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── visual_scenes ────────────────────────────────────────────────────────
    # Structured scene state object: screen_type, screen_title, application_label,
    # domain, page_key, state_signals, state_confidence, state_quality, etc.
    op.add_column(
        "visual_scenes",
        sa.Column("scene_state_summary", sa.JSON(), nullable=True),
    )
    # Explicit duration in milliseconds (end_ms − start_ms) stored alongside
    # quality flag so the UI can decide whether to display it.
    op.add_column(
        "visual_scenes",
        sa.Column("duration_ms", sa.Integer(), nullable=True),
    )
    # exact | estimated | unknown
    op.add_column(
        "visual_scenes",
        sa.Column(
            "duration_quality",
            sa.String(20),
            nullable=False,
            server_default="unknown",
        ),
    )
    # strong | degraded | weak — top-level gate for UI rendering decisions
    op.add_column(
        "visual_scenes",
        sa.Column(
            "scene_quality",
            sa.String(20),
            nullable=False,
            server_default="weak",
        ),
    )

    # ── visual_flow_edges ─────────────────────────────────────────────────────
    # Structured action object: action_kind, action_label, evidence_sources,
    # action_quality, action_confidence, fallback_used, etc.
    op.add_column(
        "visual_flow_edges",
        sa.Column("primary_action_summary", sa.JSON(), nullable=True),
    )
    # strong | degraded | weak — mirrors scene_quality for edges
    op.add_column(
        "visual_flow_edges",
        sa.Column(
            "action_quality",
            sa.String(20),
            nullable=False,
            server_default="weak",
        ),
    )
    # 0.0–1.0 numeric confidence for the action reconstruction
    op.add_column(
        "visual_flow_edges",
        sa.Column(
            "action_confidence",
            sa.Float(),
            nullable=False,
            server_default="0.0",
        ),
    )


def downgrade() -> None:
    op.drop_column("visual_flow_edges", "action_confidence")
    op.drop_column("visual_flow_edges", "action_quality")
    op.drop_column("visual_flow_edges", "primary_action_summary")

    op.drop_column("visual_scenes", "scene_quality")
    op.drop_column("visual_scenes", "duration_quality")
    op.drop_column("visual_scenes", "duration_ms")
    op.drop_column("visual_scenes", "scene_state_summary")
