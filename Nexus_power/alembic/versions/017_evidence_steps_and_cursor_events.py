"""Add evidence_steps and cursor_events tables.

These are the first-class data model for intra-page user actions:

    Page (visual_scenes)
        └── State (scene_state_summary inside visual_scenes)
                └── Step (evidence_steps)              ← NEW
                        └── Cursor evidence (cursor_events)  ← NEW

evidence_steps
    One row per observable user action inside a scene (form fill, dropdown
    select, button click, navigation).  Replaces the
    ``scene_state_summary.frame_actions`` JSON array we used as a stop-gap.
    Action_kind / target_label / observed_value are populated by the
    triangulated classifier (B.3) which combines OCR diff, cursor click,
    LLaVA delta, and audio intent into one record with an agreement_score.

    Downstream test_case_steps and Playwright/Selenium generators consume
    this table directly — no more parsing scene metadata.

cursor_events
    Per-frame cursor (x, y) and click/drag flags produced by the cursor
    tracking module (A.3).  Stored separately from evidence_steps because
    one action can span multiple cursor events (mouse-move → click → release)
    and because cursor data is independently useful for replay and
    annotation.

Revision ID: 017_evidence_steps_and_cursor_events
Revises: 016_scene_state_summaries
Create Date: 2026-05-09
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "017_evidence_steps_cursor"
down_revision: Union[str, None] = "016_scene_state_summaries"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── evidence_steps ───────────────────────────────────────────────────────
    op.create_table(
        "evidence_steps",
        sa.Column("step_id", sa.String(64), primary_key=True),
        sa.Column(
            "artifact_id",
            sa.String(64),
            sa.ForeignKey("canonical_artifacts.artifact_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "scene_id",
            sa.String(64),
            sa.ForeignKey("visual_scenes.scene_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("step_index", sa.Integer, nullable=False, server_default="0"),
        # Canonical action taxonomy — must match control_extractor's vocabulary
        # so generated test steps and evidence steps share an action_kind set.
        sa.Column("action_kind", sa.String(32), nullable=False),
        sa.Column("target_label", sa.String(500), nullable=False, server_default=""),
        sa.Column("observed_value", sa.String(2000), nullable=False, server_default=""),
        # Triggering control link — when the action is anchored to a specific
        # extracted control row, set this so downstream consumers can reuse
        # its selectors instead of synthesising new ones.
        sa.Column(
            "trigger_control_id",
            sa.String(64),
            sa.ForeignKey("evidence_controls.control_id", ondelete="SET NULL"),
            nullable=True,
        ),
        # Frame anchors: the frame just before and just after the user action.
        # Used by the frontend to render before/after thumbnails and by the
        # automation generator to reason about precondition/postcondition.
        sa.Column(
            "before_frame_id",
            sa.String(64),
            sa.ForeignKey("visual_frames.frame_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "after_frame_id",
            sa.String(64),
            sa.ForeignKey("visual_frames.frame_id", ondelete="SET NULL"),
            nullable=True,
        ),
        # Time range — milliseconds relative to artifact start.
        sa.Column("start_ms", sa.Integer, nullable=False, server_default="0"),
        sa.Column("end_ms", sa.Integer, nullable=False, server_default="0"),
        # Confidence + agreement.  ``confidence`` is the classifier's
        # overall confidence in this step (0..1).  ``agreement_score`` is
        # how many independent signals supported it (0..1, where 1 means
        # all four — OCR, cursor, LLaVA, audio — agreed).
        sa.Column(
            "confidence", sa.Float, nullable=False, server_default="0.0",
        ),
        sa.Column(
            "agreement_score", sa.Float, nullable=False, server_default="0.0",
        ),
        # Which signals contributed.  Possible values:
        #   ocr_diff, cursor_click, cursor_hover, llava_delta, audio_intent,
        #   ui_element_added, ui_element_removed, frame_captured.
        sa.Column(
            "evidence_signals",
            JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        # Cursor coordinates at the moment of the action — null when cursor
        # tracking did not produce a confident reading for this frame pair.
        sa.Column("cursor_x", sa.Integer, nullable=True),
        sa.Column("cursor_y", sa.Integer, nullable=True),
        # Audio anchor: the transcript snippet the SME spoke around the time
        # of this action, plus the alignment timestamp.  Empty when no
        # audio segment overlapped the action window.
        sa.Column(
            "audio_intent_text", sa.Text, nullable=False, server_default="",
        ),
        sa.Column("audio_intent_ts_ms", sa.Integer, nullable=True),
        # Free-form metadata: any extra detector-specific fields (e.g.
        # delta tokens, llava raw description, cursor velocity profile).
        sa.Column(
            "metadata_json",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_evidence_steps_artifact",
        "evidence_steps",
        ["artifact_id"],
    )
    op.create_index(
        "ix_evidence_steps_scene_index",
        "evidence_steps",
        ["scene_id", "step_index"],
    )
    op.create_index(
        "ix_evidence_steps_tenant_session",
        "evidence_steps",
        ["tenant_id", "session_id"],
    )

    # ── cursor_events ────────────────────────────────────────────────────────
    op.create_table(
        "cursor_events",
        sa.Column("event_id", sa.String(64), primary_key=True),
        sa.Column(
            "artifact_id",
            sa.String(64),
            sa.ForeignKey("canonical_artifacts.artifact_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "frame_id",
            sa.String(64),
            sa.ForeignKey("visual_frames.frame_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("frame_index", sa.Integer, nullable=False, server_default="0"),
        sa.Column("timestamp_ms", sa.Integer, nullable=False, server_default="0"),
        # Cursor coordinates in pixels relative to the frame's top-left.  Both
        # required when the row is persisted — detectors that can't isolate a
        # cursor location should not emit a cursor_event row at all.
        sa.Column("cursor_x", sa.Integer, nullable=False),
        sa.Column("cursor_y", sa.Integer, nullable=False),
        # Frame-pair velocity, in pixels per second, used by the classifier
        # to detect "user paused on target" (low velocity preceding a click).
        sa.Column(
            "velocity", sa.Float, nullable=False, server_default="0.0",
        ),
        # Detected interactions.  ``is_click`` is set on frames where the
        # cursor's appearance changed in ways consistent with a mouse-down
        # event (size jump, ripple, color flash).  ``is_drag`` is set on
        # frames where the cursor moved while a click was active.
        sa.Column(
            "is_click", sa.Boolean, nullable=False, server_default=sa.false(),
        ),
        sa.Column(
            "is_drag", sa.Boolean, nullable=False, server_default=sa.false(),
        ),
        # Detection method tag — lets the classifier weigh different
        # detection methods (template > motion > heuristic).
        sa.Column(
            "detection_method",
            sa.String(32),
            nullable=False,
            server_default="motion",
        ),
        sa.Column(
            "confidence", sa.Float, nullable=False, server_default="0.0",
        ),
        sa.Column(
            "metadata_json",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_cursor_events_artifact",
        "cursor_events",
        ["artifact_id"],
    )
    op.create_index(
        "ix_cursor_events_frame",
        "cursor_events",
        ["frame_id"],
    )
    op.create_index(
        "ix_cursor_events_tenant_session",
        "cursor_events",
        ["tenant_id", "session_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_cursor_events_tenant_session", table_name="cursor_events")
    op.drop_index("ix_cursor_events_frame", table_name="cursor_events")
    op.drop_index("ix_cursor_events_artifact", table_name="cursor_events")
    op.drop_table("cursor_events")

    op.drop_index("ix_evidence_steps_tenant_session", table_name="evidence_steps")
    op.drop_index("ix_evidence_steps_scene_index", table_name="evidence_steps")
    op.drop_index("ix_evidence_steps_artifact", table_name="evidence_steps")
    op.drop_table("evidence_steps")
