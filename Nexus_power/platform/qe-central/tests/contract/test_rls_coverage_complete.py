"""M0.x T-DB-03 — the STANDING, SELF-DISCOVERING tenant-isolation coverage gate.

The pre-M0.x version of this file enumerated every base table and compared it to a
hand-maintained ``_ALLOWLIST``. That has the wrong failure direction: the list had
gone stale (``advance_label_priors`` and ``mechanic_priors``, added by qec_004 and
qec_009, were on neither side of it), and a stale allowlist fails *closed* on
harmless tables while a forgotten exemption fails *open* on real ones.

This version derives the subject set from the schema itself:

    a table with a ``tenant_id`` column holds per-tenant data,
    therefore it MUST be isolated by the database.

Nothing is hand-listed on that side. A migration that adds a tenant-scoped table
is automatically subject to this contract the moment it lands — which is the whole
point: future schema changes inherit the gate for free.

The four assertions:

  1. **Coverage** — every ``tenant_id`` table has ENABLE + FORCE row security and
     at least one policy. (FORCE matters: without it the table OWNER — which is
     the ``qec`` application role — is exempt, and every policy is decorative.)
  2. **Operation coverage** — the policies on each such table cover SELECT,
     INSERT, UPDATE and DELETE, and every write command resolves to a non-empty
     CHECK expression. Note the PostgreSQL rule this assertion is built on: when
     ``WITH CHECK`` is omitted, the ``USING`` expression is used as the check for
     new rows. ``pg_policies.with_check`` is then NULL even though the table IS
     protected — reading that column alone reports a breach that does not exist
     (it did, on ``control_mechanics``, until a live INSERT attempt disproved it).
     So the assertion is on the EFFECTIVE check, ``with_check or qual``.
  3. **The policy is really tenant-scoped** — its effective expressions reference
     both ``tenant_id`` and the ``nexus.current_tenant_id`` GUC. A policy of
     ``USING (true)`` satisfies "has a policy" and isolates nothing.
  4. **No tenant table escapes by naming** — every table WITHOUT a ``tenant_id``
     column is on a short, reason-annotated allowlist, so a genuinely
     tenant-scoped table cannot dodge assertion 1 by spelling its column
     differently.

Plus a CANARY (:func:`test_the_gate_actually_has_teeth`) that materialises an
unprotected ``tenant_id`` table and proves the discovery query flags it. Without
that, a gate that silently discovered zero tables would report a green.

Gated on ``QEC_TEST_QEC_DATABASE_URL``; catalog reads only, so it runs through a
privileged or a least-privilege role. Behavioural cross-tenant proof (who can
actually read/write whose rows) lives in ``test_rls_isolation.py``.
"""
from __future__ import annotations

import uuid

import pytest
from _dbgate import ENV_QEC_DB, QEC_DB_URL, db_gate, new_engine, require_db, run
from sqlalchemy import text

needs_qec_db = db_gate(
    QEC_DB_URL, ENV_QEC_DB,
    "the standing RLS coverage gate needs a disposable qecentral DB at alembic head",
)

#: Tables with NO ``tenant_id`` column. Each entry states WHY the data is not
#: per-tenant. This list is the ONLY hand-maintained part of the gate, and it is
#: checked in both directions: an entry naming a table that no longer exists is a
#: failure, and a new tenant-free table not listed here is a failure. Adding to it
#: is therefore a deliberate, reviewable act.
_NO_TENANT_COLUMN: dict[str, str] = {
    "alembic_version": (
        "migration bookkeeping — a single global revision string, no tenant data"
    ),
    "advance_label_priors": (
        "qec_004 federated advance priors — deliberately VALUE-FREE and "
        "CROSS-TENANT: it stores label SIGNATURES and outcome counts with no "
        "tenant column by design, so one tenant's proven advance helps the fleet "
        "without exposing which tenant proved it"
    ),
    "mechanic_priors": (
        "qec_009 federated control-mechanic priors — same value-free cross-tenant "
        "design as advance_label_priors: control SHAPES and success counts only, "
        "never a tenant's values"
    ),
}

#: The GUC every tenant policy must consult.
_TENANT_GUC = "nexus.current_tenant_id"

#: The four DML commands a tenant table must be isolated for.
_REQUIRED_COMMANDS = ("SELECT", "INSERT", "UPDATE", "DELETE")
#: …of which these produce a NEW row and therefore need a check expression, or
#: tenant B could write a row stamped with tenant A's id. DELETE has no new row
#: and is governed by USING alone (you cannot delete what you cannot see).
_WRITE_COMMANDS = ("INSERT", "UPDATE")


def _effective_check(policy: dict) -> str:
    """The expression PostgreSQL actually applies to a NEW row.

    Per the CREATE POLICY docs: "If WITH CHECK is omitted, then the USING
    expression is used both to determine which rows are visible and which new
    rows will be allowed to be added." ``pg_policies.with_check`` is NULL in that
    case, so a naive read of that column reports a cross-tenant-write hole on a
    table that is in fact protected.
    """
    return policy["with_check"] or policy["qual"]


