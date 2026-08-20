"""M2.2 / T-BR-01..05 — the catalogue becomes a reviewable evidence artifact.

WHAT ``catalog_questions`` COULD NOT SAY.  The table has held ``business_rule``
since qec_012 and it has been empty on every row ever written, because nothing
produced one: the rules an experiment proved went to ``qe_business_rules``
(qec_018) and were never joined back.  Three more signals were captured by the
crawl, carried across the wire, and then dropped by the layer that composes the
catalogue — so the durable record could not answer, for any question:

  * WHAT OTHER QUESTION IT DEPENDS ON.  The crawler's ACT-THEN-DIFF pass proves
    a dependency by watching a select populate after a driver is answered.  The
    row builder simply did not copy the field, so every conditional question in
    the fleet was catalogued as unconditional — a false statement about the
    application, not a gap in one.
  * WHICH ELEMENT ASKS IT.  Captured as a testid / id / accessible name since
    M0.x and readable only inside the explorer.  A catalogue that describes a
    question in full and cannot point at the control is not reviewable against
    the application.
  * HOW MANY ANSWERS IT REALLY OFFERS.  The browser counts this; the boundary
    dropped it, so a clipped 250-option enumeration was stored as a complete
    one and nothing downstream could tell a prefix from an answer set.

FOUR COLUMNS, ALL ADDITIVE, ALL NULLABLE-OR-DEFAULTED.  Existing rows keep their
values and read back exactly as before; a deployment that runs this migration and
then serves the previous code is unaffected, because nothing existing changes
shape.  The first fold after the new code lands fills them in.

WHY ``business_rule_state`` IS A COLUMN AND NOT AN ABSENCE.  Most questions in
any application gate nothing, so an empty ``business_rule`` is the CORRECT and
final answer for most rows — which makes an empty string ambiguous between "no
rule exists" and "no build has looked".  ``UNVERIFIED``, written explicitly,
removes the ambiguity, and it is what keeps this milestone's rule against
fabricated business logic enforceable: a reviewer can count the rows claiming
evidence and the rows declining to.

RLS IS UNCHANGED AND STILL FORCED.  ``catalog_questions`` is already tenant-
isolated (qec_012); adding columns does not touch the policy, and no new table
is created.

Revision ID: qec_019
Revises: qec_018
Create Date: 2026-08-19
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers
revision: str = "qec_019"
down_revision: Union[str, None] = "qec_018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE_NAME = "catalog_questions"

#: Written on every row by the catalogue builder. ``UNVERIFIED`` is the default
#: at the DATABASE level too, not just in the builder: a row inserted by any
#: other path must not be able to arrive claiming an observed rule it has no
#: evidence for, and a default of ``observed`` would let it.
DEFAULT_RULE_STATE = "UNVERIFIED"

COLUMNS = (
    #: The question whose answer this one hangs off, as PROVEN by ACT-THEN-DIFF
    #: (a select that offered nothing until a driver was chosen, a field that did
    #: not exist until one was). The accessible name of the driver, never a value.
    ("depends_on", sa.String(200), None),
    #: The handle the PAGE declared for the control, with the verdict on whether
    #: it resolves to exactly one element. JSONB because it is a small record
    #: (strategy, value, role, frame, ordinal, anchor, group) whose shape will
    #: grow as the capture does, and because no query filters on its interior.
    ("locator", JSONB, None),
    #: How many answers the control OFFERS in the page. Greater than
    #: ``len(options)`` only when the read was clipped — which is precisely the
    #: case a consumer must be able to see. 0 means "not counted", never "none".
    ("options_total", sa.Integer, "0"),
    #: ``observed`` | ``UNVERIFIED`` — see the module docstring.
    ("business_rule_state", sa.String(24), DEFAULT_RULE_STATE),
)

#: Provenance of the rule sentence, kept beside it rather than inside it: the
#: sentence is evidence and must stay verbatim, while this says which experiment
#: produced it and which control it gates, in fields a reader can filter on.
EVIDENCE_COLUMN = "business_rule_evidence"


def upgrade() -> None:
    for name, type_, default in COLUMNS:
        kwargs = {"nullable": True}
        if default is not None:
            # Server-defaulted rather than back-filled by an UPDATE: the table
            # can hold a question per control per application per tenant, and a
            # rewrite of every row to write a constant is a lock nobody needs to
            # take for a value the default already supplies.
            kwargs = {"nullable": False, "server_default": default}
        op.add_column(TABLE_NAME, sa.Column(name, type_, **kwargs))
    op.add_column(
        TABLE_NAME,
        sa.Column(EVIDENCE_COLUMN, JSONB, nullable=True),
    )


def downgrade() -> None:
    # Reverse order, so a partially-applied upgrade unwinds cleanly.
    op.drop_column(TABLE_NAME, EVIDENCE_COLUMN)
    for name, _type, _default in reversed(COLUMNS):
        op.drop_column(TABLE_NAME, name)
