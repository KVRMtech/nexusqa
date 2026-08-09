"""P1 — trigger→child reveals on journey branches.

Records what walking an option ACTIVATED: the value-free control identities
(``kind:name``) that appeared after the answer that were not there before. A
"Yes" that reveals a detail block stores those identities; a "No" that reveals
nothing stores none. This turns the already-enumerated branch rows into
trigger→child rules (Q=Yes → these follow-ups) WITHOUT a new table — a branch
row is exactly "one enumerated option at a decision node", and what it reveals
is an attribute of that option.

``journey_branches`` was created with RLS FORCED in qec_005; an added column
inherits that policy, so no RLS re-apply is needed.

PURELY ADDITIVE — one nullable JSONB column on an existing table.

Revision ID: qec_011
Revises: qec_010
Create Date: 2026-08-08
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers
revision: str = "qec_011"
down_revision: Union[str, None] = "qec_010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("journey_branches",
                  sa.Column("reveals", JSONB, nullable=True))


def downgrade() -> None:
    op.drop_column("journey_branches", "reveals")
