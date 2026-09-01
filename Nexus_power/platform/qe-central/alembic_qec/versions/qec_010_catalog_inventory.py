"""E3 — Catalog inventory on journey nodes.

Surface the control inventory already captured in the manifest as first-class
JSONB columns on ``journey_nodes``.  The fold extracts form controls, options,
required flags, semantic types, and displayed outcome values from the page
state matching each node's fingerprint — no new capture, purely surfacing.

PURELY ADDITIVE — two nullable JSONB columns on an existing table.

Revision ID: qec_010
Revises: qec_009
Create Date: 2026-08-05
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers
revision: str = "qec_010"
down_revision: Union[str, None] = "qec_009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("journey_nodes",
                  sa.Column("controls_inventory", JSONB, nullable=True))
    op.add_column("journey_nodes",
                  sa.Column("displayed_outcomes", JSONB, nullable=True))


def downgrade() -> None:
    op.drop_column("journey_nodes", "displayed_outcomes")
    op.drop_column("journey_nodes", "controls_inventory")
