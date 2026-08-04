"""QE-Central Runnable Journeys — journey⇄case links and the journey run ledger.

Release D of ``QECentral/docs/JOURNEY_EXECUTION_PLAN.md``:

``journey_cases`` — which factory test cases exercise which journey, per crawl
artifact (re-crawls mint new artifacts; links are re-derived per completion).
``kind='journey_e2e'`` marks THE one end-to-end case adopted as the journey's
runnable form; ``display_name`` carries the business-named overlay (the frozen
factory's own case name is never edited).

``journey_runs`` — one row per journey-dispatched execution: the dispatch
run_id (transient runner job), the ingested run_id (the durable evidence /
report key, resolved via ci_run_id), the honest status, and the fold-back
verdict summary. Crawl-proof and run-proof stay DISTINCT facts.

Both tenant+app scoped, RLS forced (qec_001..005 pattern). PURELY ADDITIVE.

Revision ID: qec_006
Revises: qec_005
Create Date: 2026-08-04
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers
revision: str = "qec_006"
down_revision: Union[str, None] = "qec_005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = ("journey_cases", "journey_runs")


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
        "journey_cases",
        sa.Column("link_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("app_id", sa.String(64), nullable=False),
        sa.Column("journey_id", sa.String(64), nullable=False),
        sa.Column("artifact_id", sa.String(64), nullable=False),
        sa.Column("test_case_id", sa.String(64), nullable=False),
        sa.Column("case_name", sa.String(300), nullable=False, server_default=""),
        sa.Column("kind", sa.String(16), nullable=False, server_default="linked"),
        sa.Column("display_name", sa.String(300), nullable=False, server_default=""),
        sa.Column("coverage_score", sa.Integer, nullable=False, server_default="0"),
        _ts("created_at"),
    )
    op.create_index("uq_journey_cases_identity", "journey_cases",
                    ["tenant_id", "journey_id", "artifact_id", "test_case_id"],
                    unique=True)
    op.create_index("ix_journey_cases_journey", "journey_cases",
                    ["tenant_id", "app_id", "journey_id"])

    op.create_table(
        "journey_runs",
        sa.Column("journey_run_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("app_id", sa.String(64), nullable=False),
        sa.Column("journey_id", sa.String(64), nullable=False),
        sa.Column("artifact_id", sa.String(64), nullable=False),
        sa.Column("test_case_id", sa.String(64), nullable=False),
        # The runner job id returned at dispatch (transient status endpoint).
        sa.Column("dispatch_run_id", sa.String(64), nullable=False, server_default=""),
        # The DURABLE ingested run id (evidence/report key), resolved by
        # matching run_header.ci_run_id == dispatch_run_id after the run ends.
        sa.Column("ingested_run_id", sa.String(64), nullable=False, server_default=""),
        # dispatched | running | passed | failed | timed_out | error | blocked
        sa.Column("status", sa.String(24), nullable=False, server_default="dispatched"),
        sa.Column("blocked_reason", sa.String(400), nullable=False, server_default=""),
        sa.Column("env_ref", sa.String(200), nullable=False, server_default=""),
        sa.Column("identity_ref", sa.String(200), nullable=False, server_default=""),
        sa.Column("verdict_summary", JSONB, nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        _ts("started_at"),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("uq_journey_runs_dispatch", "journey_runs",
                    ["tenant_id", "dispatch_run_id"], unique=False)
    op.create_index("ix_journey_runs_journey", "journey_runs",
                    ["tenant_id", "app_id", "journey_id"])

    for table in _TABLES:
        _apply_rls(table)


def downgrade() -> None:
    for table in reversed(_TABLES):
        _drop_rls(table)
    op.drop_table("journey_runs")
    op.drop_table("journey_cases")
