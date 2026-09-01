"""O0 — Journey baseline lifecycle (captured → approved → validated → drifted).

A captured outcome is an observation; it becomes an expectation only when a
human approves it.  These columns give every journey a baseline lifecycle:

  * ``baseline_status`` — the current lifecycle state.
  * ``baseline_traversal_id`` — the traversal whose outcomes were approved.
  * ``baseline_outcome_hash`` — sha256 of the canonical outcome_values for
    quick drift detection at fold time.
  * ``baseline_snapshot`` — JSONB of the approved state (outcome_values +
    path metadata); the human-readable diff source.
  * ``baseline_approved_at`` / ``baseline_approved_by`` — audit trail.
  * ``drift_traversal_id`` / ``drift_detected_at`` — what caused the drift.

Everything unapproved wears ``captured`` openly — UNVERIFIED.

PURELY ADDITIVE: all columns have defaults; existing rows read as
``baseline_status='captured'`` (no baseline yet) and behave exactly as before.

Revision ID: qec_008
Revises: qec_007
Create Date: 2026-08-05
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers
revision: str = "qec_008"
down_revision: Union[str, None] = "qec_007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "journeys",
        sa.Column("baseline_status", sa.String(16), nullable=False,
                  server_default="captured"),
    )
    op.add_column(
        "journeys",
        sa.Column("baseline_traversal_id", sa.String(64), nullable=False,
                  server_default=""),
    )
    op.add_column(
        "journeys",
        sa.Column("baseline_outcome_hash", sa.String(64), nullable=False,
                  server_default=""),
    )
    op.add_column(
        "journeys",
        sa.Column("baseline_snapshot", JSONB, nullable=True),
    )
    op.add_column(
        "journeys",
        sa.Column("baseline_approved_at",
                  sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "journeys",
        sa.Column("baseline_approved_by", sa.String(200), nullable=False,
                  server_default=""),
    )
    op.add_column(
        "journeys",
        sa.Column("drift_traversal_id", sa.String(64), nullable=False,
                  server_default=""),
    )
    op.add_column(
        "journeys",
        sa.Column("drift_detected_at",
                  sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_journeys_baseline_status",
        "journeys", ["tenant_id", "app_id", "baseline_status"],
    )


def downgrade() -> None:
    op.drop_index("ix_journeys_baseline_status", table_name="journeys")
    op.drop_column("journeys", "drift_detected_at")
    op.drop_column("journeys", "drift_traversal_id")
    op.drop_column("journeys", "baseline_approved_by")
    op.drop_column("journeys", "baseline_approved_at")
    op.drop_column("journeys", "baseline_snapshot")
    op.drop_column("journeys", "baseline_outcome_hash")
    op.drop_column("journeys", "baseline_traversal_id")
    op.drop_column("journeys", "baseline_status")
