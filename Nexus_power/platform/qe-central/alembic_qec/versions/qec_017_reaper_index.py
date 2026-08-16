"""M0.x T-DB-02 — the reaper's scan index: ``qe_explorations(status, updated_at)``.

THE SCAN HAD NO INDEX. ``controlplane/reaper.reap_stale_explorations`` runs, once
per tick and ONCE PER TENANT, exactly this shape::

    SELECT exploration_id, tenant_id, status, started_at, created_at, stats
    FROM qe_explorations
    WHERE status IN ('pending','writing','running','dispatched','queued','claimed')
    ORDER BY updated_at ASC
    LIMIT 500

qec_001 gave ``qe_explorations`` only ``(tenant_id, app_id)`` and
``(tenant_id, artifact_id)`` — neither can serve a status predicate and neither
supplies ``updated_at`` order, so every tick was a Seq Scan + Sort of the whole
table, per tenant. At 100 tenants that is 100 full scans a tick. This revision
adds the composite the scan was written for.

Column ORDER is load-bearing: ``status`` first (the equality/IN predicate) then
``updated_at`` (the ordering column), so Postgres can seek each status bucket and
walk it already sorted — the ``LIMIT 500`` then stops early instead of sorting the
whole relation.

The sibling half of T-DB-02, ``app_cycles(tenant_id, app_id, created_at)``, is NOT
created here: qec_001 already ships it as ``ix_app_cycles_tenant_app_created`` with
exactly those three columns in exactly that order. Re-creating it under a second
name would leave two identical indexes to maintain and would make this revision's
downgrade ambiguous. Both indexes are asserted — by name, table, and column order —
by ``tests/contract/test_index_contract.py``, so the T-DB-02 contract is enforced
regardless of which revision installed each half.

Scope discipline: this revision touches ONE object. It creates no table, alters no
column, and adds/removes no policy — so ``downgrade`` can drop exactly what
``upgrade`` created and leave the schema bit-identical to qec_016.

Revision ID: qec_017
Revises: qec_016
Create Date: 2026-08-15
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers
revision: str = "qec_017"
down_revision: Union[str, None] = "qec_016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: The one object this revision owns. Named here so upgrade/downgrade and the
#: contract test can never drift apart on the spelling.
INDEX_NAME = "ix_qe_explorations_status_updated"
TABLE_NAME = "qe_explorations"
COLUMNS = ("status", "updated_at")


def upgrade() -> None:
    # IF NOT EXISTS: some long-lived environments had this index hand-applied
    # during the reaper incident triage. Those databases must upgrade to a
    # no-op, not abort the whole chain on a duplicate-object error.
    #
    # Deliberately NOT `CONCURRENTLY`: alembic runs migrations inside a
    # transaction (env.py `with context.begin_transaction()`) and
    # CREATE INDEX CONCURRENTLY cannot run in one. A plain CREATE INDEX takes a
    # SHARE lock that blocks writes to qe_explorations for the duration — on the
    # current fleet this table is small (one row per crawl) so that is
    # sub-second. Revisit with an offline/autocommit revision if the table ever
    # reaches the millions of rows where the write stall would be felt.
    op.execute(
        f"CREATE INDEX IF NOT EXISTS {INDEX_NAME} "
        f"ON {TABLE_NAME} ({', '.join(COLUMNS)})"
    )


def downgrade() -> None:
    # Drops EXACTLY the index upgrade() created — nothing else. The qec_001
    # indexes on this table (ix_qe_explorations_tenant_app,
    # ix_qe_explorations_tenant_artifact) and every other schema object are
    # untouched, so downgrading to qec_016 restores the qec_016 schema exactly.
    op.execute(f"DROP INDEX IF EXISTS {INDEX_NAME}")
