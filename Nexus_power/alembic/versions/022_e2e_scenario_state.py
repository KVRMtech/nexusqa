"""Add e2e_scenario_state table — P3 Lifecycle & Curation.

One row per E2E scenario per artifact, tracking:

    Lifecycle state machine:
        draft → reviewed → approved → automated → live → stable / failing
                                                       ↑
                                              revert via transitions

* ``state``                      — current lifecycle state
* ``state_changed_at/by``        — who & when made the last transition
* ``comments_json``              — ordered list of reviewer comments
* ``audit_log_json``             — every transition with from/to/user/note

The scenario_id is the deterministic ID assigned by the E2E architect (e.g.
``vs_001`` in visual_strict mode).  Scenarios themselves live in the
canonical artifact's ``e2e_architect_cache`` JSON blob; this table is the
*durable* layer that survives cache regenerations.

When the e2e_architect endpoint regenerates scenarios, state rows for
scenario_ids that no longer exist are *preserved* (for audit) but ignored
by the response merge.  New scenario_ids get a default state row on first
state mutation (lazy creation).

RLS follows the same tenant_isolation policy as migrations 010 / 019 /
020 / 021 — all queries must run with ``SET LOCAL nexus.current_tenant_id``
or the policy will block access.

Revision ID: 022_e2e_scenario_state
Revises: 021_echo_mvp
Create Date: 2026-05-12
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "022_e2e_scenario_state"
down_revision: Union[str, None] = "021_echo_mvp"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TENANT_TABLES_THIS_MIGRATION: tuple[str, ...] = (
    "e2e_scenario_state",
)


def upgrade() -> None:
    # ── e2e_scenario_state ────────────────────────────────────────────────
    op.create_table(
        "e2e_scenario_state",
        sa.Column("state_id", sa.String(64), primary_key=True),
        sa.Column(
            "artifact_id",
            sa.String(64),
            sa.ForeignKey("canonical_artifacts.artifact_id", ondelete="CASCADE"),
            nullable=False,
        ),
        # scenario_id is the deterministic ID emitted by the e2e_architect
        # endpoint (e.g. "vs_001").  No FK — scenarios live in the artifact's
        # JSON cache, not a relational table.
        sa.Column("scenario_id", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("session_id", sa.String(64), nullable=False, server_default=""),
        # Lifecycle state machine.  Validated at the application layer; the
        # column accepts any string so we can extend the state set without a
        # migration if the product evolves.
        #
        #   draft      — newly generated; not yet reviewed
        #   reviewed   — a reviewer has looked but not approved/rejected
        #   approved   — green-lit for automation/export
        #   rejected   — explicitly rejected (kept for audit, hidden by default)
        #   automated  — code generated and committed
        #   live       — automated and currently passing in CI
        #   stable     — has been live + passing for a sustained period
        #   failing    — automated but currently failing
        sa.Column(
            "state",
            sa.String(32),
            nullable=False,
            server_default="draft",
        ),
        sa.Column(
            "state_changed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "state_changed_by",
            sa.String(64),
            nullable=False,
            server_default="",
        ),
        sa.Column(
            "state_changed_by_email",
            sa.String(255),
            nullable=False,
            server_default="",
        ),
        # Comments thread. Each entry shape:
        #   {comment_id, user_id, email, body, created_at}
        sa.Column(
            "comments_json",
            JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        # Audit log of every state transition. Each entry shape:
        #   {from_state, to_state, user_id, email, at, note}
        sa.Column(
            "audit_log_json",
            JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
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
    )

    # Enforce one state row per (artifact, scenario)
    op.create_index(
        "ix_e2e_scenario_state_artifact_scenario",
        "e2e_scenario_state",
        ["artifact_id", "scenario_id"],
        unique=True,
    )
    op.create_index(
        "ix_e2e_scenario_state_tenant_state",
        "e2e_scenario_state",
        ["tenant_id", "state"],
    )
    op.create_index(
        "ix_e2e_scenario_state_artifact",
        "e2e_scenario_state",
        ["artifact_id"],
    )

    # ── RLS ───────────────────────────────────────────────────────────────
    for table in _TENANT_TABLES_THIS_MIGRATION:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;")
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table};")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
                USING (tenant_id = current_setting('nexus.current_tenant_id', true))
                WITH CHECK (tenant_id = current_setting('nexus.current_tenant_id', true));
            """
        )
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO nexus_app;")


def downgrade() -> None:
    for table in reversed(_TENANT_TABLES_THIS_MIGRATION):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table};")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY;")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;")

    op.drop_index("ix_e2e_scenario_state_artifact", table_name="e2e_scenario_state")
    op.drop_index("ix_e2e_scenario_state_tenant_state", table_name="e2e_scenario_state")
    op.drop_index(
        "ix_e2e_scenario_state_artifact_scenario", table_name="e2e_scenario_state"
    )
    op.drop_table("e2e_scenario_state")
