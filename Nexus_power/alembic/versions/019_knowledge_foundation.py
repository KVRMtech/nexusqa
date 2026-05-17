"""Knowledge Echo + Atlas foundation.

Purely additive — introduces feature flags, circuit breaker state, tenant
product catalog, integration installations, integration audit log, and
per-tenant KEK registry for envelope encryption.

No existing table or column is modified. All tenant-scoped tables receive
RLS policies that mirror the convention established in migration
``010_row_level_security``.

Revision ID: 019_knowledge_foundation
Revises: 018_ui_dictionary
Create Date: 2026-05-12
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, BYTEA, JSONB


revision: str = "019_knowledge_foundation"
down_revision: Union[str, None] = "018_ui_dictionary"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Tables created by this migration that hold a tenant_id and therefore
# participate in the row-level-security regime introduced in 010.
_TENANT_TABLES_THIS_MIGRATION: tuple[str, ...] = (
    "tenant_feature_flags",
    "feature_circuit_state",
    "products",
    "integration_installations",
    "integration_events_log",
    "tenant_keks",
)


def upgrade() -> None:
    # ── tenant_feature_flags ─────────────────────────────────────
    #
    # Per-tenant, per-feature gate with five-level toggle semantics:
    #   enabled  : master switch (False == feature inert)
    #   mode     : shadow | dm_only | live  — progression ladder
    #   config   : free-form JSON (channel allow-lists, thresholds, etc.)
    op.create_table(
        "tenant_feature_flags",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("feature_key", sa.String(128), nullable=False),
        sa.Column(
            "enabled", sa.Boolean, nullable=False, server_default=sa.text("false")
        ),
        sa.Column(
            "mode",
            sa.String(32),
            nullable=False,
            server_default=sa.text("'shadow'"),
        ),
        sa.Column(
            "config",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("enabled_by", sa.String(128), nullable=True),
        sa.Column(
            "enabled_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "disabled_at", sa.DateTime(timezone=True), nullable=True
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
            comment="Optimistic concurrency control; incremented on every update.",
        ),
        sa.CheckConstraint(
            "mode IN ('shadow','dm_only','live')",
            name="ck_tff_mode",
        ),
        sa.UniqueConstraint(
            "tenant_id", "feature_key", name="uq_tenant_feature"
        ),
    )
    op.create_index(
        "ix_tff_tenant", "tenant_feature_flags", ["tenant_id"]
    )
    op.create_index(
        "ix_tff_feature", "tenant_feature_flags", ["feature_key"]
    )

    # ── feature_circuit_state ────────────────────────────────────
    #
    # Persisted circuit-breaker state. closed = normal, open = treat
    # the feature as inert regardless of enabled/mode, half_open = a
    # single canary request is allowed through to test recovery.
    op.create_table(
        "feature_circuit_state",
        sa.Column("tenant_id", sa.String(64), primary_key=True),
        sa.Column("feature_key", sa.String(128), primary_key=True),
        sa.Column(
            "state",
            sa.String(16),
            nullable=False,
            server_default=sa.text("'closed'"),
        ),
        sa.Column(
            "trip_count",
            sa.Integer,
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "failure_count_window",
            sa.Integer,
            nullable=False,
            server_default=sa.text("0"),
            comment="Failures observed in the current sliding window.",
        ),
        sa.Column(
            "total_count_window",
            sa.Integer,
            nullable=False,
            server_default=sa.text("0"),
            comment="Total outcomes observed in the current sliding window.",
        ),
        sa.Column(
            "window_started_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "last_tripped_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column("last_trip_reason", sa.Text, nullable=True),
        sa.Column(
            "cooldown_until", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "state IN ('closed','open','half_open')",
            name="ck_circuit_state",
        ),
    )
    op.create_index(
        "ix_circuit_open",
        "feature_circuit_state",
        ["state"],
        postgresql_where=sa.text("state <> 'closed'"),
    )

    # ── products ─────────────────────────────────────────────────
    #
    # Tenant-scoped product catalog. Drives the Atlas: every ingested
    # demo is classified against one or more product_ids; every knowledge
    # card may be scoped to a product.
    op.create_table(
        "products",
        sa.Column("product_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("slug", sa.String(128), nullable=False),
        sa.Column(
            "aliases",
            ARRAY(sa.String(128)),
            nullable=False,
            server_default=sa.text("ARRAY[]::varchar[]"),
        ),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("owner_user_id", sa.String(128), nullable=True),
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
            "status IN ('active','deprecated','archived')",
            name="ck_product_status",
        ),
        sa.UniqueConstraint(
            "tenant_id", "slug", name="uq_product_tenant_slug"
        ),
    )
    op.create_index("ix_products_tenant", "products", ["tenant_id"])
    op.create_index(
        "ix_products_tenant_status",
        "products",
        ["tenant_id", "status"],
    )
    op.create_index(
        "ix_products_aliases_gin",
        "products",
        ["aliases"],
        postgresql_using="gin",
    )

    # ── integration_installations ───────────────────────────────
    #
    # Per-tenant connector enablement (Slack, Teams, email, webhook, …).
    # Distinct from the existing in-process DomainPlugin system in
    # ``nexus_sdk.plugins`` — those describe domain extensions; this
    # describes outbound/inbound system integrations.
    #
    # encrypted_credentials / dek_id are written via the envelope
    # encryption module — KEK lives in tenant_keks, DEK travels with
    # the row, plaintext never touches durable storage.
    op.create_table(
        "integration_installations",
        sa.Column("installation_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("integration_id", sa.String(128), nullable=False),
        sa.Column("integration_version", sa.String(32), nullable=False),
        sa.Column("display_name", sa.String(256), nullable=True),
        sa.Column(
            "config",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("encrypted_credentials", BYTEA, nullable=True),
        sa.Column("dek_id", sa.String(128), nullable=True),
        sa.Column("kek_id", sa.String(128), nullable=True),
        sa.Column(
            "scopes_granted",
            ARRAY(sa.String(64)),
            nullable=False,
            server_default=sa.text("ARRAY[]::varchar[]"),
        ),
        sa.Column(
            "status",
            sa.String(32),
            nullable=False,
            server_default=sa.text("'pending_auth'"),
        ),
        sa.Column(
            "last_health_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column("installed_by", sa.String(128), nullable=True),
        sa.Column(
            "installed_at",
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
            "status IN ('pending_auth','connected','degraded','revoked','error')",
            name="ck_install_status",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "integration_id",
            name="uq_install_tenant_integration",
        ),
    )
    op.create_index(
        "ix_install_tenant", "integration_installations", ["tenant_id"]
    )
    op.create_index(
        "ix_install_status", "integration_installations", ["status"]
    )

    # ── integration_events_log ──────────────────────────────────
    #
    # Append-only audit trail of every inbound webhook and outbound
    # action across all integrations. Time-partition friendly: leading
    # index column is tenant_id, secondary is created_at desc.
    op.create_table(
        "integration_events_log",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("integration_id", sa.String(128), nullable=False),
        sa.Column("installation_id", sa.String(64), nullable=False),
        sa.Column("direction", sa.String(16), nullable=False),
        sa.Column("event_type", sa.String(128), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=True),
        sa.Column("trace_id", sa.String(64), nullable=True),
        sa.Column("payload_hash", sa.String(64), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("latency_ms", sa.Integer, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "direction IN ('inbound','outbound')",
            name="ck_iel_direction",
        ),
    )
    op.create_index(
        "ix_iel_tenant_time",
        "integration_events_log",
        ["tenant_id", sa.text("created_at DESC")],
    )
    op.create_index(
        "ix_iel_install",
        "integration_events_log",
        ["installation_id"],
    )
    # Idempotency keys are unique only within (tenant_id, integration_id)
    # to avoid cross-tenant collision while still allowing fast lookups.
    op.create_index(
        "ix_iel_idem",
        "integration_events_log",
        ["tenant_id", "integration_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )

    # ── tenant_keks ──────────────────────────────────────────────
    #
    # Per-tenant root-key registry for envelope encryption. The actual
    # cryptographic material lives in the configured KMS / HSM; this
    # table stores the *reference* (ARN / key URI / file path) and
    # rotation metadata only.
    op.create_table(
        "tenant_keks",
        sa.Column("tenant_id", sa.String(64), primary_key=True),
        sa.Column("kek_id", sa.String(128), nullable=False),
        sa.Column("kek_provider", sa.String(32), nullable=False),
        sa.Column("kek_uri", sa.String(512), nullable=False),
        sa.Column(
            "rotated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "rotation_due_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "rotation_period_days",
            sa.Integer,
            nullable=False,
            server_default=sa.text("90"),
        ),
        sa.CheckConstraint(
            "kek_provider IN ('aws_kms','azure_kv','gcp_kms','local')",
            name="ck_kek_provider",
        ),
    )
    op.create_index("ix_tenant_keks_provider", "tenant_keks", ["kek_provider"])

    # ── Row-Level Security ───────────────────────────────────────
    #
    # Apply the same RLS regime as migration 010. Every tenant-scoped
    # table introduced here gets the tenant_isolation policy keyed
    # on the nexus.current_tenant_id session variable, plus grants to
    # the nexus_app role.
    for table in _TENANT_TABLES_THIS_MIGRATION:
        op.execute(
            f"""
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'nexus_app') THEN
                    CREATE ROLE nexus_app NOLOGIN;
                END IF;
            END $$;
            """
        )
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;")
        # asyncpg refuses multi-statement prepared queries; issue these
        # as two separate execute() calls.
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table};")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
                USING (tenant_id = current_setting('nexus.current_tenant_id', true))
                WITH CHECK (tenant_id = current_setting('nexus.current_tenant_id', true));
            """
        )
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO nexus_app;")

    # Sequences need usage so inserts via nexus_app succeed.
    op.execute(
        "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO nexus_app;"
    )

    # Trigger that auto-bumps updated_at + version on UPDATE — applied
    # only to tables that carry both columns.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION nexus_tff_touch() RETURNS trigger AS $$
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
        CREATE TRIGGER tff_touch_before_update
            BEFORE UPDATE ON tenant_feature_flags
            FOR EACH ROW EXECUTE FUNCTION nexus_tff_touch();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS tff_touch_before_update ON tenant_feature_flags;")
    op.execute("DROP FUNCTION IF EXISTS nexus_tff_touch();")

    for table in reversed(_TENANT_TABLES_THIS_MIGRATION):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table};")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY;")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;")

    op.drop_index("ix_tenant_keks_provider", table_name="tenant_keks")
    op.drop_table("tenant_keks")

    op.drop_index("ix_iel_idem", table_name="integration_events_log")
    op.drop_index("ix_iel_install", table_name="integration_events_log")
    op.drop_index("ix_iel_tenant_time", table_name="integration_events_log")
    op.drop_table("integration_events_log")

    op.drop_index("ix_install_status", table_name="integration_installations")
    op.drop_index("ix_install_tenant", table_name="integration_installations")
    op.drop_table("integration_installations")

    op.drop_index("ix_products_aliases_gin", table_name="products")
    op.drop_index("ix_products_tenant_status", table_name="products")
    op.drop_index("ix_products_tenant", table_name="products")
    op.drop_table("products")

    op.drop_index("ix_circuit_open", table_name="feature_circuit_state")
    op.drop_table("feature_circuit_state")

    op.drop_index("ix_tff_feature", table_name="tenant_feature_flags")
    op.drop_index("ix_tff_tenant", table_name="tenant_feature_flags")
    op.drop_table("tenant_feature_flags")
