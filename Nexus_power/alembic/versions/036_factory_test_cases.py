"""Factory test cases — stored output of the Test Factory (demonstrated E2E).

The Test Factory turns the frozen Pages & Forms evidence (``page_visits`` /
``page_actions``) into production test cases (``nexus_sdk.models``
``ProductionTestCase``).  This table is the queryable, paginatable store for
those generated cases so the UI never has to load them all at once and the
delivery layer (Excel / qTest / Zephyr / TestRail / Xray) can stream them.

* ``status`` — ``active`` (the bounded, delivered/executed suite) vs
  ``reserve`` (stored-for-future combinations; populated in Phase 2).  Phase 1
  writes only ``active`` demonstrated cases.
* ``test_case`` (JSONB) — the full serialized ``ProductionTestCase`` (steps,
  preconditions, tags) so nothing is lost; the top-level columns exist for
  filtering / pagination without parsing the blob.
* ``test_case_id`` — the generator's deterministic ``uuid5`` test_id, so
  re-generating an artifact UPSERTs in place (idempotent).

Additive: no frozen table is modified.

Revision ID: 036_factory_test_cases
Revises: 035_page_actions
Create Date: 2026-06-03
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "036_factory_test_cases"
down_revision: Union[str, None] = "035_page_actions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "factory_test_cases"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        # ── Primary key (deterministic uuid5 from the generator) ───────────
        sa.Column("test_case_id", sa.String(64), primary_key=True),

        # ── Ownership / tenancy ────────────────────────────────────────────
        sa.Column(
            "artifact_id",
            sa.String(64),
            sa.ForeignKey("canonical_artifacts.artifact_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("session_id", sa.String(64), nullable=False, server_default=""),

        # ── Filterable / pageable attributes ───────────────────────────────
        sa.Column("name", sa.String(500), nullable=False, server_default=""),
        sa.Column("description", sa.Text, nullable=False, server_default=""),
        sa.Column("priority", sa.String(40), nullable=False, server_default="P2_medium"),
        sa.Column("test_type", sa.String(40), nullable=False, server_default="functional"),
        sa.Column("confidence", sa.String(40), nullable=False, server_default="demonstrated"),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("step_count", sa.Integer, nullable=False, server_default="0"),

        # ── Payload (full ProductionTestCase + provenance) ─────────────────
        sa.Column("tags", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("test_case", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column(
            "source_evidence", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb"),
        ),

        # ── Versioning + timestamps ────────────────────────────────────────
        sa.Column("generator_version", sa.String(50), nullable=False, server_default="v1"),
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
    )

    op.create_index(
        "ix_factory_test_cases_artifact_status",
        _TABLE,
        ["artifact_id", "status"],
    )
    op.create_index(
        "ix_factory_test_cases_tenant_status",
        _TABLE,
        ["tenant_id", "status"],
    )
    op.create_index(
        "ix_factory_test_cases_tenant_priority",
        _TABLE,
        ["tenant_id", "priority"],
    )

    # ── Row-Level Security (mirrors migrations 034 / 035) ──────────────────
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
    op.drop_index("ix_factory_test_cases_tenant_priority", table_name=_TABLE)
    op.drop_index("ix_factory_test_cases_tenant_status", table_name=_TABLE)
    op.drop_index("ix_factory_test_cases_artifact_status", table_name=_TABLE)
    op.drop_table(_TABLE)