async def _introspect(url: str) -> dict:
    """One catalog snapshot: tables, tenant_id presence, RLS flags, policies."""
    engine = new_engine(url)
    try:
        async with engine.connect() as conn:
            tables = (await conn.execute(text(
                "SELECT c.relname AS name, "
                "       c.relrowsecurity AS enabled, "
                "       c.relforcerowsecurity AS forced, "
                "       EXISTS (SELECT 1 FROM information_schema.columns col "
                "               WHERE col.table_schema = 'public' "
                "                 AND col.table_name = c.relname "
                "                 AND col.column_name = 'tenant_id') AS has_tenant_id "
                "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relkind = 'r'"
            ))).mappings().all()
            policies = (await conn.execute(text(
                "SELECT tablename, policyname, cmd, permissive, "
                "       coalesce(qual, '') AS qual, "
                "       coalesce(with_check, '') AS with_check "
                "FROM pg_policies WHERE schemaname = 'public'"
            ))).mappings().all()
    finally:
        await engine.dispose()

    by_table: dict[str, list[dict]] = {}
    for p in policies:
        by_table.setdefault(p["tablename"], []).append(dict(p))
    return {
        "tables": {r["name"]: dict(r) for r in tables},
        "policies": by_table,
    }


def _snapshot() -> dict:
    require_db(QEC_DB_URL, ENV_QEC_DB)
    return run(_introspect(QEC_DB_URL))


def _tenant_tables(snap: dict) -> list[str]:
    return sorted(n for n, t in snap["tables"].items() if t["has_tenant_id"])


@needs_qec_db
def test_discovery_finds_the_tenant_tables():
    """The gate must actually have a subject set.

    A discovery bug that returned zero tables would make every assertion below
    vacuously true — the exact way a coverage gate rots into a no-op.
    """
    snap = _snapshot()
    assert snap["tables"], (
        "no base tables found — is the DSN pointed at a MIGRATED qecentral DB?"
    )
    tenant_tables = _tenant_tables(snap)
    assert len(tenant_tables) >= 20, (
        f"only {len(tenant_tables)} tenant-scoped tables discovered "
        f"({tenant_tables}) — qec_001 alone ships 21. The discovery query is "
        f"broken or the database is not at head."
    )


@needs_qec_db
def test_every_tenant_table_has_rls_enabled_and_forced():
    """T-DB-03 assertion 1 — coverage."""
    snap = _snapshot()
    offenders = []
    for name in _tenant_tables(snap):
        t = snap["tables"][name]
        n_policies = len(snap["policies"].get(name, []))
        if not (t["enabled"] and t["forced"] and n_policies >= 1):
            offenders.append(
                f"{name}: tenant_id column present but "
                f"ENABLE={t['enabled']} FORCE={t['forced']} policies={n_policies}"
            )
    assert not offenders, (
        "Tenant-scoped tables WITHOUT database-enforced isolation — add "
        "ENABLE + FORCE ROW LEVEL SECURITY and a tenant policy in the migration "
        "that created the table:\n  " + "\n  ".join(offenders)
    )


@needs_qec_db
def test_every_tenant_policy_covers_all_four_dml_commands():
    """T-DB-03 assertion 2 — operation coverage.

    ``cmd = 'ALL'`` covers everything; otherwise the per-command policies must
    union to all four. Write commands additionally need an EFFECTIVE check
    expression (see :func:`_effective_check` — ``WITH CHECK`` if given, else the
    ``USING`` clause), without which tenant B can INSERT a row stamped tenant A.
    """
    snap = _snapshot()
    offenders = []
    for name in _tenant_tables(snap):
        policies = snap["policies"].get(name, [])
        covered = set()
        checked = set()
        for p in policies:
            cmds = _REQUIRED_COMMANDS if p["cmd"] == "ALL" else (p["cmd"],)
            covered.update(cmds)
            if _effective_check(p):
                checked.update(cmds)
        missing = [c for c in _REQUIRED_COMMANDS if c not in covered]
        unchecked = [c for c in _WRITE_COMMANDS if c in covered and c not in checked]
        if missing:
            offenders.append(f"{name}: no policy covers {missing}")
        if unchecked:
            offenders.append(
                f"{name}: {unchecked} policy has neither WITH CHECK nor USING — "
                f"nothing constrains the tenant_id of a NEW row, so a "
                f"cross-tenant write is possible"
            )
    assert not offenders, (
        "Tenant policies with incomplete operation coverage:\n  " + "\n  ".join(offenders)
    )


