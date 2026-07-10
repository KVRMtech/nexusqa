"""QE-Central Phase-7 — the ``tenant_provisioning`` control record.

Adds ONE qecentral table: QE-Central's OWN mutable, RLS-scoped, per-tenant
lifecycle + quota control record (role ``qec`` owns it, full CRUD).  It exists in
the qecentral bounded context (R-7) rather than in the VKPower ``nexus.tenants``
registry because QE-Central may only SELECT/INSERT ``nexus.tenants`` (the
``qec_substrate`` grant has NO UPDATE) and VKPower alone owns that registry — a
tenant's suspend/resume/offboard state is QE-Central's to mutate at runtime, so
it lives here.

Row-Level Security is applied with the SAME ENABLE + FORCE + ``tenant_isolation``
pattern as ``qec_001`` (VKPower migrations 010/034), keyed on the
``nexus.current_tenant_id`` GUC, and the table is granted to the ``qec`` app role
when it exists.

BACKWARD-COMPAT: this migration is PURELY ADDITIVE — one new table with RLS.  A
tenant with no row here is treated as ACTIVE / UNLIMITED by the lifecycle gate
and plan resolver, so every existing tenant behaves exactly as before.

Revision ID: qec_002
Revises: qec_001
Create Date: 2026-07-10
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers
revision: str = "qec_002"
down_revision: Union[str, None] = "qec_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "tenant_provisioning"


def _ts(name: str, *, nullable: bool = False) -> sa.Column:
    """UTC timestamptz column (defaults to now() when NOT NULL)."""
    if nullable:
        return sa.Column(name, sa.DateTime(timezone=True), nullable=True)
    return sa.Column(
        name, sa.DateTime(timezone=True), nullable=False,
        server_default=sa.text("now()"),
    )


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("tenant_id", sa.String(64), primary_key=True),
        sa.Column("plan", sa.String(50), nullable=False, server_default="starter"),
        # active | suspended | offboarding | deleted (app.fleet.lifecycle.STATUS_*)
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("admin_email", sa.String(320), nullable=False, server_default=""),
        sa.Column("display_name", sa.String(200), nullable=False, server_default=""),
        # Per-tenant quota deltas over the plan envelope (plans.effective_quota).
        sa.Column("quota_overrides", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        _ts("provisioned_at"),
        _ts("suspended_at", nullable=True),
        _ts("offboarding_started_at", nullable=True),
        _ts("retention_until", nullable=True),
        _ts("tokens_revoked_at", nullable=True),
        # Explicit purge request (offboard --purge); evidence deletion is a
        # separate gated seam — this flag NEVER triggers a delete on its own.
        sa.Column("purge_requested", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("actor", sa.String(200), nullable=False, server_default=""),
        sa.Column("reason", sa.Text, nullable=False, server_default=""),
        _ts("created_at"),
        _ts("updated_at"),
    )
    op.create_index("ix_tenant_provisioning_status", _TABLE, ["status"])

    # ─── Row-Level Security (qec_001 / 010 / 034 pattern) ────────────────
    # ENABLE + FORCE (owner included) + tenant_isolation policy keyed on the
    # nexus.current_tenant_id GUC; grant to the qec app role when it exists.
    op.execute(f"""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = '{_TABLE}'
            ) THEN
                ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY;
                ALTER TABLE {_TABLE} FORCE ROW LEVEL SECURITY;

                DROP POLICY IF EXISTS tenant_isolation ON {_TABLE};
                CREATE POLICY tenant_isolation ON {_TABLE}
                    USING (tenant_id = current_setting('nexus.current_tenant_id', true))
                    WITH CHECK (tenant_id = current_setting('nexus.current_tenant_id', true));

                IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'qec') THEN
                    GRANT SELECT, INSERT, UPDATE, DELETE ON {_TABLE} TO qec;
                END IF;
            END IF;
        END $$;
    """)


def downgrade() -> None:
    op.execute(f"""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = '{_TABLE}'
            ) THEN
                DROP POLICY IF EXISTS tenant_isolation ON {_TABLE};
                ALTER TABLE {_TABLE} NO FORCE ROW LEVEL SECURITY;
                ALTER TABLE {_TABLE} DISABLE ROW LEVEL SECURITY;
            END IF;
        END $$;
    """)
    op.drop_table(_TABLE)
