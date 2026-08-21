"""M3.3 / T-FL-06 — the journeys API N+1 is removed, MEASURED not asserted.

THE DEFECT
==========
``GET /apps/{app_id}/journeys`` looped over every journey and, for each, issued:

  * ``_journey_rollup``  → traversals + branches            (2 queries)
  * ``_runnable_view``   → the adopted case (+ walk signature on the miss path)
  * ``latest_run``       → the run ledger                   (1 query)

≈5 round trips per journey. An app with 60 journeys cost ~300 queries for ONE
page load, and every one of them holds a pooled connection for its duration. At
the concurrent-crawl volume this milestone introduces, that endpoint is what
exhausts the PgBouncer pool first — the crawls do not have to be slow for the
fleet to stall, because the read path has already taken the connections.

HOW THIS IS PROVEN
==================
Not by reading the code and asserting an improvement, but by COUNTING the SQL
statements the endpoint actually issues, at two different journey counts, and
checking the count does not grow with N. A ratio-based assertion cannot be
satisfied by an implementation that merely got faster — only by one whose cost
is genuinely independent of the number of journeys.

The counter hooks SQLAlchemy's ``before_cursor_execute``, so it observes every
statement that reaches the driver, including any a future refactor adds without
noticing.
"""
from __future__ import annotations

import os
import uuid

import pytest

QEC_DB_URL = os.environ.get("QEC_TEST_QEC_DATABASE_URL", "")
SUBSTRATE_DB_URL = os.environ.get("QEC_TEST_SUBSTRATE_DATABASE_URL", "")
if QEC_DB_URL:
    os.environ["QEC_DATABASE_URL"] = QEC_DB_URL
    os.environ["QEC_TEST_DB_NULLPOOL"] = "1"
if SUBSTRATE_DB_URL:
    os.environ["NEXUS_DATABASE_URL_SUBSTRATE"] = SUBSTRATE_DB_URL

from sqlalchemy import event, text  # noqa: E402

needs_db = pytest.mark.skipif(
    not (QEC_DB_URL and SUBSTRATE_DB_URL),
    # The reason must NAME the variables. The A27.1 no-silent-skip gate
    # recognises an infrastructure skip by the environment variable in its
    # reason, so "needs the ... test DSNs" was invisible to it: had the CI
    # database failed to start, these tests would have skipped under
    # QEC_REQUIRE_DB and the build would still have gone green. Exactly the
    # hole that let six T-FL-03 object-storage tests never run.
    reason=("QEC_TEST_QEC_DATABASE_URL / QEC_TEST_SUBSTRATE_DATABASE_URL "
            "not set — T-FL-06 needs the qecentral + substrate test DSNs"),
)
pytestmark = [needs_db, pytest.mark.asyncio]


class _QueryCounter:
    """Counts every statement that reaches the driver on the qec engine."""

    def __init__(self, engine):
        self._sync_engine = engine.sync_engine
        self.statements: list[str] = []

    def _on_exec(self, conn, cursor, statement, params, context, executemany):
        self.statements.append(statement)

    def __enter__(self):
        event.listen(self._sync_engine, "before_cursor_execute", self._on_exec)
        return self

    def __exit__(self, *exc):
        event.remove(self._sync_engine, "before_cursor_execute", self._on_exec)
        return False

    @property
    def count(self) -> int:
        return len(self.statements)


async def _seed_app_with_journeys(tenant: str, app_id: str, n_journeys: int) -> None:
    """Seed an app with ``n_journeys`` journeys, each with a traversal + branch."""
    from app.controlplane.tenant_scope import scope_to_tenant
    from app.db import qec_engine, substrate_engine

    async with substrate_engine.begin() as conn:
        await conn.execute(text(
            # `tenants.name` AND `tenants.domain` are both NOT NULL
            # (nexus_sdk.db.models.TenantRow), and domain is UNIQUE as well, so
            # an insert naming only tenant_id cannot succeed against the real
            # substrate schema. Deriving the domain from the tenant id keeps it
            # unique for free. It went unnoticed because the fleet suite had
            # never been run against a database built from the migration chain
            # — Gate 3 / A20 pushed the chain to CI for the first time and all
            # 19 of these tests failed on this one line.
            "INSERT INTO tenants (tenant_id, name, domain) "
            "VALUES (:t, :t, :t || '.test') "
            "ON CONFLICT (tenant_id) DO NOTHING"), {"t": tenant})

    artifact = "art_" + uuid.uuid4().hex[:10]
    async with qec_engine.begin() as conn:
        await scope_to_tenant(conn, tenant)
        await conn.execute(text(
            "INSERT INTO client_apps (tenant_id, app_id, name, base_url, status, "
            " schedule, fences, latest_artifact_id, created_at, updated_at) "
            "VALUES (:t, :a, :a, 'https://x.example', 'active', "
            " CAST('{}' AS jsonb), CAST('{}' AS jsonb), :art, now(), now())"
        ), {"t": tenant, "a": app_id, "art": artifact})

        for i in range(n_journeys):
            jid = "j_" + uuid.uuid4().hex[:12]
            node_fp = "fp_" + uuid.uuid4().hex[:12]
            await conn.execute(text(
                "INSERT INTO journeys (journey_id, tenant_id, app_id, "
                " entry_fingerprint, flow_id, entry_url, entry_title, "
                " business_name, deepest_steps) "
                "VALUES (:j, :t, :a, :fp, :flow, 'https://x/1', 'Step', "
                "        CAST(:bn AS varchar), 3)"
            ), {"j": jid, "t": tenant, "a": app_id, "fp": node_fp,
                "flow": "flow_" + str(i), "bn": "Journey " + str(i)})
            await conn.execute(text(
                "INSERT INTO journey_traversals (traversal_id, tenant_id, app_id, "
                " journey_id, exploration_id, terminal, path_hash, completed, "
                " path_fps) "
                "VALUES (:tid, :t, :a, :j, 'exp1', 'completed', :ph, true, "
                "        CAST(:fps AS jsonb))"
            ), {"tid": "tr_" + uuid.uuid4().hex[:12], "t": tenant, "a": app_id,
                "j": jid, "ph": "ph_" + str(i),
                "fps": '["' + node_fp + '"]'})
            await conn.execute(text(
                "INSERT INTO journey_branches (branch_id, tenant_id, app_id, "
                " node_fp, control_signature, control_label_norm, "
                " option_label_norm, status) "
                "VALUES (:b, :t, :a, :fp, 'sig', 'label', 'option', 'walked')"
            ), {"b": "br_" + uuid.uuid4().hex[:12], "t": tenant, "a": app_id,
                "fp": node_fp})


