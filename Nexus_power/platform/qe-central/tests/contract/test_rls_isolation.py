"""QE-Central — behavioural Row-Level-Security tenant-isolation proof.

THE security-critical test Phase 5.5 adds: the qec_001 migration NAMES a
``tenant_isolation`` policy + ``FORCE ROW LEVEL SECURITY`` on all 21 tables
(and VKPower migration 034 does the same for ``page_visits``), but nothing has
ever proven those policies actually ISOLATE at runtime.  This test does, on the
LIVE schema, connected through the LEAST-PRIVILEGE application roles — because a
PostgreSQL superuser (or a BYPASSRLS role) silently bypasses RLS, so a test run
through a privileged connection would prove nothing.  It therefore refuses to
run (skips loudly) unless the connection is genuinely subject to RLS.

For each representative table it proves, end to end:

  1. a row inserted as tenant A (GUC = A) is visible to A;
  2. under GUC = B, ``SELECT`` returns ZERO of A's rows — even by primary key
     (the USING clause hides them);
  3. an ``INSERT`` carrying ``tenant_id = A`` while GUC = B is REJECTED by the
     ``WITH CHECK`` clause (a row can never be smuggled into another tenant);
  4. ``relforcerowsecurity`` AND ``relrowsecurity`` are both on (owner-included
     enforcement — the property that makes 1-3 hold even for the table owner).

Gating (both DISPOSABLE Postgres, at alembic head):

  * qecentral tables (client_apps, qec_scenarios, qec_approval_events,
    app_cycles, cost_ledger) — ``QEC_TEST_QEC_DATABASE_URL``, which in CI is
    connected as the non-superuser ``qec`` role.
  * substrate table page_visits — ``QEC_TEST_SUBSTRATE_DATABASE_URL``, connected
    as the non-superuser ``qec_substrate`` role (superuser bypasses RLS, so the
    substrate proof needs its own least-priv DSN distinct from
    ``QEC_TEST_DATABASE_URL``).

scripts/qec_ci_db_setup.sh wires both.  Locally, set either/both to a throwaway
Postgres and the matching group runs; unset, it skips — never a false green.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from typing import Awaitable, Callable

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from sqlalchemy.pool import NullPool

QEC_DB_URL = os.environ.get("QEC_TEST_QEC_DATABASE_URL", "")
SUBSTRATE_DB_URL = os.environ.get("QEC_TEST_SUBSTRATE_DATABASE_URL", "")

needs_qec_db = pytest.mark.skipif(
    not QEC_DB_URL,
    reason=(
        "QEC_TEST_QEC_DATABASE_URL not set — the RLS isolation proof needs a "
        "disposable qecentral DB at alembic head, connected as the non-superuser "
        "`qec` role (a superuser connection bypasses RLS)"
    ),
)
needs_substrate_db = pytest.mark.skipif(
    not SUBSTRATE_DB_URL,
    reason=(
        "QEC_TEST_SUBSTRATE_DATABASE_URL not set — the page_visits RLS proof needs "
        "the nexus substrate DB connected as the non-superuser `qec_substrate` role"
    ),
)

_GUC = "SELECT set_config('nexus.current_tenant_id', :t, true)"


def run(coro):
    return asyncio.run(coro)


def _fresh(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


# ── RLS enforceability preflight ────────────────────────────────────────────
async def _rls_is_enforceable(engine) -> tuple[bool, str]:
    """Return (enforceable, current_user).

    RLS does NOT apply to superusers or roles with BYPASSRLS, so a test run
    through such a role would falsely "pass" isolation it never enforced.  We
    detect that and skip rather than lie.
    """
    async with engine.connect() as conn:
        user = (await conn.execute(text("SELECT current_user"))).scalar()
        is_super = (await conn.execute(text("SELECT current_setting('is_superuser')"))).scalar()
        bypass = (await conn.execute(
            text("SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user")
        )).scalar()
    enforceable = (str(is_super).lower() != "on") and (bypass is not True)
    return enforceable, str(user)


async def _skip_unless_enforceable(engine) -> None:
    """Skip (loudly) if this connection is not actually subject to RLS."""
    enforceable, user = await _rls_is_enforceable(engine)
    if not enforceable:
        pytest.skip(
            f"connected as '{user}', which BYPASSES RLS (superuser/BYPASSRLS) — "
            "point this DSN at a least-privilege role (qec / qec_substrate) to "
            "exercise the isolation policy"
        )


def _new_engine(url: str):
    """A NullPool async engine.

    NullPool means no connection is reused across event loops — each test drives
    the DB inside ONE ``asyncio.run`` loop, and pooled connections bound to a
    prior (closed) loop can never resurface.
    """
    return create_async_engine(url, poolclass=NullPool)


# ── GUC-scoped execution primitives (RLS context = one transaction) ─────────
async def _scoped_scalar(engine, tenant: str, sql: str, params: dict | None = None):
    async with engine.connect() as conn:
        trans = await conn.begin()
        try:
            await conn.execute(text(_GUC), {"t": tenant})
            value = (await conn.execute(text(sql), params or {})).scalar()
            await trans.commit()
            return value
        except Exception:
            await trans.rollback()
            raise


async def _scoped_write(engine, tenant: str, sql: str, params: dict) -> None:
    async with engine.connect() as conn:
        trans = await conn.begin()
        try:
            await conn.execute(text(_GUC), {"t": tenant})
            await conn.execute(text(sql), params)
            await trans.commit()
        except Exception:
            await trans.rollback()
            raise


def _insert_sql(table: str, pk_col: str, extra_cols: dict) -> str:
    cols = [pk_col, "tenant_id", *extra_cols.keys()]
    placeholders = ", ".join(f":{c}" for c in cols)
    return f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})"


# ── The reusable isolation proof (identical for every table) ────────────────
async def _prove_isolation(
    engine,
    *,
    table: str,
    pk_col: str,
    build_extra: Callable[[AsyncConnection, str], Awaitable[dict]],
    reject_overrides: Callable[[], dict] | None = None,
) -> None:
    """Run the four-part isolation proof for one table.

    ``build_extra`` runs UNDER GUC=tenant_a inside the seeding transaction and
    returns the non-(pk, tenant) required columns for the row — creating any FK
    parents it needs on the way (e.g. page_visits' canonical_artifacts).

    The cross-tenant WITH-CHECK insert REUSES those columns (so FK parents stay
    valid — an RI check bypasses RLS and still resolves them), but a fresh pk AND
    ``reject_overrides`` replace any UNIQUE-bearing columns (the one-active-cycle
    partial index on app_id; the (artifact, sequence, version) page_visit key) so
    the ONLY constraint that can reject it is the RLS policy — never a unique
    violation masquerading as isolation.
    """
    tenant_a, tenant_b = _fresh("t-a"), _fresh("t-b")
    pk_a, pk_reject = _fresh("row"), _fresh("row")

    extra_holder: dict = {}

    # (1) Seed FK parents + capture the row's extra columns, then insert the
    #     tenant-A row — all in ONE transaction under GUC=A.
    async with engine.connect() as conn:
        trans = await conn.begin()
        try:
            await conn.execute(text(_GUC), {"t": tenant_a})
            extra_holder.update(await build_extra(conn, tenant_a))
            params = {pk_col: pk_a, "tenant_id": tenant_a, **extra_holder}
            await conn.execute(text(_insert_sql(table, pk_col, extra_holder)), params)
            await trans.commit()
        except Exception:
            await trans.rollback()
            raise

    seen_by_a = await _scoped_scalar(
        engine, tenant_a,
        f"SELECT count(*) FROM {table} WHERE {pk_col} = :pk", {"pk": pk_a},
    )
    assert seen_by_a == 1, f"{table}: tenant A cannot see its OWN row (RLS misconfigured)"

    # (2) Tenant B sees ZERO of A's rows — by tenant filter AND by primary key.
    by_tenant = await _scoped_scalar(
        engine, tenant_b,
        f"SELECT count(*) FROM {table} WHERE tenant_id = :a", {"a": tenant_a},
    )
    assert by_tenant == 0, f"{table}: tenant B can READ tenant A's rows — ISOLATION BREACH"
    by_pk = await _scoped_scalar(
        engine, tenant_b,
        f"SELECT count(*) FROM {table} WHERE {pk_col} = :pk", {"pk": pk_a},
    )
    assert by_pk == 0, f"{table}: tenant B can read tenant A's row by primary key — BREACH"

    # (3) Tenant B cannot WRITE a row tagged with tenant A (WITH CHECK rejects).
    reject_extra = {**extra_holder, **(reject_overrides() if reject_overrides else {})}
    reject_params = {pk_col: pk_reject, "tenant_id": tenant_a, **reject_extra}
    with pytest.raises(Exception) as exc_info:
        await _scoped_write(
            engine, tenant_b,
            _insert_sql(table, pk_col, reject_extra), reject_params,
        )
    assert "row-level security" in str(exc_info.value).lower(), (
        f"{table}: cross-tenant INSERT failed for the WRONG reason "
        f"(expected a WITH CHECK RLS rejection): {exc_info.value}"
    )
    # And the rejected row truly did not land (visible to nobody).
    leaked = await _scoped_scalar(
        engine, tenant_a,
        f"SELECT count(*) FROM {table} WHERE {pk_col} = :pk", {"pk": pk_reject},
    )
    assert leaked == 0, f"{table}: a WITH-CHECK-rejected row still persisted"

    # (4) The structural guarantee: FORCE + ENABLE row security are both on.
    forced = await _scoped_scalar(
        engine, tenant_a,
        "SELECT relforcerowsecurity FROM pg_class WHERE relname = :t", {"t": table},
    )
    enabled = await _scoped_scalar(
        engine, tenant_a,
        "SELECT relrowsecurity FROM pg_class WHERE relname = :t", {"t": table},
    )
    assert forced is True, f"{table}: FORCE ROW LEVEL SECURITY is OFF"
    assert enabled is True, f"{table}: ROW LEVEL SECURITY is OFF"

    # Tidy up (disposable DB, but keep it clean for repeat local runs).
    await _scoped_write(
        engine, tenant_a, f"DELETE FROM {table} WHERE {pk_col} = :pk", {"pk": pk_a},
    )


# ── qecentral-owned tables ──────────────────────────────────────────────────
# table, pk column, seed non-(pk,tenant) columns, reject overrides.  None of
# these has a tenant-scoped FK parent, so the seed columns are static.  Only
# app_cycles is UNIQUE-sensitive: its one-active-cycle partial index is on
# app_id ALONE (global across tenants), so the reject uses a fresh app_id.
_QEC_TABLES = [
    ("client_apps", "app_id",
     {"name": "RLS Probe App", "base_url": "https://rls.probe.test/"}, {}),
    ("qec_scenarios", "scenario_id", {}, {}),
    ("qec_approval_events", "event_id",
     {"subject_kind": "scenario", "subject_id": "sc-rls-probe"}, {}),
    ("app_cycles", "cycle_id",
     {"app_id": None, "trigger": "manual"}, {"app_id": None}),  # app_id filled fresh
    ("cost_ledger", "entry_id", {"unit": "browser_seconds"}, {}),
    # app_environments (qec_003) holds the KMS-sealed env credentials (creds_blob) —
    # the most sensitive newer table. Prove tenant B cannot read tenant A's sealed
    # env row by PK, nor INSERT a row tagged as tenant A (WITH CHECK rejection).
    ("app_environments", "environment_id",
     {"app_id": None, "name": "RLS Probe Env"}, {"app_id": None}),
    # NOTE: the Phase-7 ``tenant_provisioning`` table (qec_002) has tenant_id AS
    # its PK, which the generic (pk_col, tenant_id) insert shape here does not fit;
    # its RLS enable/force + tenant_isolation policy are proven by
    # test_migration_applies.py against every expected table, so it needs no
    # separate behavioral case here.
]


@needs_qec_db
@pytest.mark.parametrize("table, pk_col, extra, reject", _QEC_TABLES,
                         ids=[t[0] for t in _QEC_TABLES])
def test_qecentral_table_isolates_tenants(table, pk_col, extra, reject):
    run(_run_qec_case(table, pk_col, extra, reject))


async def _run_qec_case(table, pk_col, extra, reject):
    engine = _new_engine(QEC_DB_URL)
    try:
        await _skip_unless_enforceable(engine)

        # Fill any None sentinel with a fresh id (app_cycles.app_id) so the
        # seed's active cycle never collides with another slot.
        seed_cols = {k: (_fresh("app") if v is None else v) for k, v in extra.items()}

        async def _build_extra(_conn, _tenant):  # static columns, no FK parents
            return dict(seed_cols)

        def _reject_overrides() -> dict:
            return {k: (_fresh("app") if v is None else v) for k, v in reject.items()}

        await _prove_isolation(
            engine, table=table, pk_col=pk_col,
            build_extra=_build_extra, reject_overrides=_reject_overrides,
        )
    finally:
        await engine.dispose()


# ── substrate table page_visits (nexus DB, qec_substrate role) ──────────────
@needs_substrate_db
def test_page_visits_isolates_tenants():
    """page_visits carries an FK to canonical_artifacts, so the seed builds the
    parent chain (tenants → canonical_artifacts) under GUC=A first.  The FK
    integrity check bypasses RLS, so the ONLY reason the cross-tenant insert can
    fail is the WITH CHECK policy — exactly what we assert."""
    run(_run_page_visits_case())


async def _run_page_visits_case():
    engine = _new_engine(SUBSTRATE_DB_URL)
    try:
        await _skip_unless_enforceable(engine)

        async def _build_extra(conn: AsyncConnection, tenant: str) -> dict:
            artifact_id = _fresh("art")
            # tenants has NO RLS (it is the tenant registry); insert directly.
            await conn.execute(
                text("INSERT INTO tenants (tenant_id, name, domain) "
                     "VALUES (:t, :n, :d)"),
                {"t": tenant, "n": "RLS Probe Tenant", "d": _fresh("rls") + ".test"},
            )
            # canonical_artifacts IS RLS-scoped — the surrounding GUC=A makes the
            # WITH CHECK pass; the FK to tenants sees the row we just inserted.
            await conn.execute(
                text("INSERT INTO canonical_artifacts "
                     "(artifact_id, tenant_id, session_id) VALUES (:a, :t, :s)"),
                {"a": artifact_id, "t": tenant, "s": _fresh("sess")},
            )
            return {"artifact_id": artifact_id, "sequence_index": 0}

        # The reject reuses the SAME artifact (FK stays valid) but a distinct
        # sequence_index so (artifact, sequence, extractor_version) does not
        # collide — leaving the WITH CHECK policy as the sole rejection reason.
        await _prove_isolation(
            engine, table="page_visits", pk_col="page_visit_id",
            build_extra=_build_extra, reject_overrides=lambda: {"sequence_index": 987654},
        )
    finally:
        await engine.dispose()