@needs_qec_db
def test_every_tenant_policy_is_actually_scoped_to_the_tenant_guc():
    """T-DB-03 assertion 3 — the policy expression must do real work.

    ``CREATE POLICY p ON t USING (true)`` satisfies "the table has a policy" and
    isolates precisely nothing. Require both the column and the GUC by name.
    """
    snap = _snapshot()
    offenders = []
    for name in _tenant_tables(snap):
        for p in snap["policies"].get(name, []):
            clauses = {"USING": p["qual"], "CHECK(effective)": _effective_check(p)}
            for label, expr in clauses.items():
                if not expr:
                    continue  # absence is assertion 2's business, not ours
                if "tenant_id" not in expr or _TENANT_GUC not in expr:
                    offenders.append(
                        f"{name}.{p['policyname']} [{label}]: does not compare "
                        f"tenant_id against {_TENANT_GUC} — {expr!r}"
                    )
    assert not offenders, (
        "Policies that exist but do not isolate by tenant:\n  " + "\n  ".join(offenders)
    )


@needs_qec_db
def test_tables_without_a_tenant_id_column_are_declared_and_justified():
    """T-DB-03 assertion 4 — the naming escape hatch is closed, both ways."""
    snap = _snapshot()
    tenant_free = {n for n, t in snap["tables"].items() if not t["has_tenant_id"]}

    undeclared = sorted(tenant_free - set(_NO_TENANT_COLUMN))
    assert not undeclared, (
        "New table(s) with NO tenant_id column. If the data IS per-tenant, add a "
        "tenant_id column + RLS (and this gate covers it automatically). If it is "
        "genuinely cross-tenant, add a reason-annotated entry to "
        f"_NO_TENANT_COLUMN:\n  {undeclared}"
    )

    stale = sorted(set(_NO_TENANT_COLUMN) - set(snap["tables"]))
    assert not stale, (
        f"_NO_TENANT_COLUMN names table(s) that no longer exist — a dead "
        f"exemption hides intent: {stale}"
    )

    gained = sorted(t for t in _NO_TENANT_COLUMN if t in snap["tables"]
                    and snap["tables"][t]["has_tenant_id"])
    assert not gained, (
        f"Table(s) declared tenant-free have GAINED a tenant_id column — remove "
        f"them from _NO_TENANT_COLUMN so the RLS assertions start covering "
        f"them: {gained}"
    )


@needs_qec_db
def test_the_gate_actually_has_teeth():
    """CANARY — prove this gate would FAIL on an unprotected tenant table.

    Every assertion above is of the form "no offenders". A gate like that reports
    green both when the schema is perfect and when its discovery is broken. So:
    create a real table with a ``tenant_id`` column and NO row security, re-run
    the discovery, and assert it is caught. Then drop it.

    This is the difference between "the coverage test passed" and "the coverage
    test can still detect a breach".
    """
    require_db(QEC_DB_URL, ENV_QEC_DB)
    canary = f"_rls_canary_{uuid.uuid4().hex[:10]}"

    async def _drive():
        engine = new_engine(QEC_DB_URL)
        try:
            async with engine.begin() as conn:
                await conn.execute(text(
                    f"CREATE TABLE {canary} "
                    f"(id text PRIMARY KEY, tenant_id text NOT NULL)"
                ))
            try:
                snap = await _introspect(QEC_DB_URL)
                assert canary in _tenant_tables(snap), (
                    "discovery did not even SEE the canary table — the "
                    "tenant_id detection query is broken"
                )
                t = snap["tables"][canary]
                caught = not (t["enabled"] and t["forced"]
                              and len(snap["policies"].get(canary, [])) >= 1)
                assert caught, (
                    "an unprotected tenant_id table was NOT flagged — this "
                    "coverage gate is vacuous and would pass a real breach"
                )
            finally:
                async with engine.begin() as conn:
                    await conn.execute(text(f"DROP TABLE IF EXISTS {canary}"))
        finally:
            await engine.dispose()

    run(_drive())


@needs_qec_db
def test_coverage_totals_are_reported_for_the_record():
    """Print the M0.x §25 RLS ledger: tenant tables == RLS == policy-protected."""
    snap = _snapshot()
    tenant_tables = _tenant_tables(snap)
    with_rls = [n for n in tenant_tables
                if snap["tables"][n]["enabled"] and snap["tables"][n]["forced"]]
    with_policy = [n for n in tenant_tables if snap["policies"].get(n)]

    print(
        f"\nRLS COVERAGE LEDGER\n"
        f"  base tables (excl. alembic_version): "
        f"{len([t for t in snap['tables'] if t != 'alembic_version'])}\n"
        f"  tables with tenant_id:               {len(tenant_tables)}\n"
        f"  tables with ENABLE+FORCE RLS:        {len(with_rls)}\n"
        f"  tables with a tenant policy:         {len(with_policy)}\n"
        f"  declared tenant-free (justified):    {len(_NO_TENANT_COLUMN)}"
    )
    assert len(tenant_tables) == len(with_rls) == len(with_policy), {
        "tenant_tables": len(tenant_tables),
        "rls_protected": len(with_rls),
        "policy_protected": len(with_policy),
        "missing_rls": sorted(set(tenant_tables) - set(with_rls)),
        "missing_policy": sorted(set(tenant_tables) - set(with_policy)),
    }


if __name__ == "__main__":  # pragma: no cover — ad-hoc local run
    pytest.main([__file__, "-v"])
