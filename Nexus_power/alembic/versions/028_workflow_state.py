"""Workflow state + step history for the canonical processing core (Phase 1).

Adds two tables:
  - workflow_state: one row per workflow, holds plan + checkpoint + deadline.
  - workflow_step_history: append-only audit of every step attempt.

Indexes optimise the orchestrator sweeper's hot queries:
  - (status, deadline_at)  — find workflows past their workflow-level deadline.
  - (status, last_heartbeat) — find RUNNING workflows that stopped beating.

Revision ID: 028_workflow_state
Revises: 027_merge_e2e_and_knowledge_heads
Create Date: 2026-05-15
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "028_workflow_state"
down_revision: Union[str, None] = "027_merge_heads"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "workflow_state",
        sa.Column("workflow_id", sa.String(), primary_key=True),
        sa.Column("tenant_id", sa.String(), nullable=False, index=True),
        sa.Column("session_id", sa.String(), nullable=False, index=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("plan", sa.JSON(), nullable=False),
        sa.Column(
            "checkpoint", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")
        ),
        sa.Column("current_step", sa.String(), nullable=True),
        sa.Column("step_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "last_heartbeat",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("error_context", sa.JSON(), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_workflow_state_status_deadline",
        "workflow_state",
        ["status", "deadline_at"],
    )
    op.create_index(
        "ix_workflow_state_status_heartbeat",
        "workflow_state",
        ["status", "last_heartbeat"],
    )

    op.create_table(
        "workflow_step_history",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "workflow_id",
            sa.String(),
            sa.ForeignKey("workflow_state.workflow_id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("step_name", sa.String(), nullable=False),
        sa.Column("engine", sa.String(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
    )

    # Trigger to keep updated_at fresh on every UPDATE. Postgres has no
    # built-in onupdate at the column level; this is the canonical fix.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION workflow_state_touch() RETURNS trigger AS $$
        BEGIN
          NEW.updated_at := now();
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER workflow_state_touch_trg
        BEFORE UPDATE ON workflow_state
        FOR EACH ROW EXECUTE FUNCTION workflow_state_touch();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS workflow_state_touch_trg ON workflow_state")
    op.execute("DROP FUNCTION IF EXISTS workflow_state_touch")
    op.drop_table("workflow_step_history")
    op.drop_index(
        "ix_workflow_state_status_heartbeat", table_name="workflow_state"
    )
    op.drop_index(
        "ix_workflow_state_status_deadline", table_name="workflow_state"
    )
    op.drop_table("workflow_state")
