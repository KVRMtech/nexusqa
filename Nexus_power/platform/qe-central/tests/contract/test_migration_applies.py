"""QE-Central — the migration chain produces the designed schema.

Complements test_rls_isolation.py (which proves the policies *behave*): this
test proves the migration *shipped* — against a live ``qecentral`` DB at alembic
head it asserts the design-critical table surface (qec_001 + the qec_002 Phase-7
``tenant_provisioning`` table + qec_003 ``app_environments``), the
``tenant_isolation`` policy + ENABLE/FORCE row security on every one of them,
and the invariant-critical
unique indexes/constraints (the one-active-cycle partial index, the audit-ingest
dedupe index, the idempotent-universe identity index, the change-event and
seed-manifest uniqueness).  A missing table, a dropped policy, or a lost partial
index — the exact class of defect that let ``ground_truth_events`` ship without a
migration once — fails here, in CI, before it can reach a tenant.

The expectations are declared INDEPENDENTLY (not imported from the migration) so
that editing the migration's table list cannot silently move this goalpost — a
change to the schema must be a deliberate change here too.

M0.x correction — this file had rotted into a test that could never pass. It
asserted ``head == 'qec_003'`` and an EXACT set of 23 tables. Thirteen revisions
later the head is qec_017 and the schema carries 38 tables, so the moment a real
Postgres was wired into CI this test would have failed on both counts. It had
never actually run against a database. Two changes fix that permanently:

  * the expected head is DERIVED from the migration chain (``alembic heads``),
    never hardcoded — the pin is what went stale, not the assertion;
  * ``_CORE_TABLES`` is now a REQUIRED-SUBSET, not an exact set. It still names
    the design-critical tables independently of the migration, so deleting one
    fails here, but a new table in a new revision no longer breaks an unrelated
    test. Exact, self-maintaining coverage of the full table surface belongs to
    the gates that derive it from the schema: ``test_rls_coverage_complete.py``
    (every tenant table is isolated) and ``test_schema_drift.py`` (every table is
    modelled or declared).

Gated on ``QEC_TEST_QEC_DATABASE_URL`` (the qecentral DB at head).  These are
catalog reads only, so they run through either a privileged or the least-priv
``qec`` role.
"""
from __future__ import annotations

from _dbgate import ENV_QEC_DB, QEC_DB_URL, db_gate, require_db, run
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

DB_URL = QEC_DB_URL

needs_db = db_gate(
    QEC_DB_URL, ENV_QEC_DB,
    "the migration/schema assertions need a disposable qecentral DB at alembic head",
)


def _expected_head() -> str:
    """The chain head, read from the migration files themselves.

    A revision is head when no other revision names it as ``down_revision``.
    Computed from the files rather than hardcoded, because a hardcoded head is
    precisely the defect this rewrite is correcting.
    """
    import pathlib
    import re

    versions = pathlib.Path(__file__).resolve().parents[2] / "alembic_qec" / "versions"
    revisions, parents = set(), set()
    for path in versions.glob("qec_*.py"):
        source = path.read_text(encoding="utf-8")
        rev = re.search(r'^revision:\s*str\s*=\s*"([^"]+)"', source, re.M)
        down = re.search(r'^down_revision:.*?=\s*"([^"]+)"', source, re.M)
        if rev:
            revisions.add(rev.group(1))
        if down:
            parents.add(down.group(1))
    heads = revisions - parents
    assert len(heads) == 1, (
        f"the qec migration chain does not have exactly one head: {sorted(heads)}"
    )
    return heads.pop()


# The design-critical QE-Central-owned tables (design R-7 + Phase-7 fleet),
# declared here independently of the migration.  This is a REQUIRED SUBSET: every
# one of these must exist, and later revisions are free to add more.
_CORE_TABLES = {
    # S1 — core service
    "client_apps", "qe_explorations", "qe_harness_runs",
    # S4 — scenario governance
    "qec_criticality_registry", "qec_scenarios", "qec_approval_events",
    "qec_coverage_atoms", "qec_certified_invariants", "qec_universe_baselines",
    "qec_coverage_gaps", "qec_case_tiers", "qec_touch_events",
    # S5 — control plane
    "app_cycles", "change_events", "app_fingerprints", "cost_ledger",
    # S3 — repo-intel
    "repo_connections", "app_model_universes", "app_model_atoms",
    "crawl_seed_manifests", "repo_drift_reports",
    # Phase-7 — fleet provisioning (qec_002)
    "tenant_provisioning",
    # Multi-env — Environment Profiles (qec_003)
    "app_environments",
}

