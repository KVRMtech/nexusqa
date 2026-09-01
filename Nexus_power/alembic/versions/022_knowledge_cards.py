"""Knowledge Cards — multi-SME synthesis layer.

A ``knowledge_card`` is a persistent, evolving, multi-sourced answer
object that consolidates everything one or more SMEs ever said about a
topic. The card carries:

* a ``canonical_statement`` (the answer Phase 2 / echo dispatches use),
* ``consensus_score`` (agreement among contributing sources),
* ``authority_chain`` (who confirmed, their roles, when),
* a lifecycle state machine (tribal → consensus → canonical → deprecated,
  with ``contested`` for disagreement),
* halflife metadata for "verify or expire" workflows,
* an ``authority_matrix`` per tenant (role weights).

The card may have a Backbone graph node (``backbone_node_id``) so it
participates in semantic search alongside ``TranscriptSegment`` and
``BusinessRule``.

Revision ID: 022_knowledge_cards
Revises: 021_echo_mvp
Create Date: 2026-05-12
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB


revision: str = "022_knowledge_cards"
down_revision: Union[str, None] = "021_echo_mvp"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TENANT_TABLES: tuple[str, ...] = (
    "knowledge_cards",
    "knowledge_card_sources",
    "knowledge_card_history",
    "tenant_authority_matrix",
)


def upgrade() -> None:
    # ── tenant_authority_matrix ─────────────────────────────────
    #
    # Per-tenant role → weight overrides for authority computation.
    # Defaults live in code; this table only stores explicit overrides.
    op.create_table(
        "tenant_authority_matrix",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("role", sa.String(128), nullable=False),
        sa.Column(
            "weight",
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
        sa.CheckConstraint("weight > 0.0", name="ck_auth_weight_positive"),
        sa.UniqueConstraint("tenant_id", "role", name="uq_auth_tenant_role"),
    )
    op.create_index(
        "ix_auth_tenant", "tenant_authority_matrix", ["tenant_id"]
    )

    # ── knowledge_cards ─────────────────────────────────────────
    op.create_table(
        "knowledge_cards",
        sa.Column("card_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("topic_slug", sa.String(256), nullable=False),
        sa.Column("topic_label", sa.String(512), nullable=False),
        sa.Column("canonical_statement", sa.Text, nullable=False),
        sa.Column(
            "canonical_confidence",
            sa.Float,
            nullable=False,
            server_default=sa.text("0.0"),
        ),
        sa.Column(
            "consensus_score",
            sa.Float,
            nullable=False,
            server_default=sa.text("0.0"),
        ),
        sa.Column(
            "lifecycle_state",
            sa.String(16),
            nullable=False,
            server_default=sa.text("'tribal'"),
        ),
        sa.Column(
            "authority_chain",
            JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "contributing_count",
            sa.Integer,
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "dissent_count",
            sa.Integer,
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("product_id", sa.String(64), nullable=True),
        sa.Column("validity_start", sa.Date, nullable=True),
        sa.Column("validity_end", sa.Date, nullable=True),
        sa.Column("jurisdiction", sa.String(64), nullable=True),
        sa.Column("superseded_by", sa.String(64), nullable=True),
        sa.Column(
            "halflife_days",
            sa.Integer,
            nullable=False,
            server_default=sa.text("270"),
        ),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verify_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("backbone_node_id", sa.String(64), nullable=True),
        sa.Column(
            "tags",
            ARRAY(sa.String(128)),
            nullable=False,
            server_default=sa.text("ARRAY[]::varchar[]"),
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
            comment="Optimistic concurrency control.",
        ),
        sa.CheckConstraint(
            "lifecycle_state IN ('tribal','consensus','canonical','deprecated','contested')",
            name="ck_kc_state",
        ),
        sa.CheckConstraint(
            "canonical_confidence >= 0.0 AND canonical_confidence <= 1.0",
            name="ck_kc_conf",
        ),
        sa.CheckConstraint(
            "consensus_score >= 0.0 AND consensus_score <= 1.0",
            name="ck_kc_consensus",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.product_id"],
            ondelete="SET NULL",
            name="fk_kc_product",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "topic_slug",
            name="uq_kc_topic_slug",
        ),
    )
    op.create_index(
        "ix_kc_tenant_state",
        "knowledge_cards",
        ["tenant_id", "lifecycle_state"],
    )
    op.create_index(
        "ix_kc_tenant_product",
        "knowledge_cards",
        ["tenant_id", "product_id"],
    )
    op.create_index(
        "ix_kc_verify_due",
        "knowledge_cards",
        ["verify_due_at"],
        postgresql_where=sa.text("verify_due_at IS NOT NULL"),
    )
    op.create_index(
        "ix_kc_tags_gin",
        "knowledge_cards",
        ["tags"],
        postgresql_using="gin",
    )

    # Self-FK for superseded_by — added after table create so the
    # constraint can reference the same table.
    op.create_foreign_key(
        "fk_kc_superseded",
        "knowledge_cards",
        "knowledge_cards",
        ["superseded_by"],
        ["card_id"],
        ondelete="SET NULL",
    )

    # Updated-at + version trigger
    op.execute(
        """
        CREATE OR REPLACE FUNCTION nexus_kc_touch() RETURNS trigger AS $$
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
        CREATE TRIGGER kc_touch_before_update
            BEFORE UPDATE ON knowledge_cards
            FOR EACH ROW EXECUTE FUNCTION nexus_kc_touch();
        """
    )

    # ── knowledge_card_sources ─────────────────────────────────
    #
    # Provenance: every contributing segment/rule/doc that shaped the
    # card, including its weight and lifecycle status within the card.
    op.create_table(
        "knowledge_card_sources",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("card_id", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("source_id", sa.String(64), nullable=False),
        sa.Column("backbone_node_id", sa.String(64), nullable=True),
        sa.Column("session_id", sa.String(64), nullable=True),
        sa.Column("artifact_id", sa.String(64), nullable=True),
        sa.Column("sme_id", sa.String(128), nullable=True),
        sa.Column("sme_role", sa.String(128), nullable=True),
        sa.Column("stated_at", sa.Date, nullable=True),
        sa.Column(
            "similarity_to_canonical", sa.Float, nullable=True
        ),
        sa.Column(
            "weight",
            sa.Float,
            nullable=False,
            server_default=sa.text("1.0"),
        ),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default=sa.text("'active'"),
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
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "status IN ('active','superseded','dissenting','retracted')",
            name="ck_kcs_status",
        ),
        sa.CheckConstraint(
            "source_type IN ('segment','rule','doc','manual','external')",
            name="ck_kcs_source_type",
        ),
        sa.ForeignKeyConstraint(
            ["card_id"],
            ["knowledge_cards.card_id"],
            ondelete="CASCADE",
            name="fk_kcs_card",
        ),
        sa.UniqueConstraint(
            "card_id",
            "source_type",
            "source_id",
            name="uq_kcs_card_source",
        ),
    )
    op.create_index("ix_kcs_card", "knowledge_card_sources", ["card_id"])
    op.create_index(
        "ix_kcs_tenant_status",
        "knowledge_card_sources",
        ["tenant_id", "status"],
    )
    op.create_index(
        "ix_kcs_dissent",
        "knowledge_card_sources",
        ["card_id"],
        postgresql_where=sa.text("status = 'dissenting'"),
    )

    # ── knowledge_card_history ─────────────────────────────────
    op.create_table(
        "knowledge_card_history",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("card_id", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("change_type", sa.String(32), nullable=False),
        sa.Column("changed_by", sa.String(128), nullable=True),
        sa.Column("snapshot", JSONB, nullable=False),
        sa.Column("note", sa.String(512), nullable=True),
        sa.Column(
            "changed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "change_type IN ("
            "'created','source_added','source_status_changed','superseded',"
            "'promoted','demoted','marked_contested','verified','renamed',"
            "'canonical_updated','tags_updated','metadata_updated'"
            ")",
            name="ck_kch_change",
        ),
        sa.ForeignKeyConstraint(
            ["card_id"],
            ["knowledge_cards.card_id"],
            ondelete="CASCADE",
            name="fk_kch_card",
        ),
    )
    op.create_index(
        "ix_kch_card_time",
        "knowledge_card_history",
        ["card_id", sa.text("changed_at DESC")],
    )
    op.create_index(
        "ix_kch_tenant_change",
        "knowledge_card_history",
        ["tenant_id", "change_type"],
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


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS kc_touch_before_update ON knowledge_cards;"
    )
    op.execute("DROP FUNCTION IF EXISTS nexus_kc_touch();")

    for table in reversed(_TENANT_TABLES):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table};")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY;")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;")

    op.drop_index("ix_kch_tenant_change", table_name="knowledge_card_history")
    op.drop_index("ix_kch_card_time", table_name="knowledge_card_history")
    op.drop_table("knowledge_card_history")

    op.drop_index("ix_kcs_dissent", table_name="knowledge_card_sources")
    op.drop_index("ix_kcs_tenant_status", table_name="knowledge_card_sources")
    op.drop_index("ix_kcs_card", table_name="knowledge_card_sources")
    op.drop_table("knowledge_card_sources")

    op.drop_constraint(
        "fk_kc_superseded", "knowledge_cards", type_="foreignkey"
    )
    op.drop_index("ix_kc_tags_gin", table_name="knowledge_cards")
    op.drop_index("ix_kc_verify_due", table_name="knowledge_cards")
    op.drop_index("ix_kc_tenant_product", table_name="knowledge_cards")
    op.drop_index("ix_kc_tenant_state", table_name="knowledge_cards")
    op.drop_table("knowledge_cards")

    op.drop_index("ix_auth_tenant", table_name="tenant_authority_matrix")
    op.drop_table("tenant_authority_matrix")
