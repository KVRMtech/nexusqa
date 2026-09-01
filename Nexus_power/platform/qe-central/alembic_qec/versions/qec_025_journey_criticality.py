"""Tier 2 — a journey's criticality band becomes a durable fact.

WHAT ALREADY WORKED.  M2.4 wired ``criticality.evaluate`` to the journey graph
through ``journey_criticality.subject_from_journey_graph`` and ranked the result
deterministically, so ``GET /apps/{id}/journeys`` and its Top-N finally answered
"which of these twenty matters most".  Every part of that is computed at READ
time, from the tenant's currently-active signal pack.

WHY THAT IS NOT ENOUGH ON ITS OWN.  A band that exists only for the duration of
a request cannot be compared with anything.  Three questions an operator asks
are unanswerable against it:

  * "did this journey's criticality CHANGE with the last crawl?"  — there is no
    previous value to diff against;
  * "what did we say about this journey when we certified it?"  — the band that
    justified a decision is not in the record beside the decision;
  * "which journeys were re-banded when we published a new signal pack?" — the
    pack version that produced a band is not stored either, so nothing can even
    identify which bands are stale.

READ TIME STAYS AUTHORITATIVE.  These columns are the band AS OF THE LAST FOLD,
not a cache the API serves from.  A stored band served as current would silently
outlive the evidence and the pack that produced it — the precise failure mode
this repository keeps closing — so ``_rank_journeys`` continues to evaluate
live, and ``criticality_registry_version`` is stored beside the band so a reader
can see WHICH pack said it and whether that is still the active one.

FOUR COLUMNS, ALL ADDITIVE.  ``criticality_band`` defaults to the empty string,
which is the pre-migration truth of every existing row: no fold has banded this
journey yet.  Deliberately NOT defaulted to the registry's fail-up band — an
unbanded journey and a journey banded P1 because nothing matched are different
facts, and starting every row at P1 would erase the difference on day one.

``criticality_evidence`` holds the markers that fired, carried through from the
registry verbatim, because a band nobody can audit is a number nobody should
act on.

RLS IS UNCHANGED AND STILL FORCED.  ``journeys`` is tenant-isolated since
qec_009; adding columns does not touch the policy and no table is created.

Revision ID: qec_025
Revises: qec_024
Create Date: 2026-08-31
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers
revision: str = "qec_025"
down_revision: Union[str, None] = "qec_024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE_NAME = "journeys"

#: The pre-migration meaning of every existing row: no fold has banded this
#: journey. NOT the registry's fail-up band — see the module docstring.
DEFAULT_BAND = ""

COLUMNS = (
    #: P0..P3 as of the last fold. "" until one has run.
    ("criticality_band", sa.String(8), DEFAULT_BAND),
    #: Which signal pack produced the band above. Stored so a reader can tell a
    #: band the ACTIVE pack would still produce from one a superseded pack did.
    ("criticality_registry_version", sa.String(64), ""),
)

#: The markers that fired, verbatim from the registry. JSONB because it is a
#: small list whose interior nothing queries, and because its shape follows the
#: registry's rather than this table's.
EVIDENCE_COLUMN = "criticality_evidence"

#: When the band was last written. Distinct from ``updated_at``, which moves for
#: any change to the row and therefore cannot answer "how old is this band".
BANDED_AT_COLUMN = "criticality_banded_at"


def upgrade() -> None:
    for name, type_, default in COLUMNS:
        op.add_column(
            TABLE_NAME,
            # Server-defaulted rather than back-filled by an UPDATE: a constant
            # written to every journey of every tenant is a lock nobody needs to
            # take for a value the default already supplies.
            sa.Column(name, type_, nullable=False, server_default=default),
        )
    op.add_column(TABLE_NAME, sa.Column(EVIDENCE_COLUMN, JSONB, nullable=True))
    op.add_column(
        TABLE_NAME,
        sa.Column(BANDED_AT_COLUMN, sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    # Reverse order, so a partially-applied upgrade unwinds cleanly.
    op.drop_column(TABLE_NAME, BANDED_AT_COLUMN)
    op.drop_column(TABLE_NAME, EVIDENCE_COLUMN)
    for name, _type, _default in reversed(COLUMNS):
        op.drop_column(TABLE_NAME, name)
