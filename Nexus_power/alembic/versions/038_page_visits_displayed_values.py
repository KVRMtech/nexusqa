"""ANSWERS P1.B — add ``page_visits.displayed_values`` (rendered value nodes).

Carries ``[{label, selector, text}]`` captured for each page (a premium/total/
decline rendered as text that no interactive-control walker sees) so the value
oracle can ground an expected outcome to a real node WITHOUT a client-authored
source_hint.  ``nullable=False`` + server default ``[]`` so every existing row is
valid; the ORM (:class:`nexus_sdk.db.models.PageVisitRow`) mirrors it as ``JSON``
(same ORM-JSON / DB-JSONB pairing as ``form_snapshot``).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "038_page_visits_displayed_values"
down_revision: Union[str, None] = "037_combination_reserve"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "page_visits",
        sa.Column(
            "displayed_values", JSONB, nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("page_visits", "displayed_values")
