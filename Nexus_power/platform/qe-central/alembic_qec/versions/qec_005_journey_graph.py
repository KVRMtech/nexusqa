"""QE-Central Journey Graph — journeys, nodes, edges, traversals, branches.

Release C of ``QECentral/docs/JOURNEY_GRAPH_PLAN.md``: the flow ledger's
walked paths become a queryable graph. Five tenant+app-scoped tables, all
under the ENABLE + FORCE + ``tenant_isolation`` RLS pattern of qec_001..004.

Honesty invariants the schema encodes:
  * ``journeys.flow_id`` stays EQUAL to the ledger's ``flow_id_for`` output —
    every historical flow joins the graph without migration;
  * ``journey_traversals.completed`` is copied from the ledger (derived at
    source, never asserted) and dedups on (exploration, journey, path hash)
    so re-folding is idempotent;
  * ``journey_branches`` records every enumerated option — UNWALKED IS A ROW,
    not an absence; ``blocked`` carries its attributed reason.

PURELY ADDITIVE: nothing existing changes shape; an app never folded behaves
exactly as before.

Revision ID: qec_005
Revises: qec_004
Create Date: 2026-08-04
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers
revision: str = "qec_005"
down_revision: Union[str, None] = "qec_004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = ("journeys", "journey_nodes", "journey_edges",
           "journey_traversals", "journey_branches")


def _ts(name: str) -> sa.Column:
    return sa.Column(
        name, sa.DateTime(timezone=True), nullable=False,
        server_default=sa.text("now()"),
    )


def _apply_rls(table: str) -> None:
    op.execute(f"""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = '{table}'
            ) THEN
                ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
                ALTER TABLE {table} FORCE ROW LEVEL SECURITY;

                DROP POLICY IF EXISTS tenant_isolation ON {table};
                CREATE POLICY tenant_isolation ON {table}
                    USING (tenant_id = current_setting('nexus.current_tenant_id', true))
                    WITH CHECK (tenant_id = current_setting('nexus.current_tenant_id', true));

                IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'qec') THEN
                    GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO qec;
                END IF;
            END IF;
        END $$;
    """)


def _drop_rls(table: str) -> None:
    op.execute(f"""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = '{table}'
            ) THEN
                DROP POLICY IF EXISTS tenant_isolation ON {table};
                ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY;
                ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;
            END IF;
        END $$;
    """)


def upgrade() -> None:
    op.create_table(
        "journeys",
        sa.Column("journey_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("app_id", sa.String(64), nullable=False),
        sa.Column("entry_fingerprint", sa.String(128), nullable=False),
        sa.Column("flow_id", sa.String(64), nullable=False),
        sa.Column("entry_url", sa.String(2000), nullable=False, server_default=""),
        sa.Column("entry_title", sa.String(200), nullable=False, server_default=""),
        sa.Column("business_name", sa.String(200), nullable=False, server_default=""),
        sa.Column("name_source", sa.String(16), nullable=False, server_default="fallback"),
        sa.Column("named_by", sa.String(200), nullable=False, server_default=""),
        sa.Column("name_description", sa.String(500), nullable=False, server_default=""),
        sa.Column("deepest_steps", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_proven_at", sa.DateTime(timezone=True), nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
    )
    op.create_index("uq_journeys_tenant_app_entry", "journeys",
                    ["tenant_id", "app_id", "entry_fingerprint"], unique=True)

    op.create_table(
        "journey_nodes",
        sa.Column("node_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("app_id", sa.String(64), nullable=False),
        sa.Column("fingerprint", sa.String(128), nullable=False),
        sa.Column("url", sa.String(2000), nullable=False, server_default=""),
        sa.Column("title", sa.String(200), nullable=False, server_default=""),
        sa.Column("is_decision", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("is_boundary", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("has_outcome", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("stale", sa.Boolean, nullable=False, server_default=sa.text("false")),
        _ts("first_seen_at"),
        _ts("last_seen_at"),
    )
    op.create_index("uq_journey_nodes_tenant_app_fp", "journey_nodes",
                    ["tenant_id", "app_id", "fingerprint"], unique=True)

    op.create_table(
        "journey_edges",
        sa.Column("edge_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("app_id", sa.String(64), nullable=False),
        sa.Column("from_fp", sa.String(128), nullable=False),
        sa.Column("to_fp", sa.String(128), nullable=False),
        sa.Column("trigger_label_norm", sa.String(200), nullable=False),
        sa.Column("advance_tier", sa.Integer, nullable=False, server_default="0"),
        sa.Column("walk_count", sa.Integer, nullable=False, server_default="1"),
        _ts("first_walked_at"),
        _ts("last_walked_at"),
    )
    op.create_index("uq_journey_edges_identity", "journey_edges",
                    ["tenant_id", "app_id", "from_fp", "to_fp",
                     "trigger_label_norm"], unique=True)

    op.create_table(
        "journey_traversals",
        sa.Column("traversal_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("app_id", sa.String(64), nullable=False),
        sa.Column("journey_id", sa.String(64), nullable=False),
        sa.Column("exploration_id", sa.String(64), nullable=False),
        sa.Column("terminal", sa.String(40), nullable=False),
        sa.Column("completed", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("fully_answered", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("path_fps", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("path_hash", sa.String(64), nullable=False),
        sa.Column("identity_ref", sa.String(200), nullable=False, server_default=""),
        sa.Column("env_ref", sa.String(200), nullable=False, server_default=""),
        sa.Column("outcome_values", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("pre_hardening", sa.Boolean, nullable=False, server_default=sa.text("false")),
        _ts("created_at"),
    )
    op.create_index("uq_journey_traversals_identity", "journey_traversals",
                    ["tenant_id", "app_id", "exploration_id", "journey_id",
                     "path_hash"], unique=True)
    op.create_index("ix_journey_traversals_journey", "journey_traversals",
                    ["tenant_id", "app_id", "journey_id"])

    op.create_table(
        "journey_branches",
        sa.Column("branch_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("app_id", sa.String(64), nullable=False),
        sa.Column("node_fp", sa.String(128), nullable=False),
        sa.Column("control_signature", sa.String(64), nullable=False),
        sa.Column("control_label_norm", sa.String(200), nullable=False),
        sa.Column("option_label_norm", sa.String(200), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="discovered"),
        sa.Column("blocked_reason", sa.String(400), nullable=False, server_default=""),
        sa.Column("walked_in_traversal", sa.String(64), nullable=False, server_default=""),
        _ts("last_status_at"),
        _ts("created_at"),
    )
    op.create_index("uq_journey_branches_identity", "journey_branches",
                    ["tenant_id", "app_id", "node_fp", "control_signature",
                     "option_label_norm"], unique=True)
    op.create_index("ix_journey_branches_status", "journey_branches",
                    ["tenant_id", "app_id", "status"])

    for table in _TABLES:
        _apply_rls(table)

    # Per-tenant autonomy flags — BOTH default OFF (fail-closed). Branch walks
    # and the autowalk loop also require their env-level switches; a tenant
    # flag alone never acts.
    op.add_column(
        "tenant_provisioning",
        sa.Column("branch_walks_enabled", sa.Boolean, nullable=False,
                  server_default=sa.text("false")),
    )
    op.add_column(
        "tenant_provisioning",
        sa.Column("journey_autowalk", sa.Boolean, nullable=False,
                  server_default=sa.text("false")),
    )


def downgrade() -> None:
    op.drop_column("tenant_provisioning", "journey_autowalk")
    op.drop_column("tenant_provisioning", "branch_walks_enabled")
    for table in reversed(_TABLES):
        _drop_rls(table)
    op.drop_table("journey_branches")
    op.drop_table("journey_traversals")
    op.drop_table("journey_edges")
    op.drop_table("journey_nodes")
    op.drop_table("journeys")
