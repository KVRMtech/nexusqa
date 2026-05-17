"""Add action_kind, display_label, observed_value to evidence_controls.

These columns carry structured user-action evidence extracted by the
eyes-engine and normalised by the control extractor (Phase 6):

  action_kind    — interaction type: click / enter_text / select_option /
                   check / navigate / review / interact
  display_label  — pre-composed human-readable phrase for the UI renderer,
                   e.g. "Fill: Life Insurance = Get a quote"
  observed_value — cleaned OCR-evidence value that was entered or selected

Revision ID: 015_control_action_semantics
Revises: 014_visual_flow_interleaved
Create Date: 2026-04-22
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "015_control_action_semantics"
down_revision: Union[str, None] = "014_visual_flow_interleaved"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "evidence_controls",
        sa.Column(
            "action_kind",
            sa.String(50),
            nullable=False,
            server_default="",
        ),
    )
    op.add_column(
        "evidence_controls",
        sa.Column(
            "display_label",
            sa.String(600),
            nullable=False,
            server_default="",
        ),
    )
    op.add_column(
        "evidence_controls",
        sa.Column(
            "observed_value",
            sa.String(500),
            nullable=False,
            server_default="",
        ),
    )


def downgrade() -> None:
    op.drop_column("evidence_controls", "observed_value")
    op.drop_column("evidence_controls", "display_label")
    op.drop_column("evidence_controls", "action_kind")
