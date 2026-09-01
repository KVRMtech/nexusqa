"""Add is_interleaved and visit_count to visual_flows — multi-app session support.

Tracks whether a flow was visited multiple times during an interleaved
multi-application KT session (e.g., Outlook → USAA → Mainframe → USAA (return)).

Columns added to visual_flows:
  - is_interleaved  BOOLEAN  — True when user returned to this app after another
  - visit_count     INTEGER  — Number of distinct time-contiguous visit segments

Revision ID: 014_visual_flow_interleaved
Revises: 013_visual_flows
Create Date: 2026-04-21
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "014_visual_flow_interleaved"
down_revision: Union[str, None] = "013_visual_flows"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "visual_flows",
        sa.Column("is_interleaved", sa.Boolean, nullable=False, server_default="false"),
    )
    op.add_column(
        "visual_flows",
        sa.Column("visit_count", sa.Integer, nullable=False, server_default="1"),
    )


def downgrade() -> None:
    op.drop_column("visual_flows", "visit_count")
    op.drop_column("visual_flows", "is_interleaved")
