"""QE-Central — the qec_001 migration produces EXACTLY the designed schema.

Complements test_rls_isolation.py (which proves the policies *behave*): this
test proves the migration *shipped* — against a live ``qecentral`` DB at alembic
head it asserts the exact 22-table surface (qec_001 + the qec_002 Phase-7
``tenant_provisioning`` table), the ``tenant_isolation`` policy +
ENABLE/FORCE row security on every one of them, and the invariant-critical
unique indexes/constraints (the one-active-cycle partial index, the audit-ingest
dedupe index, the idempotent-universe identity index, the change-event and
seed-manifest uniqueness).  A missing table, a dropped policy, or a lost partial
index — the exact class of defect that let ``ground_truth_events`` ship without a
migration once — fails here, in CI, before it can reach a tenant.

The expectations are declared INDEPENDENTLY (not imported from the migration) so
that editing the migration's table list cannot silently move this goalpost — a
change to the schema must be a deliberate change here too.

Gated on ``QEC_TEST_QEC_DATABASE_URL`` (the qecentral DB at head).  These are
catalog reads only, so they run through either a privileged or the least-priv
``qec`` role.
"""
from __future__ import annotations

import asyncio
import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

DB_URL = os.environ.get("QEC_TEST_QEC_DATABASE_URL", "")

needs_db = pytest.mark.skipif(
    not DB_URL,
    reason=(
        "QEC_TEST_QEC_DATABASE_URL not set — the migration/schema assertions need "
        "a disposable qecentral DB at alembic head"
    ),
)


def run(coro):
    return asyncio.run(coro)


# The complete, authoritative list of the 22 QE-Central-owned tables (design
# R-7 + Phase-7 fleet).  Declared here so a schema change is a deliberate edit in
# BOTH the migration and this test.
_EXPECTED_TABLES = {
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
        head, tables, rls, policies, indexes, constraints = run(_fetch())

        # ── alembic is exactly at the qec_003 head (… → qec_002 → qec_003) ──
        assert head == "qec_003", f"qecentral alembic head is {head!r}, expected 'qec_003'"

        # ── EXACTLY the 23 designed tables (no more, no fewer) ──────────────
        assert tables == _EXPECTED_TABLES, {
            "missing": sorted(_EXPECTED_TABLES - tables),
            "unexpected": sorted(tables - _EXPECTED_TABLES),
        }
        assert len(_EXPECTED_TABLES) == 23

        # ── every table: RLS enabled + FORCEd + a tenant_isolation policy ───
        for table in _EXPECTED_TABLES:
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
