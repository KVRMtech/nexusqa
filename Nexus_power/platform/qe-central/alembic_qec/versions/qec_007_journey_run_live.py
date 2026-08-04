"""QE-Central Runnable Journeys — the watchable live run.

A journey run is dispatched HEADED through the factory's ``run-live`` path so
an operator can watch it execute (the same noVNC stream the Playwright tab
offers). ``live_url`` is the viewer address for that stream; it is transient
(the runner tears the session down shortly after the run ends) and is kept on
the ledger row so the portal can show the window while the run is in flight.

PURELY ADDITIVE: one nullable column with an empty default; rows written
before this migration read as "no live view" and behave exactly as before.

Revision ID: qec_007
Revises: qec_006
Create Date: 2026-08-04
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers
revision: str = "qec_007"
down_revision: Union[str, None] = "qec_006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "journey_runs",
        sa.Column("live_url", sa.String(500), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("journey_runs", "live_url")
