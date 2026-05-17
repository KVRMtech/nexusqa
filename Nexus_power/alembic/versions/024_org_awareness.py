"""Org awareness — SCIM directory, subscriptions, gaps, channel policies.

Introduces the tables Phase 6 needs to make the echo platform
organisationally aware:

  * ``org_users`` / ``org_groups`` / ``org_user_groups`` — projection
    of the tenant's identity provider (Okta / Azure AD / Ping / …)
    via SCIM v2.0 provisioning. The platform never writes user data
    that didn't originate from SCIM.
  * ``topic_subscriptions`` — per-user opt-in / opt-out routing of
    echoes by topic, product, card, jurisdiction, or channel.
  * ``knowledge_gaps`` (+ ``knowledge_gap_questions``) — aggregated
    "questions we couldn't answer well" — drives the gap dashboard
    and the suggested-KT workflow.
  * ``surface_channel_policies`` — per-(surface, channel) overrides
    that the orchestrator consults before any dispatch.

All tables are tenant-scoped under the same RLS regime introduced
in migration 010.

Revision ID: 024_org_awareness
Revises: 023_product_atlas
Create Date: 2026-05-13
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB


revision: str = "024_org_awareness"
down_revision: Union[str, None] = "023_product_atlas"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TENANT_TABLES: tuple[str, ...] = (
    "org_users",
    "org_groups",
    "org_user_groups",
    "topic_subscriptions",
    "knowledge_gaps",
    "knowledge_gap_questions",
    "surface_channel_policies",
)


def upgrade() -> None:
    # ── org_users ───────────────────────────────────────────────
    op.create_table(
        "org_users",
        sa.Column("org_user_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("external_id", sa.String(128), nullable=False),
        sa.Column("user_name", sa.String(256), nullable=False),
        sa.Column("display_name", sa.String(256)),
        sa.Column("email", sa.String(256)),
        sa.Column(
            "active",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column("department", sa.String(128)),
        sa.Column("team", sa.String(128)),
        sa.Column("region", sa.String(128)),
        sa.Column("location", sa.String(128)),
        sa.Column("role", sa.String(128)),
        sa.Column("title", sa.String(256)),
        sa.Column("hire_date", sa.Date),
        sa.Column("manager_org_user_id", sa.String(64)),
        sa.Column("jurisdiction", sa.String(64)),
        sa.Column(
            "external_ids",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
            comment="External handles by surface, e.g. {\"slack\": \"U0123\"}.",
        ),
        sa.Column(
            "scim_metadata",
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
        ),
        sa.UniqueConstraint(
            "tenant_id", "external_id", name="uq_org_users_tenant_external"
        ),
        sa.UniqueConstraint(
            "tenant_id", "user_name", name="uq_org_users_tenant_username"
        ),
    )
    op.create_index(
        "ix_org_users_tenant_active",
        "org_users",
        ["tenant_id", "active"],
    )
    op.create_index(
        "ix_org_users_tenant_email",
        "org_users",
        ["tenant_id", "email"],
        postgresql_where=sa.text("email IS NOT NULL"),
    )
    op.create_index(
        "ix_org_users_manager",
        "org_users",
        ["manager_org_user_id"],
        postgresql_where=sa.text("manager_org_user_id IS NOT NULL"),
    )
    op.create_foreign_key(
        "fk_org_users_manager",
        "org_users",
        "org_users",
        ["manager_org_user_id"],
        ["org_user_id"],
        ondelete="SET NULL",
    )

    # ── org_groups ──────────────────────────────────────────────
    op.create_table(
        "org_groups",
        sa.Column("org_group_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("external_id", sa.String(128), nullable=False),
        sa.Column("display_name", sa.String(256), nullable=False),
        sa.Column("description", sa.Text),
        sa.Column(
            "group_kind",
            sa.String(32),
            nullable=False,
            server_default=sa.text("'custom'"),
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
            "group_kind IN ('department','team','region','role','custom')",
            name="ck_org_groups_kind",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "external_id",
            name="uq_org_groups_tenant_external",
        ),
    )
    op.create_index(
        "ix_org_groups_tenant_kind",
        "org_groups",
        ["tenant_id", "group_kind"],
    )

    # ── org_user_groups ─────────────────────────────────────────
    op.create_table(
        "org_user_groups",
        sa.Column("tenant_id", sa.String(64), primary_key=True),
        sa.Column("org_user_id", sa.String(64), primary_key=True),
        sa.Column("org_group_id", sa.String(64), primary_key=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["org_user_id"],
            ["org_users.org_user_id"],
            ondelete="CASCADE",
            name="fk_oug_user",
        ),
        sa.ForeignKeyConstraint(
            ["org_group_id"],
            ["org_groups.org_group_id"],
            ondelete="CASCADE",
            name="fk_oug_group",
        ),
    )
    op.create_index(
        "ix_oug_user",
        "org_user_groups",
        ["tenant_id", "org_user_id"],
    )
    op.create_index(
        "ix_oug_group",
        "org_user_groups",
        ["tenant_id", "org_group_id"],
    )

    # ── topic_subscriptions ─────────────────────────────────────
    op.create_table(
        "topic_subscriptions",
        sa.Column("subscription_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("org_user_id", sa.String(64), nullable=False),
        sa.Column(
            "subscription_kind",
            sa.String(32),
            nullable=False,
            comment="topic | product | card | jurisdiction | channel",
        ),
        sa.Column("target_id", sa.String(256), nullable=False),
        sa.Column(
            "mode",
            sa.String(32),
            nullable=False,
            server_default=sa.text("'all'"),
        ),
        sa.Column(
            "delivery_surface",
            sa.String(32),
            nullable=False,
            server_default=sa.text("'slack'"),
        ),
        sa.Column("delivery_address", sa.String(256)),
        sa.Column(
            "bootstrap_source",
            sa.String(32),
            nullable=False,
            server_default=sa.text("'manual'"),
        ),
        sa.Column(
            "metadata_json",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "active",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("true"),
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
            "subscription_kind IN ('topic','product','card','jurisdiction','channel')",
            name="ck_sub_kind",
        ),
        sa.CheckConstraint(
            "mode IN ('all','high_confidence_only','mute')",
            name="ck_sub_mode",
        ),
        sa.CheckConstraint(
            "delivery_surface IN ('slack','teams','email','webhook')",
            name="ck_sub_surface",
        ),
        sa.CheckConstraint(
            "bootstrap_source IN ('manual','role','team','manager_chain','sme','admin')",
            name="ck_sub_bootstrap",
        ),
        sa.ForeignKeyConstraint(
            ["org_user_id"],
            ["org_users.org_user_id"],
            ondelete="CASCADE",
            name="fk_sub_user",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "org_user_id",
            "subscription_kind",
            "target_id",
            name="uq_sub_quad",
        ),
    )
    op.create_index(
        "ix_sub_tenant_user_active",
        "topic_subscriptions",
        ["tenant_id", "org_user_id"],
        postgresql_where=sa.text("active = true"),
    )
    op.create_index(
        "ix_sub_tenant_target_active",
        "topic_subscriptions",
        ["tenant_id", "subscription_kind", "target_id"],
        postgresql_where=sa.text("active = true"),
    )

    # ── knowledge_gaps ──────────────────────────────────────────
    op.create_table(
        "knowledge_gaps",
        sa.Column("gap_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("topic_hash", sa.String(64), nullable=False),
        sa.Column("topic_label", sa.String(256), nullable=False),
        sa.Column("topic_summary", sa.Text, nullable=False),
        sa.Column(
            "question_count",
            sa.Integer,
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "unique_askers_count",
            sa.Integer,
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "product_ids",
            ARRAY(sa.String(64)),
            nullable=False,
            server_default=sa.text("ARRAY[]::varchar[]"),
        ),
        sa.Column(
            "suggested_sme_ids",
            ARRAY(sa.String(128)),
            nullable=False,
            server_default=sa.text("ARRAY[]::varchar[]"),
        ),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default=sa.text("'open'"),
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
        ),
        sa.CheckConstraint(
            "status IN ('open','addressed','scheduled','archived')",
            name="ck_gap_status",
        ),
        sa.UniqueConstraint(
            "tenant_id", "topic_hash", name="uq_gap_tenant_topic"
        ),
    )
    op.create_index(
        "ix_gap_tenant_status",
        "knowledge_gaps",
        ["tenant_id", "status"],
    )
    op.create_index(
        "ix_gap_tenant_last_seen",
        "knowledge_gaps",
        ["tenant_id", sa.text("last_seen_at DESC")],
    )

    # ── knowledge_gap_questions ─────────────────────────────────
    op.create_table(
        "knowledge_gap_questions",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("gap_id", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("echo_dispatch_id", sa.String(64), nullable=True),
        sa.Column("asker_user_id_ext", sa.String(128), nullable=True),
        sa.Column("asker_org_user_id", sa.String(64), nullable=True),
        sa.Column("question_text_redacted", sa.Text, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["gap_id"],
            ["knowledge_gaps.gap_id"],
            ondelete="CASCADE",
            name="fk_gapq_gap",
        ),
    )
    op.create_index(
        "ix_gapq_gap",
        "knowledge_gap_questions",
        ["gap_id"],
    )
    op.create_index(
        "ix_gapq_tenant_time",
        "knowledge_gap_questions",
        ["tenant_id", sa.text("created_at DESC")],
    )

    # ── surface_channel_policies ────────────────────────────────
    op.create_table(
        "surface_channel_policies",
        sa.Column("tenant_id", sa.String(64), primary_key=True),
        sa.Column("surface", sa.String(32), primary_key=True),
        sa.Column("channel_id_ext", sa.String(128), primary_key=True),
        sa.Column(
            "echo_mode",
            sa.String(16),
            nullable=False,
            server_default=sa.text("'inherit'"),
            comment="live|dm_only|shadow|muted|inherit",
        ),
        sa.Column("min_confidence_override", sa.Float),
        sa.Column(
            "allowlist_json",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "blocklist_json",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "quiet_hours_json",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "metadata_json",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("updated_by", sa.String(128)),
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
            "echo_mode IN ('live','dm_only','shadow','muted','inherit')",
            name="ck_policy_mode",
        ),
        sa.CheckConstraint(
            "surface IN ('slack','teams','email','webhook')",
            name="ck_policy_surface",
        ),
        sa.CheckConstraint(
            "min_confidence_override IS NULL "
            "OR (min_confidence_override >= 0.0 AND min_confidence_override <= 1.0)",
            name="ck_policy_min_conf",
        ),
    )
    op.create_index(
        "ix_policy_tenant_surface",
        "surface_channel_policies",
        ["tenant_id", "surface"],
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

    # ── Trigger for org_users updated_at + version ─────────────
    op.execute(
        """
        CREATE OR REPLACE FUNCTION nexus_org_users_touch() RETURNS trigger AS $$
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
        CREATE TRIGGER org_users_touch_before_update
            BEFORE UPDATE ON org_users
            FOR EACH ROW EXECUTE FUNCTION nexus_org_users_touch();
        """
    )

    # Trigger for knowledge_gaps too.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION nexus_gaps_touch() RETURNS trigger AS $$
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
        CREATE TRIGGER gaps_touch_before_update
            BEFORE UPDATE ON knowledge_gaps
            FOR EACH ROW EXECUTE FUNCTION nexus_gaps_touch();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS gaps_touch_before_update ON knowledge_gaps;")
    op.execute("DROP FUNCTION IF EXISTS nexus_gaps_touch();")
    op.execute("DROP TRIGGER IF EXISTS org_users_touch_before_update ON org_users;")
    op.execute("DROP FUNCTION IF EXISTS nexus_org_users_touch();")

    for table in reversed(_TENANT_TABLES):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table};")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY;")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;")

    op.drop_index("ix_policy_tenant_surface", table_name="surface_channel_policies")
    op.drop_table("surface_channel_policies")

    op.drop_index("ix_gapq_tenant_time", table_name="knowledge_gap_questions")
    op.drop_index("ix_gapq_gap", table_name="knowledge_gap_questions")
    op.drop_table("knowledge_gap_questions")

    op.drop_index("ix_gap_tenant_last_seen", table_name="knowledge_gaps")
    op.drop_index("ix_gap_tenant_status", table_name="knowledge_gaps")
    op.drop_table("knowledge_gaps")

    op.drop_index("ix_sub_tenant_target_active", table_name="topic_subscriptions")
    op.drop_index("ix_sub_tenant_user_active", table_name="topic_subscriptions")
    op.drop_table("topic_subscriptions")

    op.drop_index("ix_oug_group", table_name="org_user_groups")
    op.drop_index("ix_oug_user", table_name="org_user_groups")
    op.drop_table("org_user_groups")

    op.drop_index("ix_org_groups_tenant_kind", table_name="org_groups")
    op.drop_table("org_groups")

    op.drop_constraint("fk_org_users_manager", "org_users", type_="foreignkey")
    op.drop_index("ix_org_users_manager", table_name="org_users")
    op.drop_index("ix_org_users_tenant_email", table_name="org_users")
    op.drop_index("ix_org_users_tenant_active", table_name="org_users")
    op.drop_table("org_users")
