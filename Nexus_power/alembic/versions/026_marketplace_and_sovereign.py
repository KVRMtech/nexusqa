"""Phase 8 — Plugin marketplace + sovereign tier infrastructure.

Tables:

  * ``marketplace_listings`` / ``marketplace_listing_versions`` —
    publicly-discoverable plugin metadata + per-version manifests +
    security-review state.
  * ``marketplace_install_requests`` — workflow rows tracking a
    tenant's request to install a marketplace plugin (gated on
    review status when the plugin is non-core).
  * ``tenant_tiers`` — per-tenant tier (standard / pro / sovereign)
    plus BYOK + compliance metadata.
  * ``tenant_relationships`` — directed parent/child edges enabling
    hierarchical tenants (holding company → subsidiaries) and
    knowledge-sharing scopes.
  * ``compliance_evidence_exports`` — append-only history of audit
    bundle exports.
  * ``telemetry_optout`` — per-tenant opt-out for cross-tenant pattern
    learning.

All tenant-scoped tables receive the standard RLS policy. The
``marketplace_listings`` + ``marketplace_listing_versions`` rows are
global (no tenant_id) — the marketplace is the same view across all
tenants — but writes are operator-gated.

Revision ID: 026_marketplace_and_sovereign
Revises: 025_action_layer
Create Date: 2026-05-13
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB


revision: str = "026_marketplace_and_sovereign"
down_revision: Union[str, None] = "025_action_layer"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TENANT_TABLES: tuple[str, ...] = (
    "marketplace_install_requests",
    "tenant_tiers",
    "tenant_relationships",
    "compliance_evidence_exports",
    "telemetry_optout",
)


def upgrade() -> None:
    # ── marketplace_listings (global, not tenant-scoped) ───────
    op.create_table(
        "marketplace_listings",
        sa.Column("listing_id", sa.String(64), primary_key=True),
        sa.Column("plugin_id", sa.String(128), nullable=False),
        sa.Column("display_name", sa.String(256), nullable=False),
        sa.Column("vendor", sa.String(128), nullable=False),
        sa.Column(
            "tier",
            sa.String(16),
            nullable=False,
            server_default=sa.text("'standard'"),
        ),
        sa.Column("description", sa.Text),
        sa.Column(
            "tags",
            ARRAY(sa.String(64)),
            nullable=False,
            server_default=sa.text("ARRAY[]::varchar[]"),
        ),
        sa.Column("documentation_url", sa.String(512)),
        sa.Column("repository_url", sa.String(512)),
        sa.Column("support_contact", sa.String(256)),
        sa.Column(
            "review_state",
            sa.String(16),
            nullable=False,
            server_default=sa.text("'draft'"),
        ),
        sa.Column(
            "is_core",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
            comment="First-party (vendor=nexus-core) listings are auto-approved.",
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
            "tier IN ('standard','enterprise','sovereign','community')",
            name="ck_listing_tier",
        ),
        sa.CheckConstraint(
            "review_state IN ('draft','submitted','in_review','approved','rejected','withdrawn')",
            name="ck_listing_review_state",
        ),
        sa.UniqueConstraint("plugin_id", name="uq_listing_plugin"),
    )
    op.create_index(
        "ix_listing_review_state",
        "marketplace_listings",
        ["review_state"],
    )
    op.create_index(
        "ix_listing_tags_gin",
        "marketplace_listings",
        ["tags"],
        postgresql_using="gin",
    )

    # ── marketplace_listing_versions (global) ──────────────────
    op.create_table(
        "marketplace_listing_versions",
        sa.Column("version_id", sa.String(64), primary_key=True),
        sa.Column("listing_id", sa.String(64), nullable=False),
        sa.Column("version", sa.String(32), nullable=False),
        sa.Column(
            "manifest_yaml",
            sa.Text,
            nullable=False,
            comment="Validated against the Phase 0 manifest schema.",
        ),
        sa.Column(
            "manifest_json",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("manifest_sha256", sa.String(64), nullable=False),
        sa.Column(
            "review_state",
            sa.String(16),
            nullable=False,
            server_default=sa.text("'submitted'"),
        ),
        sa.Column("reviewed_by", sa.String(128)),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.Column("review_notes", sa.Text),
        sa.Column(
            "published_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "yanked_at",
            sa.DateTime(timezone=True),
            comment="If set, this version was retracted after publish.",
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
        sa.CheckConstraint(
            "review_state IN ('submitted','in_review','approved','rejected','published','yanked')",
            name="ck_listing_version_state",
        ),
        sa.ForeignKeyConstraint(
            ["listing_id"],
            ["marketplace_listings.listing_id"],
            ondelete="CASCADE",
            name="fk_listing_version_listing",
        ),
        sa.UniqueConstraint(
            "listing_id", "version", name="uq_listing_version"
        ),
    )
    op.create_index(
        "ix_listing_version_state",
        "marketplace_listing_versions",
        ["listing_id", "review_state"],
    )

    # ── marketplace_install_requests (tenant-scoped) ───────────
    op.create_table(
        "marketplace_install_requests",
        sa.Column("request_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("listing_id", sa.String(64), nullable=False),
        sa.Column("version_id", sa.String(64), nullable=False),
        sa.Column("requested_by", sa.String(128)),
        sa.Column(
            "state",
            sa.String(16),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column(
            "scopes_requested",
            ARRAY(sa.String(64)),
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
        sa.Column("decided_by", sa.String(128)),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("note", sa.String(512)),
        sa.CheckConstraint(
            "state IN ('pending','approved','rejected','auto_approved','installed','revoked')",
            name="ck_install_req_state",
        ),
        sa.ForeignKeyConstraint(
            ["listing_id"],
            ["marketplace_listings.listing_id"],
            ondelete="CASCADE",
            name="fk_install_req_listing",
        ),
        sa.ForeignKeyConstraint(
            ["version_id"],
            ["marketplace_listing_versions.version_id"],
            ondelete="CASCADE",
            name="fk_install_req_version",
        ),
    )
    op.create_index(
        "ix_install_req_tenant_state",
        "marketplace_install_requests",
        ["tenant_id", "state"],
    )

    # ── tenant_tiers ────────────────────────────────────────────
    #
    # The tenant_id is the platform-API's notion of a tenant; we don't
    # FK to a tenants table because the platform uses string tenant_ids
    # opaquely. Each row carries the tier + per-tier configuration.
    op.create_table(
        "tenant_tiers",
        sa.Column("tenant_id", sa.String(64), primary_key=True),
        sa.Column(
            "tier",
            sa.String(16),
            nullable=False,
            server_default=sa.text("'standard'"),
        ),
        sa.Column(
            "compliance_regimes",
            ARRAY(sa.String(32)),
            nullable=False,
            server_default=sa.text("ARRAY[]::varchar[]"),
            comment="Tags like 'hipaa', 'glba', 'sox', 'fedramp_moderate'.",
        ),
        sa.Column(
            "data_residency",
            sa.String(32),
            nullable=False,
            server_default=sa.text("'us'"),
        ),
        sa.Column(
            "byok_required",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("byok_kek_uri", sa.String(512)),
        sa.Column(
            "audit_retention_days",
            sa.Integer,
            nullable=False,
            server_default=sa.text("365"),
        ),
        sa.Column(
            "telemetry_opt_in",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("true"),
            comment="When false, this tenant is excluded from cross-tenant pattern learning.",
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
            "tier IN ('standard','pro','sovereign','community')",
            name="ck_tenant_tier",
        ),
        sa.CheckConstraint(
            "audit_retention_days >= 30",
            name="ck_tenant_audit_retention_min",
        ),
    )

    # ── tenant_relationships ───────────────────────────────────
    #
    # Directed parent → child edges. A holdco tenant can have many
    # subsidiary tenants; the ``share_scope`` controls whether the
    # parent's knowledge can be consumed by the child.
    op.create_table(
        "tenant_relationships",
        sa.Column("tenant_id", sa.String(64), primary_key=True),
        sa.Column("related_tenant_id", sa.String(64), primary_key=True),
        sa.Column("relationship_kind", sa.String(32), primary_key=True),
        sa.Column(
            "share_scope",
            sa.String(32),
            nullable=False,
            server_default=sa.text("'none'"),
            comment="What knowledge flows: none|public|cards|atlas|all",
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
        sa.Column("updated_by", sa.String(128)),
        sa.CheckConstraint(
            "relationship_kind IN ('parent','child','peer')",
            name="ck_rel_kind",
        ),
        sa.CheckConstraint(
            "share_scope IN ('none','public','cards','atlas','all')",
            name="ck_rel_share_scope",
        ),
        sa.CheckConstraint(
            "tenant_id <> related_tenant_id",
            name="ck_rel_distinct",
        ),
    )
    op.create_index(
        "ix_rel_related",
        "tenant_relationships",
        ["related_tenant_id"],
    )

    # ── compliance_evidence_exports ─────────────────────────────
    op.create_table(
        "compliance_evidence_exports",
        sa.Column("export_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("requested_by", sa.String(128), nullable=False),
        sa.Column(
            "period_start",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "period_end",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "scopes",
            ARRAY(sa.String(32)),
            nullable=False,
            server_default=sa.text("ARRAY[]::varchar[]"),
            comment="Which audit slices to include: echoes, plugin_events, scim, atlas, ...",
        ),
        sa.Column(
            "manifest",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
            comment="Counts + storage refs for each scope.",
        ),
        sa.Column("manifest_sha256", sa.String(64), nullable=False),
        sa.Column("signature", sa.String(256)),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("storage_uri", sa.String(512)),
        sa.CheckConstraint(
            "status IN ('pending','running','succeeded','failed')",
            name="ck_export_status",
        ),
        sa.CheckConstraint(
            "period_end > period_start",
            name="ck_export_period",
        ),
    )
    op.create_index(
        "ix_export_tenant_time",
        "compliance_evidence_exports",
        ["tenant_id", sa.text("created_at DESC")],
    )

    # ── telemetry_optout ───────────────────────────────────────
    op.create_table(
        "telemetry_optout",
        sa.Column("tenant_id", sa.String(64), primary_key=True),
        sa.Column("opted_out", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("opted_out_at", sa.DateTime(timezone=True)),
        sa.Column("opted_out_by", sa.String(128)),
        sa.Column(
            "categories",
            ARRAY(sa.String(32)),
            nullable=False,
            server_default=sa.text("ARRAY[]::varchar[]"),
            comment="Specific telemetry categories opted out of (empty = all).",
        ),
        sa.Column(
            "metadata_json",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
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

    # marketplace_listings + marketplace_listing_versions are global.
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON marketplace_listings TO nexus_app;"
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON marketplace_listing_versions TO nexus_app;"
    )

    # Update trigger for tenant_tiers.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION nexus_tenant_tiers_touch() RETURNS trigger AS $$
        BEGIN
            NEW.updated_at := now();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER tenant_tiers_touch_before_update
            BEFORE UPDATE ON tenant_tiers
            FOR EACH ROW EXECUTE FUNCTION nexus_tenant_tiers_touch();
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS tenant_tiers_touch_before_update ON tenant_tiers;"
    )
    op.execute("DROP FUNCTION IF EXISTS nexus_tenant_tiers_touch();")

    for table in reversed(_TENANT_TABLES):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table};")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY;")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;")

    op.drop_table("telemetry_optout")
    op.drop_index(
        "ix_export_tenant_time", table_name="compliance_evidence_exports"
    )
    op.drop_table("compliance_evidence_exports")
    op.drop_index("ix_rel_related", table_name="tenant_relationships")
    op.drop_table("tenant_relationships")
    op.drop_table("tenant_tiers")
    op.drop_index(
        "ix_install_req_tenant_state", table_name="marketplace_install_requests"
    )
    op.drop_table("marketplace_install_requests")
    op.drop_index(
        "ix_listing_version_state", table_name="marketplace_listing_versions"
    )
    op.drop_table("marketplace_listing_versions")
    op.drop_index("ix_listing_tags_gin", table_name="marketplace_listings")
    op.drop_index("ix_listing_review_state", table_name="marketplace_listings")
    op.drop_table("marketplace_listings")
