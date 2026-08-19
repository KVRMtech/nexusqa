"""M0.x T-DB-02 — the two required indexes exist AND the planner actually uses them.

"The migration created the index" is not the deliverable. An index on the wrong
column ORDER, or one the planner declines in favour of a Seq Scan, costs storage
and write throughput and buys nothing. So this module asserts both halves:

**Structure** (cheap, against the shared CI database):

  ==================================  =======================================
  ``ix_qe_explorations_status_updated``  ``qe_explorations (status, updated_at)``
  ``ix_app_cycles_tenant_app_created``   ``app_cycles (tenant_id, app_id, created_at)``
  ==================================  =======================================

  Column order is asserted as an ordered list, not a set: ``(updated_at, status)``
  would satisfy a set comparison and would NOT serve the reaper's status
  predicate.

  Only the first index is created by qec_017. The second has shipped since
  qec_001 as ``ix_app_cycles_tenant_app_created`` — qec_017 deliberately does not
  duplicate it under a new name. Asserting both here is what makes the T-DB-02
  contract whole regardless of which revision installed each half.

**Behaviour** (against a throwaway database seeded with a representative fleet):

  ``EXPLAIN`` the three real queries these indexes exist for and assert the plan
  names the index. The dataset is seeded to production-like SHAPE — 50k
  explorations of which ~0.6% are non-terminal, 100k cycles across 100 tenants ×
  2000 apps — because on a 10-row table PostgreSQL correctly prefers a Seq Scan
  and a plan assertion there would be measuring nothing.

The three queries are quoted from the code that runs them:

  * ``controlplane/reaper.reap_stale_explorations`` — the per-tenant, per-tick
    stale-crawl scan;
  * ``services/touch_meter._recent_cycles`` — the autonomy-trend window;
  * ``controlplane/cycle/driver._scan_for_work`` — the ``last_cycle_at``
    correlated subquery, evaluated once per active app per scan.

Structural tests are gated on ``QEC_TEST_QEC_DATABASE_URL``; the plan tests
additionally need ``QEC_TEST_ADMIN_DATABASE_URL`` (they build and seed their own
database rather than dropping 150k rows into the shared one).
"""
from __future__ import annotations

import pytest
from _dbgate import (
    ADMIN_DB_URL,
    ENV_ADMIN_DB,
    ENV_QEC_DB,
    QEC_DB_URL,
    db_gate,
    new_engine,
    require_db,
    run,
)
from sqlalchemy import text
from test_migration_roundtrip import (  # reuse the scratch-DB lifecycle
    _alembic,
    _create_scratch_db,
    _drop_scratch_db,
)

needs_qec_db = db_gate(
    QEC_DB_URL, ENV_QEC_DB,
    "the index structure contract needs a qecentral DB at alembic head",
)
needs_admin_db = db_gate(
    ADMIN_DB_URL, ENV_ADMIN_DB,
    "the query-plan proof needs a superuser DSN to build a seeded throwaway DB",
)

#: (index name, table, ordered columns). The contract, spelled once.
REQUIRED_INDEXES = [
    ("ix_qe_explorations_status_updated", "qe_explorations", ["status", "updated_at"]),
    ("ix_app_cycles_tenant_app_created", "app_cycles", ["tenant_id", "app_id", "created_at"]),
    # M1.7 / T-GW-04 (qec_018). The dispatch read is
    #   SELECT ... FROM qe_business_rules
    #   WHERE tenant_id = ? AND app_id = ? AND schema_version <= ?
    #   ORDER BY last_proven_at DESC LIMIT 500
    # so the equality columns lead and the ordering column follows -- the same
    # shape, and the same reasoning, as the reaper index above.
    ("uq_qe_business_rules_identity", "qe_business_rules",
     ["tenant_id", "app_id", "rule_key"]),
    ("ix_qe_business_rules_tenant_app_proven", "qe_business_rules",
     ["tenant_id", "app_id", "last_proven_at"]),
]

# ── the real queries, verbatim in shape ─────────────────────────────────────
Q_REAPER = (
    "SELECT exploration_id, tenant_id, status, started_at, created_at, stats "
    "FROM qe_explorations "
    "WHERE status IN ('pending','writing','running','dispatched','queued','claimed') "
    "ORDER BY updated_at ASC LIMIT 500"
)
Q_TREND = (
    "SELECT cycle_id, state, trigger, created_at FROM app_cycles "
    "WHERE tenant_id = 't-42' AND app_id = 'app-1042' "
    "ORDER BY created_at DESC LIMIT 200"
)
Q_LAST_CYCLE = (
    "SELECT max(created_at) FROM app_cycles c "
    "WHERE c.tenant_id = 't-42' AND c.app_id = 'app-1042'"
)


