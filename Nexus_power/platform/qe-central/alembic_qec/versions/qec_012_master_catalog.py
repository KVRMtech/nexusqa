"""P2 — app-scoped Master Catalog + versions.

The per-node catalog (qec_010) answers "what is on this page". The Master
Catalog answers "what questions does this APPLICATION have", deduped by a stable
``question_id`` (derived from the value-free control signature) across every
journey and node — so the 400 questions live ONCE, not per journey. A snapshot
per crawl (``catalog_versions``) lets a re-crawl (new ``artifact_id``) diff
against the last catalog for regression (P6).

Both tables hold question TEXT — business content — so they are RLS FORCED,
matching the qec_005 tenant-isolation pattern.

PURELY ADDITIVE: two new tables; nothing existing changes shape.

Revision ID: qec_012
Revises: qec_011
Create Date: 2026-08-08
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers
revision: str = "qec_012"
down_revision: Union[str, None] = "qec_011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = ("catalog_questions", "catalog_versions")


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
        "catalog_questions",
        sa.Column("cq_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("app_id", sa.String(64), nullable=False),
        #: stable, value-free (derived from control signature) — the join key.
        sa.Column("question_id", sa.String(64), nullable=False),
        sa.Column("name", sa.String(300), nullable=False, server_default=""),
        sa.Column("answer_type", sa.String(40), nullable=False, server_default="text"),
        sa.Column("required", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("options", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("validation", JSONB, nullable=True),
        sa.Column("business_rule", sa.String(500), nullable=False, server_default=""),
        sa.Column("expected_next_page", sa.String(200), nullable=False, server_default=""),
        sa.Column("semantic_type", sa.String(80), nullable=False, server_default=""),
        sa.Column("provenance", sa.String(24), nullable=False, server_default="observed"),
        sa.Column("pages", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("first_seen_artifact", sa.String(64), nullable=False, server_default=""),
        sa.Column("last_seen_artifact", sa.String(64), nullable=False, server_default=""),
        _ts("first_seen_at"),
        _ts("last_seen_at"),
    )
    op.create_index("uq_catalog_questions_identity", "catalog_questions",
                    ["tenant_id", "app_id", "question_id"], unique=True)

    op.create_table(
        "catalog_versions",
        sa.Column("version_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("app_id", sa.String(64), nullable=False),
        sa.Column("artifact_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("snapshot_hash", sa.String(64), nullable=False),
        sa.Column("question_count", sa.Integer, nullable=False, server_default="0"),
        #: the full Master Catalog snapshot at this version — what P6 diffs.
        sa.Column("snapshot", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        _ts("created_at"),
    )
    op.create_index("uq_catalog_versions_identity", "catalog_versions",
                    ["tenant_id", "app_id", "artifact_id"], unique=True)

    for table in _TABLES:
        _apply_rls(table)


def downgrade() -> None:
    for table in reversed(_TABLES):
        _drop_rls(table)
    op.drop_table("catalog_versions")
    op.drop_table("catalog_questions")
