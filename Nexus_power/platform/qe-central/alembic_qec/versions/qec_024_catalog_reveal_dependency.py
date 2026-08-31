"""Tier 2 — the reveal->child join reaches the catalogue.

WHAT WAS ALREADY TRUE, AND WHY IT WAS NOT ENOUGH.  The branch walk has proved
trigger->child relationships since P1: it answers a question, re-reads the page,
and records what that answer REVEALED on ``journey_branches.reveals`` (qec_011).
``journey_projector.rules_from_branches`` resolves those raw
``kind:name`` / ``group:id`` identities to catalogue question ids.  Both halves
work.  Exactly one caller has ever read the result — the persona projector,
which uses it to PREDICT a path analytically.

The CATALOGUE never saw it.  So a question that exists only because another was
answered a particular way was published with ``depends_on`` empty, and in the
artifact a client is handed it was indistinguishable from a question the
application asks unconditionally.

The sharpest way to put it: qec_019 added ``depends_on`` and documented it as
"the question whose answer this one hangs off (ACT-THEN-DIFF proven)", and the
only thing that ever wrote it was the page's own DECLARED signal.  On a
bare-button questionnaire — a page that declares no dependencies at all, which
is the whole reason the branch walk exists — that column was empty on every row
while the evidence to fill it had already been captured, carried across the
wire, resolved, and dropped one function short.

TWO COLUMNS, BOTH ADDITIVE.

``revealed_by`` is the evidence: which questions, answered with which options,
were observed to reveal this one.  JSONB because it is a small list of records
(``question_id``, ``question``, ``option``) and nothing queries its interior.
A question reachable from several triggers keeps all of them, bounded by
``catalog.MAX_REVEALED_BY``.

``depends_on_source`` is the honesty marker on ``depends_on`` itself, and it is
the reason this is two columns rather than one.  Merging a proven reveal into a
declared dependency would make the two indistinguishable, and they are not the
same claim:

    declared       the application STATES the field hangs off another
    proven_reveal  no page stated anything; a crawl ANSWERED the trigger and
                   the child appeared
    ""             nothing anyone has observed

A DECLARED DEPENDENCY IS NEVER OVERWRITTEN by a reveal.  The two can genuinely
disagree — an app may declare ``depends_on`` naming one control while a
different control is what actually reveals it — and that disagreement is the
interesting part of the record, so both are kept.

DEFAULTS ARE THE PRE-MIGRATION TRUTH.  ``depends_on_source`` defaults to the
empty string and ``revealed_by`` to NULL, which is exactly what every existing
row means today: nothing has been proven about where this question comes from.
The first fold after the new code lands fills them in.  Serving the previous
code against a migrated database is unaffected — nothing existing changes shape.

RLS IS UNCHANGED AND STILL FORCED.  ``catalog_questions`` is tenant-isolated
since qec_012; adding columns does not touch the policy and no table is created.

Revision ID: qec_024
Revises: qec_023
Create Date: 2026-08-31
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers
revision: str = "qec_024"
down_revision: Union[str, None] = "qec_023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE_NAME = "catalog_questions"

#: The pre-migration meaning of every existing row: nothing has been observed
#: about where this question comes from. Defaulted at the DATABASE level, not
#: only in the builder, so a row inserted by any other path cannot arrive
#: claiming a provenance it has no evidence for.
DEFAULT_DEPENDS_ON_SOURCE = ""

#: Which questions, answered how, were observed to reveal this one.
REVEALS_COLUMN = "revealed_by"

#: "" | "declared" | "proven_reveal" — see the module docstring. Sized for the
#: longest of those and nothing more; a wider column would invite a fourth value
#: that no reader knows how to weigh.
SOURCE_COLUMN = "depends_on_source"


def upgrade() -> None:
    op.add_column(TABLE_NAME, sa.Column(REVEALS_COLUMN, JSONB, nullable=True))
    op.add_column(
        TABLE_NAME,
        sa.Column(
            SOURCE_COLUMN, sa.String(16),
            nullable=False,
            # Server-defaulted rather than back-filled by an UPDATE: the table
            # holds a question per control per application per tenant, and a
            # rewrite of every row to write a constant is a lock nobody needs to
            # take for a value the default already supplies.
            server_default=DEFAULT_DEPENDS_ON_SOURCE,
        ),
    )


def downgrade() -> None:
    # Reverse order, so a partially-applied upgrade unwinds cleanly.
    op.drop_column(TABLE_NAME, SOURCE_COLUMN)
    op.drop_column(TABLE_NAME, REVEALS_COLUMN)
