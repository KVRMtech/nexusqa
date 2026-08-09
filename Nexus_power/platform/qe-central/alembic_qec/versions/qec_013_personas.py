"""P3 — persona journeys.

A persona (a declared answer profile — Healthy, Tobacco, Diabetes…) replays the
ONE Master Catalog + P1 rules to produce a distinct business journey, projected
by ``journey_projector`` and optionally proven by a verifying crawl. We store the
persona reference and its projected path — executed / activated / skipped question
ids (value-free) — with an honest provenance: ``inferred`` until a real traversal
confirms it, then ``live_confirmed``.

``personas.persona_id`` references the platform-api ``tp_personas`` row; the seam
between the two services carries ANSWER shapes, never credentials (Δ3). Persona
names can carry business context, so both tables are RLS FORCED.

PURELY ADDITIVE: two new tables.

Revision ID: qec_013
Revises: qec_012
Create Date: 2026-08-08
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers
revision: str = "qec_013"
down_revision: Union[str, None] = "qec_012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = ("personas", "persona_journeys")


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
        "personas",
        sa.Column("persona_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("app_id", sa.String(64), nullable=False),
        sa.Column("name", sa.String(200), nullable=False, server_default=""),
        sa.Column("source_ref", sa.String(200), nullable=False, server_default=""),
        _ts("created_at"),
    )
    op.create_index("uq_personas_identity", "personas",
                    ["tenant_id", "app_id", "persona_id"], unique=True)

    op.create_table(
        "persona_journeys",
        sa.Column("persona_journey_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("app_id", sa.String(64), nullable=False),
        sa.Column("persona_id", sa.String(64), nullable=False),
        sa.Column("journey_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("path_hash", sa.String(64), nullable=False, server_default=""),
        sa.Column("executed", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("activated", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("skipped", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        #: honest provenance: inferred (projected) until a real traversal confirms.
        sa.Column("provenance", sa.String(24), nullable=False, server_default="inferred"),
        sa.Column("verified_traversal_id", sa.String(64), nullable=False, server_default=""),
        _ts("created_at"),
    )
    op.create_index("uq_persona_journeys_identity", "persona_journeys",
                    ["tenant_id", "app_id", "persona_id", "journey_id"], unique=True)

    for table in _TABLES:
        _apply_rls(table)


def downgrade() -> None:
    for table in reversed(_TABLES):
        _drop_rls(table)
    op.drop_table("persona_journeys")
    op.drop_table("personas")