async def _measure(n_journeys: int) -> int:
    """Query count for ONE ``list_journeys`` call over ``n_journeys`` journeys."""
    from app.db import qec_engine
    from app.routers.journeys import list_journeys

    tenant = "tfl06_" + uuid.uuid4().hex[:8]
    app_id = "app_" + uuid.uuid4().hex[:8]
    await _seed_app_with_journeys(tenant, app_id, n_journeys)

    with _QueryCounter(qec_engine) as counter:
        result = await list_journeys(app_id=app_id, user={"tenant_id": tenant})
    assert result["journeys_found"] == n_journeys, (
        "the endpoint returned " + str(result["journeys_found"])
        + " journeys, expected " + str(n_journeys) + " — the measurement would "
        "be meaningless if the endpoint did not actually read them all")
    return counter.count


async def test_journeys_endpoint_cost_is_independent_of_journey_count():
    """THE T-FL-06 proof: query count must not grow with the number of journeys.

    Measured at N=2 and N=25. The old implementation issued ~5 queries per
    journey, so it would have gone from ~10 to ~125 — a growth of ~115. The
    batched implementation issues a fixed set of app-scoped queries.

    The tolerance is deliberately not zero: ``reconcile_stale`` and the
    lazy walk-signature fallback are legitimately allowed to vary a little. What
    is NOT allowed is growth PROPORTIONAL to N, which is what the bound below
    rules out — 23 extra journeys may add at most 10 queries in total.
    """
    small = await _measure(2)
    large = await _measure(25)

    growth = large - small
    assert growth <= 10, (
        "the journeys endpoint still scales with journey count: "
        + str(small) + " queries for 2 journeys, " + str(large) + " for 25 "
        "(+" + str(growth) + "). An N+1 remains — each additional journey is "
        "still costing its own round trip, which is what exhausts the "
        "connection pool under concurrent crawl load.")

    # And the absolute cost at 25 journeys must be small enough that the
    # endpoint is not itself a pool hazard.
    assert large <= 30, (
        "the journeys endpoint issued " + str(large) + " queries for 25 "
        "journeys — too many to be safe at concurrency")


async def test_batched_rollup_matches_the_per_journey_rollup():
    """The optimisation must not change the ANSWER, only how it is obtained.

    A faster endpoint that returns different numbers is a regression, not an
    optimisation — so the batched rollup is compared field-by-field against the
    original per-journey implementation on the same data.
    """
    from app.db import tenant_scoped_qec_session
    from app.db.journey_models import JourneyRow
    from app.routers.journeys import (
        _batch_branches,
        _batch_traversals,
        _journey_rollup,
        _rollup_from_batches,
    )
    from sqlalchemy import select

    tenant = "tfl06_eq_" + uuid.uuid4().hex[:8]
    app_id = "app_" + uuid.uuid4().hex[:8]
    await _seed_app_with_journeys(tenant, app_id, 4)

    async with tenant_scoped_qec_session(tenant) as session:
        journeys = (await session.execute(
            select(JourneyRow).where(
                JourneyRow.tenant_id == tenant,
                JourneyRow.app_id == app_id))).scalars().all()
        traversals = await _batch_traversals(session, tenant, app_id)
        branches = await _batch_branches(session, tenant, app_id)
        for j in journeys:
            old = await _journey_rollup(session, tenant, app_id, j)
            new = _rollup_from_batches(
                j, traversals.get(j.journey_id, []), branches)
            assert new == old, (
                "the batched rollup disagrees with the per-journey rollup for "
                + j.journey_id + " — the optimisation changed the answer")
