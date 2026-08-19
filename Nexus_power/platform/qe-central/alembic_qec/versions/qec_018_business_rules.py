"""M1.7 / T-GW-04 — durable business-rule learning.

WHAT WAS BEING THROWN AWAY.  The explorer already runs a real experiment when an
application disables its own forward control: it answers one declined question,
re-reads the page, and lets the app render its own verdict.  When the control
enables, the crawl has PROVED a rule that exists nowhere in the markup — a
``.min(1)`` in a validation schema, a hand-written ``canAdvance()`` — and it
writes the sentence into ``coverage.advance_blocked[i].business_rule``.

That sentence was read by exactly one thing: ``services/fleet_funnel``, which
COUNTS how many blocked advances a crawl met.  Nothing persisted the rule,
nothing indexed it, and no dispatch ever handed one back.  So every crawl of the
same application re-ran the same experiment against the same checkbox to
re-derive the same fact.  Learning happened; it never accumulated.

THE TABLE.  One row per (tenant, app, rule_key), where ``rule_key`` is derived
by the explorer from the URL TEMPLATE + the blocked control's label + the field
that unblocked it — so ``/application/8814/health`` and ``/application/9137/health``
are one rule, not one per applicant.

VERSIONED, not overwritten-in-place: ``version`` increments on every re-proof and
``times_proven`` counts them, so a rule that has been confirmed on forty crawls
is distinguishable from one proved once eleven months ago.  ``last_proven_at``
and ``last_crawl_id`` make each row traceable back to the run that proved it,
which is what keeps this a record of evidence rather than a cache of opinions.

RLS FORCED.  A rule names business questions on a client's application — that is
client content, and it follows the same tenant-isolation pattern as qec_005 /
qec_012.  There is no cross-tenant pooling here and none is contemplated: what an
insurer's underwriting wizard requires is not a fact about anyone else.

PURELY ADDITIVE: one new table.  Nothing existing changes shape, and an explorer
that never sends ``discovered_rules`` leaves it empty forever.

Revision ID: qec_018
Revises: qec_017
Create Date: 2026-08-18
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers
revision: str = "qec_018"
down_revision: Union[str, None] = "qec_017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE_NAME = "qe_business_rules"

#: The lookup a dispatch performs: "every rule this tenant has proved about this
#: app".  Unique on the rule identity so a re-proof is an UPDATE, never a second
#: row — a store that accumulated duplicates would report a rising rule count for
#: an application that had learned nothing new.
UNIQUE_INDEX = "uq_qe_business_rules_identity"
#: Ordering index for the dispatch read (newest-proven first, bounded).
SCAN_INDEX = "ix_qe_business_rules_tenant_app_proven"


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
        TABLE_NAME,
        sa.Column("rule_row_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("app_id", sa.String(64), nullable=False),
        #: The explorer-derived identity (``rule:<24 hex>``) — see app/rules.py.
        sa.Column("rule_key", sa.String(64), nullable=False),
        #: What KIND of rule.  Named rather than implied so the next kind (a
        #: cross-field dependency, a value range the app rejected) needs no
        #: migration.
        sa.Column("kind", sa.String(32), nullable=False, server_default="advance_gate"),
        #: WHERE it holds — an id-collapsed url_template, never a raw URL.
        sa.Column("url_template", sa.String(500), nullable=False, server_default=""),
        #: The control the app had disabled, and the question that enabled it.
        sa.Column("blocked_label", sa.String(120), nullable=False, server_default=""),
        sa.Column("field_label", sa.String(120), nullable=False, server_default=""),
        #: The sentence the application itself justified, kept VERBATIM.  It is
        #: the record of what was observed; regenerating it on read would make it
        #: a description rather than evidence.
        sa.Column("proof", sa.String(500), nullable=False, server_default=""),
        #: The explorer's wire-shape version.  A reader that does not recognise a
        #: version must ignore the rule rather than guess at it.
        sa.Column("schema_version", sa.Integer, nullable=False, server_default="1"),
        #: Bumped on every re-proof.
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("times_proven", sa.Integer, nullable=False, server_default="1"),
        #: Traceability: which crawl last proved it, and when.
        sa.Column("last_crawl_id", sa.String(50), nullable=False, server_default=""),
        _ts("first_proven_at"),
        _ts("last_proven_at"),
    )
    op.create_index(UNIQUE_INDEX, TABLE_NAME,
                    ["tenant_id", "app_id", "rule_key"], unique=True)
    # Column order is load-bearing, as in qec_017: the equality predicate
    # (tenant, app) first so Postgres can seek the bucket, then the ordering
    # column so the bounded read walks it already sorted.
    op.create_index(SCAN_INDEX, TABLE_NAME,
                    ["tenant_id", "app_id", "last_proven_at"])
    _apply_rls(TABLE_NAME)


def downgrade() -> None:
    # Drops EXACTLY what upgrade created, leaving the schema bit-identical to
    # qec_017: the policy, then the indexes with the table.
    _drop_rls(TABLE_NAME)
    op.drop_index(SCAN_INDEX, table_name=TABLE_NAME)
    op.drop_index(UNIQUE_INDEX, table_name=TABLE_NAME)
    op.drop_table(TABLE_NAME)
