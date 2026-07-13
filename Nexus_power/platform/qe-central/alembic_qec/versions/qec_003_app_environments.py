"""QE-Central multi-env — the ``app_environments`` Environment Profile table.

Adds ONE qecentral table: a named Environment Profile per client app
(dev/test/uat/prod). An app is crawled ONCE against a reference env → one
env-invariant flow + one approved baseline; each environment is a run-time REBIND
of that same flow, carrying its own ``base_url`` + routing cookies/headers +
``data_overrides`` + per-env ``fences`` + an ``env_assertion`` that fails a run
CLOSED-to-RED if it landed on the wrong env. Secret material (login creds,
basic-auth, secret cookies/headers) is folded into a KMS ``creds_blob`` — never
plaintext at rest.

Row-Level Security is applied with the SAME ENABLE + FORCE + ``tenant_isolation``
pattern as ``qec_001``/``qec_002`` (VKPower 010/034), keyed on the
``nexus.current_tenant_id`` GUC, granted to the ``qec`` app role when it exists.

PURELY ADDITIVE: one new table with RLS. An app with no environment rows behaves
exactly as before (its single ``base_url`` IS the reference/only env).

Revision ID: qec_003
Revises: qec_002
Create Date: 2026-07-12
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers
revision: str = "qec_003"
down_revision: Union[str, None] = "qec_002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "app_environments"


def _ts(name: str) -> sa.Column:
    return sa.Column(
        name, sa.DateTime(timezone=True), nullable=False,
        server_default=sa.text("now()"),
    )


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("environment_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("app_id", sa.String(64), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("base_url", sa.String(2000), nullable=False, server_default=""),
        sa.Column("canonical_host", sa.String(500), nullable=False, server_default=""),
        sa.Column("cookies", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("headers", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("data_overrides", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("fences", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("env_attestation", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("env_assertion", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        # KMS envelope (AAD=environment_id); NULL = no secrets.
        sa.Column("creds_blob", sa.LargeBinary, nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        _ts("created_at"),
        _ts("updated_at"),
    )
    op.create_index("ix_app_environments_tenant_app", _TABLE, ["tenant_id", "app_id"])
    op.create_unique_constraint(
        "uq_app_environments_name", _TABLE, ["tenant_id", "app_id", "name"])

    # ─── Row-Level Security (qec_001 / qec_002 / 010 / 034 pattern) ──────
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
