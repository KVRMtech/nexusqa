"""Product Atlas — cross-layer projection of the Backbone graph.

The Atlas is the read-side view of a product's knowledge across
layers (experience / application / data / rule / test / ops /
compliance). Backbone remains the source of truth for the graph
+ vector store; the tables here are denormalised projections for
fast tenant-scoped queries (atlas page render, coverage stats,
impact analysis).

Tables introduced:

  * ``atlas_nodes``        — one row per (tenant, product, layer,
                              backbone_node_id) tuple.
  * ``atlas_edges``        — cross-layer relationships, with status
                              for the operator review pipeline.
  * ``atlas_alignments``   — pending alignment proposals awaiting
                              operator approval.
  * ``atlas_layer_stats``  — per-(product, layer) rollup for the UI.

All tables are tenant-scoped and receive the standard RLS policy.

Revision ID: 023_product_atlas
Revises: 022_knowledge_cards
Create Date: 2026-05-13
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB


revision: str = "023_product_atlas"
down_revision: Union[str, None] = "022_knowledge_cards"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TENANT_TABLES: tuple[str, ...] = (
    "atlas_nodes",
    "atlas_edges",
    "atlas_alignments",
    "atlas_layer_stats",
)


def upgrade() -> None:
    # ── atlas_nodes ─────────────────────────────────────────────
    op.create_table(
        "atlas_nodes",
        sa.Column("atlas_node_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("product_id", sa.String(64), nullable=False),
        sa.Column("backbone_node_id", sa.String(64), nullable=False),
        sa.Column("node_type", sa.String(64), nullable=False),
        sa.Column("layer", sa.String(16), nullable=False),
        sa.Column("label", sa.String(512), nullable=False),
        sa.Column(
            "source_session_ids",
            ARRAY(sa.String(64)),
            nullable=False,
            server_default=sa.text("ARRAY[]::varchar[]"),
        ),
        sa.Column(
            "source_artifact_ids",
            ARRAY(sa.String(64)),
            nullable=False,
            server_default=sa.text("ARRAY[]::varchar[]"),
        ),
        sa.Column(
            "source_segment_ids",
            ARRAY(sa.String(64)),
            nullable=False,
            server_default=sa.text("ARRAY[]::varchar[]"),
        ),
        sa.Column(
            "confidence",
            sa.Float,
            nullable=False,
            server_default=sa.text("1.0"),
        ),
        sa.Column(
            "metadata_json",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("last_seen_at", sa.DateTime(timezone=True)),
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
        sa.Column(
            "version",
            sa.Integer,
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.CheckConstraint(
            "layer IN ('experience','application','data','rule','test','ops','compliance')",
            name="ck_atlas_layer",
        ),
        sa.CheckConstraint(
            "confidence >= 0.0 AND confidence <= 1.0",
            name="ck_atlas_node_conf",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.product_id"],
            ondelete="CASCADE",
            name="fk_atlas_node_product",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "product_id",
            "backbone_node_id",
            name="uq_atlas_node_backbone",
        ),
    )
    op.create_index(
        "ix_atlas_nodes_product_layer",
        "atlas_nodes",
        ["tenant_id", "product_id", "layer"],
    )
    op.create_index(
        "ix_atlas_nodes_tenant_updated",
        "atlas_nodes",
        ["tenant_id", sa.text("updated_at DESC")],
    )
    op.create_index(
        "ix_atlas_nodes_segments_gin",
        "atlas_nodes",
        ["source_segment_ids"],
        postgresql_using="gin",
    )

    # ── atlas_edges ─────────────────────────────────────────────
    op.create_table(
        "atlas_edges",
        sa.Column("edge_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("product_id", sa.String(64), nullable=False),
        sa.Column("from_atlas_node_id", sa.String(64), nullable=False),
        sa.Column("to_atlas_node_id", sa.String(64), nullable=False),
        sa.Column("relation_type", sa.String(48), nullable=False),
        sa.Column(
            "confidence",
            sa.Float,
            nullable=False,
            server_default=sa.text("1.0"),
        ),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default=sa.text("'auto'"),
        ),
        sa.Column(
            "evidence_json",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("reviewed_by", sa.String(128), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
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
            "status IN ('auto','confirmed','pending_review','rejected')",
            name="ck_atlas_edge_status",
        ),
        sa.CheckConstraint(
            "confidence >= 0.0 AND confidence <= 1.0",
            name="ck_atlas_edge_conf",
        ),
        sa.CheckConstraint(
            "from_atlas_node_id <> to_atlas_node_id",
            name="ck_atlas_edge_distinct",
        ),
        sa.ForeignKeyConstraint(
            ["from_atlas_node_id"],
            ["atlas_nodes.atlas_node_id"],
            ondelete="CASCADE",
            name="fk_atlas_edge_from",
        ),
        sa.ForeignKeyConstraint(
            ["to_atlas_node_id"],
            ["atlas_nodes.atlas_node_id"],
            ondelete="CASCADE",
            name="fk_atlas_edge_to",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "from_atlas_node_id",
            "to_atlas_node_id",
            "relation_type",
            name="uq_atlas_edge_triple",
        ),
    )
    op.create_index(
        "ix_atlas_edges_product",
        "atlas_edges",
        ["tenant_id", "product_id"],
    )
    op.create_index(
        "ix_atlas_edges_from",
        "atlas_edges",
        ["from_atlas_node_id"],
    )
    op.create_index(
        "ix_atlas_edges_to",
        "atlas_edges",
        ["to_atlas_node_id"],
    )
    op.create_index(
        "ix_atlas_edges_pending",
        "atlas_edges",
        ["tenant_id"],
        postgresql_where=sa.text("status = 'pending_review'"),
    )

    # ── atlas_alignments ────────────────────────────────────────
    #
    # Proposals from the cross-modal aligner. High-confidence proposals
    # are auto-applied (status='auto_applied') and also written here for
    # audit; medium-confidence are 'pending' until an operator decides.
    op.create_table(
        "atlas_alignments",
        sa.Column("alignment_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("product_id", sa.String(64), nullable=False),
        sa.Column("from_atlas_node_id", sa.String(64), nullable=False),
        sa.Column("to_atlas_node_id", sa.String(64), nullable=False),
        sa.Column("suggested_relation", sa.String(48), nullable=False),
        sa.Column("similarity", sa.Float),
        sa.Column(
            "evidence_json",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("decided_by", sa.String(128), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("note", sa.String(512), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "status IN ('pending','approved','rejected','auto_applied','superseded')",
            name="ck_atlas_align_status",
        ),
        sa.CheckConstraint(
            "from_atlas_node_id <> to_atlas_node_id",
            name="ck_atlas_align_distinct",
        ),
        sa.ForeignKeyConstraint(
            ["from_atlas_node_id"],
            ["atlas_nodes.atlas_node_id"],
            ondelete="CASCADE",
            name="fk_atlas_align_from",
        ),
        sa.ForeignKeyConstraint(
            ["to_atlas_node_id"],
            ["atlas_nodes.atlas_node_id"],
            ondelete="CASCADE",
            name="fk_atlas_align_to",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "from_atlas_node_id",
            "to_atlas_node_id",
            "suggested_relation",
            name="uq_atlas_align_triple",
        ),
    )
    op.create_index(
        "ix_atlas_align_pending",
        "atlas_alignments",
        ["tenant_id", "product_id"],
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "ix_atlas_align_created",
        "atlas_alignments",
        ["tenant_id", sa.text("created_at DESC")],
    )

    # ── atlas_layer_stats ───────────────────────────────────────
    op.create_table(
        "atlas_layer_stats",
        sa.Column("tenant_id", sa.String(64), primary_key=True),
        sa.Column("product_id", sa.String(64), primary_key=True),
        sa.Column("layer", sa.String(16), primary_key=True),
        sa.Column(
            "node_count",
            sa.Integer,
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "edge_count_in",
            sa.Integer,
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "edge_count_out",
            sa.Integer,
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("last_node_at", sa.DateTime(timezone=True)),
        sa.Column(
            "coverage_score",
            sa.Float,
            nullable=False,
            server_default=sa.text("0.0"),
        ),
        sa.Column(
            "metadata_json",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "layer IN ('experience','application','data','rule','test','ops','compliance')",
            name="ck_atlas_stats_layer",
        ),
        sa.CheckConstraint(
            "coverage_score >= 0.0 AND coverage_score <= 1.0",
            name="ck_atlas_stats_coverage",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.product_id"],
            ondelete="CASCADE",
            name="fk_atlas_stats_product",
        ),
    )
    op.create_index(
        "ix_atlas_stats_product",
        "atlas_layer_stats",
        ["tenant_id", "product_id"],
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
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO nexus_app;")

    # Trigger for atlas_nodes — bump updated_at + version on UPDATE.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION nexus_atlas_nodes_touch() RETURNS trigger AS $$
        BEGIN
            NEW.updated_at := now();
            NEW.version := COALESCE(OLD.version, 0) + 1;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER atlas_nodes_touch_before_update
            BEFORE UPDATE ON atlas_nodes
            FOR EACH ROW EXECUTE FUNCTION nexus_atlas_nodes_touch();
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS atlas_nodes_touch_before_update ON atlas_nodes;"
    )
    op.execute("DROP FUNCTION IF EXISTS nexus_atlas_nodes_touch();")

    for table in reversed(_TENANT_TABLES):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table};")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY;")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;")

    op.drop_index("ix_atlas_stats_product", table_name="atlas_layer_stats")
    op.drop_table("atlas_layer_stats")

    op.drop_index("ix_atlas_align_created", table_name="atlas_alignments")
    op.drop_index("ix_atlas_align_pending", table_name="atlas_alignments")
    op.drop_table("atlas_alignments")

    op.drop_index("ix_atlas_edges_pending", table_name="atlas_edges")
    op.drop_index("ix_atlas_edges_to", table_name="atlas_edges")
    op.drop_index("ix_atlas_edges_from", table_name="atlas_edges")
    op.drop_index("ix_atlas_edges_product", table_name="atlas_edges")
    op.drop_table("atlas_edges")

    op.drop_index("ix_atlas_nodes_segments_gin", table_name="atlas_nodes")
    op.drop_index("ix_atlas_nodes_tenant_updated", table_name="atlas_nodes")
    op.drop_index("ix_atlas_nodes_product_layer", table_name="atlas_nodes")
    op.drop_table("atlas_nodes")
