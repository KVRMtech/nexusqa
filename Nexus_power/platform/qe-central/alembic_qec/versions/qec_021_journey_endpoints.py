"""M2.4 / T-GEN-03 — the journey graph learns which API calls its steps made.

WHAT THE GRAPH COULD NOT SAY.  ``journey_nodes`` has held a control inventory
since qec_010 and ``journey_edges`` has held the trigger label since qec_005, so
the graph could always answer "what did the crawl click, and what did the page
offer".  It could never answer "and what did that click CALL".  The crawl has
captured the XHR/fetch stream per visit for just as long, and every consumer of
it was a post-mortem one — the substrate stores it, the failure attributor scans
it after a run has already gone red.  Nothing joined a captured call to the
control that caused it.

The consequence is the one this milestone exists to remove: a specification
generated from a journey could assert that a page rendered and nothing about the
system behind it, so an API that starts returning the wrong thing behind a UI
that still paints passes every generated test green.

TWO COLUMNS, ONE ON EACH SIDE OF THE JOIN, AND THEY ARE NOT THE SAME FACT.

  * ``journey_nodes.observed_endpoints`` — the calls observed while that STATE
    was open.  A property of a page.
  * ``journey_edges.observed_endpoints`` — the calls the crawl recorded THIS
    TRIGGER firing (M2.5 stamps the in-flight UI action onto every network
    event; the fold joins on that stamp).  A property of a transition, which is
    what a test step actually is.

The edge column is the load-bearing one — "which click caused this POST" is a
question about the click — and the node column is what the compiler falls back
to for crawls that predate the M2.5 stamp, by differencing the two states an
edge connects.  Both are recorded so a reader can tell a READ attribution from
an INFERRED one; collapsing them into one column would destroy exactly that
distinction.

WHY 2xx ONLY LIVES IN THE PRODUCER, NOT IN A CHECK CONSTRAINT.  These columns
feed a compiler, and a generated assertion demanding a 5xx would freeze an
application's bug into its own regression suite as the expected behaviour.  The
narrowing happens where the evidence is shaped (``state_identity._state_endpoints``
and ``endpoint_map.normalize_endpoint``) rather than in the DDL, because the FULL
account — every status, every retry, the auth pattern, the response shape — is
the M2.5 endpoint inventory, a different artifact for a different reader, and a
database rule that refused a 5xx here would read as a claim that no 5xx was ever
seen.

BOTH COLUMNS ARE ADDITIVE AND NULLABLE.  Existing rows keep their values and read
back exactly as before, and a deployment that runs this migration and then serves
the previous code is unaffected — nothing existing changes shape.  NULL means "no
build has looked", which for these tables is honest: the first fold after the new
code lands fills them in, and until then the compiler falls back and says so.

RLS IS UNCHANGED AND STILL FORCED.  Both tables are tenant+app scoped with RLS
forced since qec_005; adding a column does not touch a policy, and no new table
is created.

Revision ID: qec_021
Revises: qec_020
Create Date: 2026-08-19
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers
revision: str = "qec_021"
down_revision: Union[str, None] = "qec_020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

COLUMN_NAME = "observed_endpoints"

#: (table, column) — the two sides of the join described in the docstring.
TARGETS = ("journey_nodes", "journey_edges")


def upgrade() -> None:
    for table in TARGETS:
        op.add_column(table, sa.Column(COLUMN_NAME, JSONB, nullable=True))


def downgrade() -> None:
    # Reverse order, so a partially-applied upgrade unwinds cleanly.
    for table in reversed(TARGETS):
        op.drop_column(table, COLUMN_NAME)
