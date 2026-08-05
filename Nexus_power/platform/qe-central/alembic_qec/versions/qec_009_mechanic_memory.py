"""R4 — Mechanic memory (compounding interaction knowledge).

When the explorer verifies that a specific ladder rung operates a control,
the proven mechanic is persisted here. On the next crawl, the explorer tries
the proven mechanic FIRST — bypassing the full ladder walk.

Two tables (same pattern as advance_memory / advance_label_priors):

  * ``control_mechanics`` — TENANT-PRIVATE (RLS-forced): one row per proven
    control-interaction mechanic, keyed ``(tenant_id, control_sig)``.
  * ``mechanic_priors`` — CROSS-TENANT, VALUE-FREE: pooled knowledge of which
    mechanic works for which control shape. Contribution is consent-gated
    (share_advance_priors), OFF by default.

PURELY ADDITIVE — no existing tables are touched.

Revision ID: qec_009
Revises: qec_008
Create Date: 2026-08-05
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers
revision: str = "qec_009"
down_revision: Union[str, None] = "qec_008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "control_mechanics",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("control_sig", sa.String(64), nullable=False),
        sa.Column("mechanic", sa.String(80), nullable=False),
        sa.Column("app_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("proof_count", sa.Integer, nullable=False, server_default="1"),
        sa.Column("last_proven_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("tenant_id", "control_sig"),
    )
    op.execute(
        "ALTER TABLE control_mechanics ENABLE ROW LEVEL SECURITY"
    )
    op.execute(
        "ALTER TABLE control_mechanics FORCE ROW LEVEL SECURITY"
    )
    op.execute(
        "CREATE POLICY tenant_isolation ON control_mechanics "
        "USING (tenant_id = current_setting('nexus.current_tenant_id', true))"
    )

    op.create_table(
        "mechanic_priors",
        sa.Column("control_sig", sa.String(64), nullable=False),
        sa.Column("mechanic", sa.String(80), nullable=False),
        sa.Column("proof_count", sa.Integer, nullable=False, server_default="1"),
        sa.Column("distinct_tenants", sa.Integer, nullable=False,
                  server_default="1"),
        sa.Column("contributor_hashes", JSONB, nullable=False,
                  server_default="[]"),
        sa.Column("last_proven_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("control_sig", "mechanic"),
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON control_mechanics")
    op.drop_table("mechanic_priors")
    op.drop_table("control_mechanics")