# Invariant-enforcing unique indexes/constraints.  The two PARTIAL indexes carry
# a WHERE predicate that is load-bearing (one ACTIVE cycle per app; dedupe only
# ingested audit rows) — a plain unique index would break the design.
_PARTIAL_UNIQUE_INDEXES = {
    "uq_app_cycles_one_active_per_app",
    "uq_qec_touch_events_source_ref",
}
_UNIQUE_INDEXES = {
    "uq_app_model_universes_identity",
}
_UNIQUE_CONSTRAINTS = {
    "uq_change_events_app_dedupe",
    "uq_crawl_seed_manifests_universe",
    "uq_app_environments_name",
}


async def _fetch():
    engine = create_async_engine(DB_URL, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            head = (await conn.execute(
                text("SELECT version_num FROM alembic_version")
            )).scalar()
            tables = set((await conn.execute(text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_type = 'BASE TABLE' "
                "AND table_name <> 'alembic_version'"
            ))).scalars().all())
            # relrowsecurity / relforcerowsecurity per table.
            rls = {
                r[0]: (r[1], r[2]) for r in (await conn.execute(text(
                    "SELECT relname, relrowsecurity, relforcerowsecurity "
                    "FROM pg_class WHERE relkind = 'r' AND relnamespace = "
                    "'public'::regnamespace"
                ))).all()
            }
            # tenant_isolation policies (pg_policies view — world-readable).
            policies = {
                (r[0], r[1]) for r in (await conn.execute(text(
                    "SELECT tablename, policyname FROM pg_policies "
                    "WHERE schemaname = 'public'"
                ))).all()
            }
            indexes = {
                r[0]: r[1] for r in (await conn.execute(text(
                    "SELECT indexname, indexdef FROM pg_indexes "
                    "WHERE schemaname = 'public'"
                ))).all()
            }
            # contype is a "char" column — asyncpg returns it as BYTES (b'u'),
            # which silently fails a == "u" comparison. Cast to text in SQL so
            # the assertion compares strings (this test only started truly
            # RUNNING once the NullPool loop fix landed; the bytes mismatch was
            # latent until then).
            constraints = {
                r[0]: r[1] for r in (await conn.execute(text(
                    "SELECT conname, contype::text FROM pg_constraint con "
                    "JOIN pg_namespace n ON n.oid = con.connamespace "
                    "WHERE n.nspname = 'public'"
                ))).all()
            }
        return head, tables, rls, policies, indexes, constraints
    finally:
        await engine.dispose()


@needs_db
class TestMigrationAppliesCleanly:
    def test_schema_matches_the_design(self):
        require_db(QEC_DB_URL, ENV_QEC_DB)
        head, tables, rls, policies, indexes, constraints = run(_fetch())

        # ── alembic is at the chain head, whatever the chain currently says ──
        expected_head = _expected_head()
        assert head == expected_head, (
            f"qecentral alembic head is {head!r}, but the migration chain's head "
            f"is {expected_head!r} — the database is not fully migrated"
        )

        # ── every design-critical table is present (later ones may be added) ─
        missing = sorted(_CORE_TABLES - tables)
        assert not missing, (
            f"design-critical table(s) absent from the migrated schema: {missing}"
        )

        # ── every table: RLS enabled + FORCEd + a tenant_isolation policy ───
        for table in _CORE_TABLES:
            enabled, forced = rls.get(table, (None, None))
            assert enabled is True, f"{table}: ROW LEVEL SECURITY not enabled"
            assert forced is True, f"{table}: FORCE ROW LEVEL SECURITY not set"
            assert (table, "tenant_isolation") in policies, (
                f"{table}: tenant_isolation policy missing"
            )

        # ── the invariant-critical unique indexes exist ────────────────────
        for name in _PARTIAL_UNIQUE_INDEXES:
            assert name in indexes, f"partial unique index {name} missing"
            assert "WHERE" in indexes[name].upper(), (
                f"{name} must be PARTIAL (a WHERE predicate) — a full unique "
                f"index would break the design: {indexes[name]}"
            )
        for name in _UNIQUE_INDEXES:
            assert name in indexes, f"unique index {name} missing"

        # ── the unique CONSTRAINTS exist (contype 'u') ──────────────────────
        for name in _UNIQUE_CONSTRAINTS:
            assert constraints.get(name) == "u", (
                f"unique constraint {name} missing (got {constraints.get(name)!r})"
            )

    def test_one_active_cycle_index_predicate_names_the_terminal_states(self):
        """The partial index's WHERE must exclude the three terminal states
        (done / failed / budget_stopped) so a finished cycle frees the app slot
        while blackout_deferred keeps holding it (§3.5)."""
        _, _, _, _, indexes, _ = run(_fetch())
        predicate = indexes["uq_app_cycles_one_active_per_app"]
        for terminal in ("done", "failed", "budget_stopped"):
            assert terminal in predicate, (
                f"terminal state {terminal!r} absent from the one-active-cycle "
                f"partial index predicate: {predicate}"
            )
