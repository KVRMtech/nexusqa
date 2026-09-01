"""M2.3 — catalog RETIREMENT: a question may stop being asked, and say so.

THE HOLE THIS CLOSES. ``catalog_questions`` (qec_012) had no lifecycle state at
all: the upsert bumped ``last_seen_artifact`` on every sighting and did nothing
whatever on an absence, so a question the application had stopped asking was
indistinguishable from one it asked this morning. ``journey_nodes.stale``
(qec_005) had a column and a docstring promising "not observed by the app's
latest fold — kept, marked, and excluded from active planning", and exactly one
writer, which assigned it ``False``. Nothing ever set it True and no query ever
filtered on it.

The consequence was not a missing feature, it was a WRONG CATALOGUE. Node
control inventories merge by union, so a removed control stayed for ever; every
later snapshot still contained it; and ``catalog_diff``'s ``removed`` bucket —
which the code has always computed — was unreachable from any real crawl. A
regulated client could be shown a catalogue asserting the application still asks
a question it dropped two releases ago, and nothing in the system could notice.

WHAT THIS ADDS. The lifecycle state (active → stale → retired) on both tables
that hold a question's identity:

  * ``catalog_questions`` — the durable, deduped Master Catalog row.
  * ``journey_branches``  — one row per ANSWER of a questionnaire question, which
    is where every choice question in the catalogue comes from. Without it a
    withdrawn Yes/No would be resurrected as active by the branch fold-in the
    moment the node side retired it.

NOTHING IS EVER DELETED. Retirement is a stamp, not a DELETE: the question id,
its content, its first-seen record, the timestamp it retired and the crawl that
retired it all survive, and remain queryable for audit. Retirement is also
reversible — an application that asks the question again revives it — so these
columns are nullable/defaulted rather than a one-way tombstone table.

PURELY ADDITIVE: new columns with defaults, plus one partial index. Existing rows
become ``active``, which is what they were being read as anyway.

Revision ID: qec_020
Revises: qec_019
Create Date: 2026-08-19
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers
revision: str = "qec_020"
down_revision: Union[str, None] = "qec_019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: (table, column, type, server_default) — the lifecycle record, identically
#: shaped on both tables so one reader can interpret either.
_LIFECYCLE_COLUMNS = (
    #: Previously known, NOT observed by the crawl that last looked for it.
    ("stale", sa.Boolean(), sa.text("false"), False),
    #: When the absence became conclusive. NULL while the question is live —
    #: the presence of this value IS the retirement.
    ("retired_at", sa.DateTime(timezone=True), None, True),
    #: WHICH crawl retired it. An audit answer to "on whose evidence?".
    ("retired_in_crawl", sa.String(64), sa.text("''"), False),
    #: ``conclusive_absence`` (one trustworthy crawl re-read the page and it was
    #: gone) or ``repeated_absence`` (N degraded crawls agreed). Recorded because
    #: the two carry different weight and a reader must not have to guess which.
    ("retire_reason", sa.String(32), sa.text("''"), False),
    #: How many crawls have now looked and not found it. Kept after retirement:
    #: it is the evidence trail behind the stamp.
    ("missed_crawls", sa.Integer(), sa.text("0"), False),
    #: The last crawl that DID observe it — distinct from ``last_seen_artifact``,
    #: which the old upsert bumped on every fold whether it saw the question or
    #: not, and which therefore cannot answer "when did we last actually see it".
    ("last_seen_crawl", sa.String(64), sa.text("''"), False),
)

_TABLES = ("catalog_questions", "journey_branches")


def _has_table(name: str) -> str:
    return (f"SELECT FROM information_schema.tables WHERE table_schema='public' "
            f"AND table_name='{name}'")


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    existing_tables = set(inspector.get_table_names())

    for table in _TABLES:
        if table not in existing_tables:
            # A fresh database creates these from the models; an older one has
            # them from qec_005/qec_012. Skipping rather than failing keeps the
            # migration runnable against both, and is the qec_012 house pattern.
            continue
        present = {c["name"] for c in inspector.get_columns(table)}
        for name, type_, default, nullable in _LIFECYCLE_COLUMNS:
            if name in present:
                continue
            op.add_column(table, sa.Column(
                name, type_, nullable=nullable, server_default=default))

    # ACTIVE PLANNING READS THIS. The catalogue read path, scenario derivation
    # and the version snapshot all ask for the NON-retired questions of one app;
    # without an index that is a full scan of every question the tenant has ever
    # catalogued, retired ones included, on every crawl completion.
    if "catalog_questions" in existing_tables:
        op.execute("""
            CREATE INDEX IF NOT EXISTS ix_catalog_questions_active
                ON catalog_questions (tenant_id, app_id)
             WHERE retired_at IS NULL
        """)


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    existing_tables = set(inspector.get_table_names())

    op.execute("DROP INDEX IF EXISTS ix_catalog_questions_active")
    for table in _TABLES:
        if table not in existing_tables:
            continue
        present = {c["name"] for c in inspector.get_columns(table)}
        for name, _type, _default, _nullable in _LIFECYCLE_COLUMNS:
            if name in present:
                op.drop_column(table, name)
