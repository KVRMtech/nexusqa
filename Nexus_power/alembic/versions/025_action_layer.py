"""Action layer — sandbox runs, synthesized tours, impact analyses.

These tables back the Phase 7 "answer → do" features:

  * ``action_invocations`` — audit + idempotency log for every action
    fired against an external system (sandbox run, GitHub PR, Confluence
    publish). One row per intent; status transitions in place.
  * ``synthesized_tours`` — saved tour playlists assembled from the
    atlas (Phase 5). Each row carries the persona-targeted playlist
    plus provenance.
  * ``impact_analyses`` — cached blast-radius reports for atlas nodes.
    Expensive to recompute, cheap to read.

All tables are tenant-scoped under the standard RLS regime.

Revision ID: 025_action_layer
Revises: 024_org_awareness
Create Date: 2026-05-13
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB


revision: str = "025_action_layer"
down_revision: Union[str, None] = "024_org_awareness"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TENANT_TABLES: tuple[str, ...] = (
    "action_invocations",
    "synthesized_tours",
    "impact_analyses",
)


def upgrade() -> None:
    # ── action_invocations ───────────────────────────────────────
    op.create_table(
        "action_invocations",
        sa.Column("invocation_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("trigger_dispatch_id", sa.String(64)),
        sa.Column("trigger_user_id", sa.String(128)),
        sa.Column("trace_id", sa.String(64)),
        sa.Column(
            "idempotency_key",
            sa.String(128),
            comment="Caller-supplied dedup key, optional.",
        ),
        sa.Column(
            "request",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "result",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default=sa.text("'queued'"),
        ),
        sa.Column("error", sa.Text),
        sa.Column("latency_ms", sa.Integer),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "kind IN ('sandbox_run','github_pr','confluence_publish','impact_analysis','tour_generate')",
            name="ck_invoke_kind",
        ),
        sa.CheckConstraint(
            "status IN ('queued','running','succeeded','failed','cancelled')",
            name="ck_invoke_status",
        ),
    )
    op.create_index(
        "ix_invoke_tenant_kind_time",
        "action_invocations",
        ["tenant_id", "kind", sa.text("created_at DESC")],
    )
    op.create_index(
        "ix_invoke_trace",
        "action_invocations",
        ["trace_id"],
        postgresql_where=sa.text("trace_id IS NOT NULL"),
    )
    op.create_index(
        "ix_invoke_idem",
        "action_invocations",
        ["tenant_id", "kind", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )

    # ── synthesized_tours ────────────────────────────────────────
    op.create_table(
        "synthesized_tours",
        sa.Column("tour_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("product_id", sa.String(64), nullable=False),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("persona", sa.String(64)),
        sa.Column("target_minutes", sa.Integer),
        sa.Column(
            "playlist",
            JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
            comment="Array of segments: {atlas_node_id, label, segment_ids, layer}",
        ),
        sa.Column(
            "coverage",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "atlas_node_ids",
            ARRAY(sa.String(64)),
            nullable=False,
            server_default=sa.text("ARRAY[]::varchar[]"),
        ),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default=sa.text("'draft'"),
        ),
        sa.Column("created_by", sa.String(128)),
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
        sa.CheckConstraint(
            "status IN ('draft','published','archived')",
            name="ck_tour_status",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.product_id"],
            ondelete="CASCADE",
            name="fk_tour_product",
        ),
    )
    op.create_index(
        "ix_tour_tenant_product",
        "synthesized_tours",
        ["tenant_id", "product_id"],
    )
    op.create_index(
        "ix_tour_tenant_status",
        "synthesized_tours",
        ["tenant_id", "status"],
    )
    op.create_index(
        "ix_tour_nodes_gin",
        "synthesized_tours",
        ["atlas_node_ids"],
        postgresql_using="gin",
    )

    # ── impact_analyses ──────────────────────────────────────────
    op.create_table(
        "impact_analyses",
        sa.Column("analysis_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("product_id", sa.String(64), nullable=False),
        sa.Column("root_atlas_node_id", sa.String(64), nullable=False),
        sa.Column("change_description", sa.Text),
        sa.Column(
            "downstream",
            JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "upstream",
            JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "layer_summary",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "estimated_blast_radius",
            sa.Integer,
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("requested_by", sa.String(128)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.product_id"],
            ondelete="CASCADE",
            name="fk_impact_product",
        ),
        sa.ForeignKeyConstraint(
            ["root_atlas_node_id"],
            ["atlas_nodes.atlas_node_id"],
            ondelete="CASCADE",
            name="fk_impact_root",
        ),
    )
    op.create_index(
        "ix_impact_tenant_product",
        "impact_analyses",
        ["tenant_id", "product_id"],
    )
    op.create_index(
        "ix_impact_root_time",
        "impact_analyses",
        ["root_atlas_node_id", sa.text("created_at DESC")],
    )
    op.create_index(
        "ix_impact_expiry",
        "impact_analyses",
        ["expires_at"],
        postgresql_where=sa.text("expires_at IS NOT NULL"),
    )

    # ── RLS ─────────────────────────────────────────────────────
    for table in _TENANT_TABLES:
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
        op.execute(
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO nexus_app;"
        )

    # ── Trigger for synthesized_tours.updated_at ────────────────
    op.execute(
        """
        CREATE OR REPLACE FUNCTION nexus_tours_touch() RETURNS trigger AS $$
        BEGIN
            NEW.updated_at := now();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER tours_touch_before_update
            BEFORE UPDATE ON synthesized_tours
            FOR EACH ROW EXECUTE FUNCTION nexus_tours_touch();
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS tours_touch_before_update ON synthesized_tours;"
    )
    op.execute("DROP FUNCTION IF EXISTS nexus_tours_touch();")

    for table in reversed(_TENANT_TABLES):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table};")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY;")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;")

    op.drop_index("ix_impact_expiry", table_name="impact_analyses")
    op.drop_index("ix_impact_root_time", table_name="impact_analyses")
    op.drop_index("ix_impact_tenant_product", table_name="impact_analyses")
    op.drop_table("impact_analyses")

    op.drop_index("ix_tour_nodes_gin", table_name="synthesized_tours")
    op.drop_index("ix_tour_tenant_status", table_name="synthesized_tours")
    op.drop_index("ix_tour_tenant_product", table_name="synthesized_tours")
    op.drop_table("synthesized_tours")

    op.drop_index("ix_invoke_idem", table_name="action_invocations")
    op.drop_index("ix_invoke_trace", table_name="action_invocations")
    op.drop_index("ix_invoke_tenant_kind_time", table_name="action_invocations")
    op.drop_table("action_invocations")