async def _index_columns(url: str) -> dict[str, tuple[str, list[str]]]:
    """{index_name: (table, [columns in index order])} for the public schema.

    Read from ``pg_index.indkey`` (the ordered attribute-number vector) rather
    than parsed out of ``indexdef`` text, so the column ORDER assertion is on
    what PostgreSQL stores, not on a string that happens to read correctly.
    """
    engine = new_engine(url)
    try:
        async with engine.connect() as conn:
            rows = (await conn.execute(text(
                "SELECT ic.relname AS index_name, tc.relname AS table_name, "
                "       a.attname AS column_name, k.ord "
                "FROM pg_index i "
                "JOIN pg_class ic ON ic.oid = i.indexrelid "
                "JOIN pg_class tc ON tc.oid = i.indrelid "
                "JOIN pg_namespace n ON n.oid = tc.relnamespace "
                "JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS k(attnum, ord) ON true "
                "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum "
                "WHERE n.nspname = 'public' "
                "ORDER BY ic.relname, k.ord"
            ))).mappings().all()
    finally:
        await engine.dispose()

    out: dict[str, tuple[str, list[str]]] = {}
    for r in rows:
        table, cols = out.setdefault(r["index_name"], (r["table_name"], []))
        cols.append(r["column_name"])
    return out


# ── structure ───────────────────────────────────────────────────────────────

@needs_qec_db
@pytest.mark.parametrize("index_name, table, columns", REQUIRED_INDEXES,
                         ids=[r[0] for r in REQUIRED_INDEXES])
def test_required_index_exists_on_the_right_table_and_columns(index_name, table, columns):
    require_db(QEC_DB_URL, ENV_QEC_DB)
    found = run(_index_columns(QEC_DB_URL))

    assert index_name in found, (
        f"required index {index_name} does not exist. Present on {table}: "
        f"{sorted(n for n, (t, _) in found.items() if t == table)}"
    )
    actual_table, actual_columns = found[index_name]
    assert actual_table == table, (
        f"{index_name} is on {actual_table}, expected {table}"
    )
    assert actual_columns == columns, (
        f"{index_name} column ORDER is {actual_columns}, expected {columns}. "
        f"Order is load-bearing: a leading column that is not the equality/IN "
        f"predicate cannot serve the query this index exists for."
    )


@needs_qec_db
def test_the_reaper_index_is_a_plain_full_btree():
    """Not partial, not unique, not an expression index.

    A partial index with a WHERE predicate would silently stop covering the
    moment the reaper's ACTIVE_STATUSES tuple gains a state — which it is
    documented to do (``queued``/``claimed`` were added ahead of Phase 2).
    """
    require_db(QEC_DB_URL, ENV_QEC_DB)

    async def _fetch():
        engine = new_engine(QEC_DB_URL)
        try:
            async with engine.connect() as conn:
                return (await conn.execute(text(
                    "SELECT indexdef FROM pg_indexes "
                    "WHERE schemaname='public' AND indexname=:n"
                ), {"n": "ix_qe_explorations_status_updated"})).scalar()
        finally:
            await engine.dispose()

    definition = run(_fetch())
    assert definition, "ix_qe_explorations_status_updated is missing"
    upper = definition.upper()
    assert "USING BTREE" in upper, f"expected a btree index: {definition}"
    assert "WHERE" not in upper, (
        f"the reaper index must NOT be partial — a predicate would stop covering "
        f"as soon as a new non-terminal status is added: {definition}"
    )
    assert "UNIQUE" not in upper, f"the reaper index must not be unique: {definition}"


# ── behaviour: the planner must actually choose them ────────────────────────

_SEED_SQL = """
INSERT INTO qe_explorations
  (exploration_id, tenant_id, app_id, artifact_id, session_id, status,
   explorer_version, extractor_version, stats, error, started_at, created_at, updated_at)
SELECT 'exp-'||g, 't-'||(g%100), 'app-'||(g%2000), 'art-'||g, 'sess-'||g,
  CASE WHEN g%160=0 THEN (ARRAY['pending','writing','running','dispatched','queued','claimed'])[1+(g%6)]
       WHEN g%7=0 THEN 'failed' WHEN g%23=0 THEN 'refused' ELSE 'completed' END,
  'v1', 'qec_live_v1@c'||g, '{}'::jsonb, '',
  now()-(g||' minutes')::interval, now()-(g||' minutes')::interval, now()-(g||' minutes')::interval
FROM generate_series(1, 50000) g;

INSERT INTO app_cycles
  (cycle_id, tenant_id, app_id, trigger, state, selected_scope, honest_gaps, result, error,
   created_at, updated_at)
SELECT 'cyc-'||g, 't-'||(g%100), 'app-'||(g%2000),
  CASE WHEN g%11=0 THEN 'full_floor' ELSE (ARRAY['manual','schedule','webhook_repo','probe_drift'])[1+(g%4)] END,
  -- every seeded cycle is TERMINAL: uq_app_cycles_one_active_per_app is unique
  -- on app_id alone, so a non-terminal seed would collide on the second row per app.
  CASE WHEN g%13=0 THEN 'failed' WHEN g%29=0 THEN 'budget_stopped' ELSE 'done' END,
  '{}'::jsonb, '{}'::jsonb, '{}'::jsonb, '',
  now()-(g||' minutes')::interval, now()-(g||' minutes')::interval
FROM generate_series(1, 100000) g;
"""


