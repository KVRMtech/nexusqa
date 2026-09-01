"""Combination reserve — the lossless "store everything" pool (Phase 2).

For each artifact the Test Factory stores ONE reserve row holding the full
captured **option domains** (per field: selected value + available options)
plus the **generation spec** (strategy + risk weights + base test).  This lets
us reconstruct ANY combination on demand without materialising a huge table:
the bounded, risk-ranked subset is promoted into ``factory_test_cases`` as the
active suite, while the reserve preserves the complete combinatorial space for
future promotion (e.g. when a production defect appears in an untested combo).

Additive: no frozen table is modified.

Revision ID: 037_combination_reserve
Revises: 036_factory_test_cases
Create Date: 2026-06-04
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "037_combination_reserve"
down_revision: Union[str, None] = "036_factory_test_cases"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "e2e_combination_reserve"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("reserve_id", sa.String(64), primary_key=True),
        sa.Column(
            "artifact_id",
            sa.String(64),
            sa.ForeignKey("canonical_artifacts.artifact_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("session_id", sa.String(64), nullable=False, server_default=""),

        # The complete captured option space + how to expand it.
        sa.Column(
            "option_domains", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "generation_spec", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb"),
        ),

        # Bookkeeping: full-space size vs how many were promoted to active.
        sa.Column("combination_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("selected_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("status", sa.String(20), nullable=False, server_default="reserve"),

        sa.Column("generator_version", sa.String(50), nullable=False, server_default="v1"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("artifact_id", "generator_version",
                            name="uq_combination_reserve_artifact_version"),
    )

    op.create_index(
        "ix_combination_reserve_artifact", _TABLE, ["artifact_id"],
    )
    op.create_index(
        "ix_combination_reserve_tenant", _TABLE, ["tenant_id"],
    )

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

                IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'nexus_app') THEN
                    GRANT SELECT, INSERT, UPDATE, DELETE ON {_TABLE} TO nexus_app;
                END IF;
            END IF;
        END$$;
    """)


def downgrade() -> None:
    op.drop_index("ix_combination_reserve_tenant", table_name=_TABLE)
    op.drop_index("ix_combination_reserve_artifact", table_name=_TABLE)
    op.drop_table(_TABLE)
