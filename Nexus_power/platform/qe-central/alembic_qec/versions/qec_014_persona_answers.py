"""P3 — declared answers on a persona.

A persona registered for journey generation carries its own declared answers
(``{question_id_or_name: option}``) IN qe-central — so the 20-persona generation
runs off stored personas without any cross-service value egress. The answers are
decision-level option labels (Yes/No/enumerated), the shape the projector needs;
values stay in the tenant.

``personas`` is RLS-forced (qec_013); an added column inherits the policy.

PURELY ADDITIVE — one nullable JSONB column on an existing table.

Revision ID: qec_014
Revises: qec_013
Create Date: 2026-08-09
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers
revision: str = "qec_014"
down_revision: Union[str, None] = "qec_013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("personas",
                  sa.Column("answers", JSONB, nullable=True))


def downgrade() -> None:
    op.drop_column("personas", "answers")
