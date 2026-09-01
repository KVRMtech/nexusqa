"""QE-Central advance memory — proven crawl-time advance decisions.

Adds the learning layer under the E2E advance oracle (Release B Phase 4 of
``QECentral/docs/E2E_ADVANCE_HARDENING_PLAN.md``):

``advance_memory`` — TENANT-PRIVATE, RLS-forced.  One row per proven decision
point: the value-free signature (candidate labels + kinds + title shape — NO
URLs, NO hosts, NO user values) → the normalized label of the control that a
crawl PROVED advances (a genuine advance was observed: real effect + new
unseen state).  Recall turns a repeat LLM question into a free lookup.

``advance_label_priors`` — CROSS-TENANT and deliberately RLS-free: it carries
ONLY normalized advance-label patterns (product UI text such as "see my
quote") plus pseudonymous contributor hashes for a distinct-tenant count.
Contribution is OPT-IN per tenant (``tenant_provisioning.share_advance_priors``,
default FALSE) — the flag this migration also adds.

PURELY ADDITIVE: nothing existing changes shape; a deployment that never
recalls or harvests behaves exactly as before.

Revision ID: qec_004
Revises: qec_003
Create Date: 2026-08-03
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers
revision: str = "qec_004"
down_revision: Union[str, None] = "qec_003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_MEMORY = "advance_memory"
_PRIORS = "advance_label_priors"


def _ts(name: str) -> sa.Column:
    return sa.Column(
        name, sa.DateTime(timezone=True), nullable=False,
        server_default=sa.text("now()"),
    )


def upgrade() -> None:
    op.create_table(
        _MEMORY,
        sa.Column("tenant_id", sa.String(64), primary_key=True),
        sa.Column("signature", sa.String(64), primary_key=True),
        sa.Column("chosen_label_norm", sa.String(200), nullable=False),
        # Provenance only — recall is keyed (tenant_id, signature): the same
        # wizard framework shape answers across a tenant's apps.
        sa.Column("app_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("proof_count", sa.Integer, nullable=False, server_default="1"),
        _ts("last_proven_at"),
        _ts("created_at"),
    )

    # ─── Row-Level Security (qec_001..qec_003 pattern) ───────────────────
    op.execute(f"""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = '{_MEMORY}'
            ) THEN
                ALTER TABLE {_MEMORY} ENABLE ROW LEVEL SECURITY;
                ALTER TABLE {_MEMORY} FORCE ROW LEVEL SECURITY;

                DROP POLICY IF EXISTS tenant_isolation ON {_MEMORY};
                CREATE POLICY tenant_isolation ON {_MEMORY}
                    USING (tenant_id = current_setting('nexus.current_tenant_id', true))
                    WITH CHECK (tenant_id = current_setting('nexus.current_tenant_id', true));

                IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'qec') THEN
                    GRANT SELECT, INSERT, UPDATE, DELETE ON {_MEMORY} TO qec;
                END IF;
            END IF;
        END $$;
    """)

    # Cross-tenant, VALUE-FREE label priors. No tenant_id column BY DESIGN —
    # nothing tenant-identifying may live here; contributors are counted via
    # pseudonymous hashes only.  Deliberately NOT under RLS (there is no
    # tenant key to isolate on and the content is shared by construction).
    op.create_table(
        _PRIORS,
        sa.Column("label_norm", sa.String(200), primary_key=True),
        sa.Column("proof_count", sa.Integer, nullable=False, server_default="1"),
        sa.Column("distinct_tenants", sa.Integer, nullable=False, server_default="1"),
        sa.Column("contributor_hashes", JSONB, nullable=False,
                  server_default=sa.text("'[]'::jsonb")),
        _ts("last_proven_at"),
        _ts("created_at"),
    )
    op.execute(f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'qec') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE ON {_PRIORS} TO qec;
            END IF;
        END $$;
    """)

    # Opt-in consent flag: contributing advance-label priors is OFF until a
    # tenant explicitly turns it on.  Recall of the pooled (value-free)
    # priors is not gated — contribution is.
    op.add_column(
        "tenant_provisioning",
        sa.Column("share_advance_priors", sa.Boolean, nullable=False,
                  server_default=sa.text("false")),
    )


def downgrade() -> None:
    op.drop_column("tenant_provisioning", "share_advance_priors")
    op.drop_table(_PRIORS)
    op.execute(f"""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = '{_MEMORY}'
            ) THEN
                DROP POLICY IF EXISTS tenant_isolation ON {_MEMORY};
                ALTER TABLE {_MEMORY} NO FORCE ROW LEVEL SECURITY;
                ALTER TABLE {_MEMORY} DISABLE ROW LEVEL SECURITY;
            END IF;
        END $$;
    """)
    op.drop_table(_MEMORY)