async def _seeded_scratch_db():
    name, url = await _create_scratch_db()
    _alembic(url, "upgrade", "head")
    engine = new_engine(url)
    try:
        async with engine.begin() as conn:
            for statement in filter(None, (s.strip() for s in _SEED_SQL.split(";"))):
                await conn.execute(text(statement))
            # Without ANALYZE the planner works off default estimates and its
            # choice would not reflect the seeded distribution.
            await conn.execute(text("ANALYZE qe_explorations"))
            await conn.execute(text("ANALYZE app_cycles"))
    finally:
        await engine.dispose()
    return name, url


async def _plan(url: str, query: str) -> str:
    engine = new_engine(url)
    try:
        async with engine.connect() as conn:
            rows = (await conn.execute(text(f"EXPLAIN (ANALYZE, BUFFERS) {query}"))).scalars().all()
    finally:
        await engine.dispose()
    return "\n".join(rows)


@needs_admin_db
def test_query_plans_select_the_required_indexes():
    """The single expensive test: build, seed, and EXPLAIN all three queries."""
    require_db(ADMIN_DB_URL, ENV_ADMIN_DB)
    run(_drive_plans())


async def _drive_plans():
    name, url = await _seeded_scratch_db()
    try:
        cases = [
            ("reaper stale-crawl scan", Q_REAPER, "ix_qe_explorations_status_updated"),
            ("touch_meter trend window", Q_TREND, "ix_app_cycles_tenant_app_created"),
            ("driver last_cycle_at", Q_LAST_CYCLE, "ix_app_cycles_tenant_app_created"),
        ]
        failures = []
        for label, query, index_name in cases:
            plan = await _plan(url, query)
            print(f"\n── PLAN: {label} ──\n{plan}")
            if index_name not in plan:
                failures.append(
                    f"{label}: the planner did NOT use {index_name} on a "
                    f"representative dataset — the index is not earning its "
                    f"write cost.\n{plan}"
                )
            if "Seq Scan" in plan:
                failures.append(f"{label}: plan still contains a Seq Scan.\n{plan}")
        assert not failures, "\n\n".join(failures)
    finally:
        await _drop_scratch_db(name)


@needs_admin_db
def test_the_reaper_index_is_still_chosen_under_rls():
    """The reaper never runs the bare query — RLS rewrites it.

    In production the reaper scans under ``nexus.current_tenant_id``, so
    PostgreSQL AND-s the policy's tenant predicate into the query. That extra
    predicate can change the plan: a different index may look cheaper and the
    new one be dropped. Assert the tenant-filtered shape still reaches it.
    """
    require_db(ADMIN_DB_URL, ENV_ADMIN_DB)
    run(_drive_rls_plan())


async def _drive_rls_plan():
    name, url = await _seeded_scratch_db()
    try:
        # The predicate RLS injects, written out explicitly (this DSN is a
        # superuser, for whom the policy itself does not apply).
        rls_shaped = (
            "SELECT exploration_id, tenant_id, status, started_at, created_at, stats "
            "FROM qe_explorations "
            "WHERE tenant_id = 't-7' "
            "  AND status IN ('pending','writing','running','dispatched','queued','claimed') "
            "ORDER BY updated_at ASC LIMIT 500"
        )
        plan = await _plan(url, rls_shaped)
        print(f"\n── PLAN: reaper scan with the RLS tenant predicate ──\n{plan}")
        assert "ix_qe_explorations_status_updated" in plan, (
            "under the tenant predicate RLS injects, the planner abandons the "
            f"reaper index — it does not help the query the reaper really "
            f"runs:\n{plan}"
        )
        assert "Seq Scan" not in plan, f"tenant-scoped reaper scan is a Seq Scan:\n{plan}"
    finally:
        await _drop_scratch_db(name)


if __name__ == "__main__":  # pragma: no cover — ad-hoc local run
    pytest.main([__file__, "-v", "-s"])
