"""Phase 0 WS-C — the STANDING RLS coverage gate (never-green-wash at 100 tenants).

Enumerates every BASE TABLE in the qecentral ``public`` schema and asserts each one
either:

  (a) has ENABLE + FORCE ROW LEVEL SECURITY with at least one policy, OR
  (b) is on a short, reason-annotated allowlist of tables that legitimately need no
      per-tenant isolation.

So a NEW table introduced by ANY later phase (Phase 1 seed manifest, Phase 4 data
library, Phase 5 regression tables, Phase 6 signing keys) that ships WITHOUT RLS
FAILS this test in CI. The isolation audit becomes a standing gate on every
migration, not a one-time pass — the classic multi-tenant breach vector closed.

DB-gated: runs only when ``QEC_TEST_QEC_DATABASE_URL`` points at a disposable
qecentral DB at alembic head. Reads only the Postgres catalog (``pg_class`` /
``pg_policies``), so it does not need an RLS-subject role.
"""
from __future__ import annotations

import asyncio
import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool  # test-only: no cross-event-loop pooling

QEC_DB_URL = os.environ.get("QEC_TEST_QEC_DATABASE_URL", "")

needs_qec_db = pytest.mark.skipif(
    not QEC_DB_URL,
    reason="QEC_TEST_QEC_DATABASE_URL not set — the standing RLS coverage gate needs "
           "a disposable qecentral DB at alembic head",
)

# Tables that legitimately carry NO per-tenant data and are therefore exempt from
# the FORCE-RLS requirement. EVERY entry must state WHY. Adding a table here is a
# deliberate, reviewable act — the whole point of the gate is that a new tenant
# table cannot be added silently without either RLS or a justified exemption.
_ALLOWLIST: dict[str, str] = {
    "alembic_version": "migration bookkeeping — a single global revision string, no tenant data",
    "tenants": "the global tenant registry itself; a tenant IS the row (tenant_id is the PK) "
               "and it is provisioned only by the platform super-admin scope, not per-tenant RLS",
    "tenant_provisioning": "platform-super-admin lifecycle registry keyed by tenant_id as PK; "
                           "governed by the fleet super-admin scope, RLS enable/force still "
                           "asserted structurally by test_migration_applies.py",
}


def run(coro):
    return asyncio.run(coro)


async def _catalog():
    engine = create_async_engine(QEC_DB_URL, pool_pre_ping=True, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            tables = (await conn.execute(text(
                "SELECT c.relname AS name, c.relrowsecurity AS enabled, "
                "       c.relforcerowsecurity AS forced "
                "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relkind = 'r'"
            ))).mappings().all()
            policies = (await conn.execute(text(
                "SELECT tablename, count(*) AS n FROM pg_policies "
                "WHERE schemaname = 'public' GROUP BY tablename"
            ))).mappings().all()
        return (
            {r["name"]: (bool(r["enabled"]), bool(r["forced"])) for r in tables},
            {r["tablename"]: int(r["n"]) for r in policies},
        )
    finally:
        await engine.dispose()


@needs_qec_db
def test_every_base_table_is_rls_forced_or_allowlisted():
    rls, policies = run(_catalog())
    assert rls, "no base tables found — is the DSN pointed at a migrated qecentral DB?"

    offenders = []
    for name, (enabled, forced) in sorted(rls.items()):
        if name in _ALLOWLIST:
            continue
        if not (enabled and forced and policies.get(name, 0) >= 1):
            offenders.append(
                f"{name}: enabled={enabled} forced={forced} policies={policies.get(name, 0)}"
            )

    assert not offenders, (
        "Tables missing ENABLE+FORCE RLS + a policy (add RLS in the migration, or "
        "add a reason-annotated exemption to _ALLOWLIST):\n  " + "\n  ".join(offenders)
    )


@needs_qec_db
def test_allowlist_has_no_stale_entries():
    # An allowlist entry for a table that no longer exists is dead weight that hides
    # intent — keep the exemption list honest and minimal.
    rls, _ = run(_catalog())
    stale = [t for t in _ALLOWLIST if t not in rls]
    assert not stale, f"_ALLOWLIST references non-existent tables: {stale}"
