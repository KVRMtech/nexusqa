"""U2 — per-tenant vision autonomy flag.

The vision Perceiver / coordinate rung is DOUBLE-GATED, fail-closed: the env
switch ``QEC_CRAWL_VISION_ENABLED`` AND this per-tenant flag must BOTH be on
(``branch_planner.autonomy_flags`` ANDs them). A tenant that has not opted in
never has a vision call made on its behalf, even if the env switch is on.

``tenant_provisioning`` was created with RLS in qec_002; an added column inherits
that policy.

PURELY ADDITIVE — one boolean column defaulting False on an existing table.

Revision ID: qec_015
Revises: qec_014
Create Date: 2026-08-09
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers
revision: str = "qec_015"
down_revision: Union[str, None] = "qec_014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tenant_provisioning",
        sa.Column("vision_enabled", sa.Boolean, nullable=False,
                  server_default=sa.text("false")),
    )


def downgrade() -> None:
    op.drop_column("tenant_provisioning", "vision_enabled")
