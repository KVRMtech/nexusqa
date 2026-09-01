"""M0.x §13/§14 — tenant context is transaction-scoped and cannot leak through
a reused pooled connection.

Every other RLS test in this suite runs on a ``NullPool`` engine: one fresh
connection per checkout, discarded after. That is deliberate (it keeps the
per-test event loops independent) but it also means those tests CANNOT see the
one RLS bug that only exists in production — a tenant GUC that outlives its
transaction and is still set when the connection is handed to the next tenant.

Production runs a real pool (``app/db/_make_engine``: pool_size=10,
max_overflow=5). The isolation guarantee there rests entirely on one argument:
``set_config('nexus.current_tenant_id', :tid, true)`` — the trailing ``true`` is
``is_local``, which scopes the setting to the surrounding TRANSACTION. Flip it to
``false`` and the setting becomes session-scoped, survives ``COMMIT``, rides the
connection back into the pool, and the next tenant to check that connection out
inherits it. Nothing in the code review catches that; the tests here do.

So this module deliberately uses a **pool of exactly one connection**
(``pool_size=1, max_overflow=0``) and asserts, on the same physical backend
verified by ``pg_backend_pid()``:

  * tenant B, on the connection tenant A just used, sees ZERO of A's rows;
  * the GUC is EMPTY at the start of the next checkout — the fail-closed state,
    which under FORCE RLS means "see nothing", not "see everything";
  * the DATABASE's tenant context outranks any application-level ``WHERE
    tenant_id = ...`` filter — a session bound to B cannot read A's rows by
    simply asking for them;
  * and (the canary) a genuinely leaking ``is_local=false`` write IS detected by
    this harness, so a green here means the check has teeth.

Runs through :func:`app.db._tenant_scoped` itself — the real production context
manager, handed a locally-built factory — so this proves the shipped helper, not
a re-implementation of it.

Gated on ``QEC_TEST_QEC_DATABASE_URL`` connected as the least-privilege ``qec``
role; a superuser bypasses RLS and would pass every assertion vacuously.
"""
from __future__ import annotations

import uuid

import pytest
from _dbgate import (
    ENV_QEC_DB,
    QEC_DB_URL,
    db_gate,
    require_db,
    run,
    skip_unless_rls_enforceable,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

needs_qec_db = db_gate(
    QEC_DB_URL, ENV_QEC_DB,
    "the pooled-connection tenant-leak proof needs a qecentral DB at alembic head "
    "connected as the non-superuser `qec` role",
)

_GUC_SET = "SELECT set_config('nexus.current_tenant_id', :t, :local)"
_GUC_READ = "SELECT coalesce(current_setting('nexus.current_tenant_id', true), '')"

#: A tenant-scoped table with no FK parents and a simple two-column insert.
_TABLE = "client_apps"


def _fresh(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _single_connection_engine():
    """An engine whose pool holds EXACTLY ONE connection.

    ``pool_size=1, max_overflow=0`` guarantees that sequential checkouts get the
    SAME physical backend — which is precisely the condition under which a leaked
    session GUC becomes a cross-tenant read. The tests assert the reuse actually
    happened (via ``pg_backend_pid()``) rather than assuming it.
    """
    return create_async_engine(QEC_DB_URL, pool_size=1, max_overflow=0, pool_pre_ping=False)


async def _backend_pid(session) -> int:
    return int((await session.execute(text("SELECT pg_backend_pid()"))).scalar())


# ── the four proofs ─────────────────────────────────────────────────────────

@needs_qec_db
def test_pooled_connection_does_not_leak_tenant_context():
    """A → commit → connection returned → B checks out the SAME backend."""
    require_db(QEC_DB_URL, ENV_QEC_DB)
    run(_drive_pool_reuse())


async def _drive_pool_reuse():
    from app.db import _tenant_scoped  # the REAL production context manager

    engine = _single_connection_engine()
    await skip_unless_rls_enforceable(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    tenant_a, tenant_b = _fresh("t-a"), _fresh("t-b")
    app_a = _fresh("app")

    try:
        # ── Tenant A: write a row through the production tenant-scoped session.
        async with _tenant_scoped(factory, tenant_a) as session:
            pid_a = await _backend_pid(session)
            await session.execute(
                text(f"INSERT INTO {_TABLE} (app_id, tenant_id, name, base_url) "
                     f"VALUES (:a, :t, :n, :u)"),
                {"a": app_a, "t": tenant_a, "n": "pool probe", "u": "https://pool.test/"},
            )
            seen = (await session.execute(
                text(f"SELECT count(*) FROM {_TABLE} WHERE app_id = :a"), {"a": app_a},
            )).scalar()
        assert seen == 1, "tenant A cannot see its own row — RLS misconfigured"
        # The context manager committed and closed: the connection is now back in
        # the pool with whatever session state tenant A left on it.

        # ── Tenant B: next checkout. With pool_size=1 this is the same backend.
        async with _tenant_scoped(factory, tenant_b) as session:
            pid_b = await _backend_pid(session)
            by_pk = (await session.execute(
                text(f"SELECT count(*) FROM {_TABLE} WHERE app_id = :a"), {"a": app_a},
            )).scalar()
            by_tenant = (await session.execute(
                text(f"SELECT count(*) FROM {_TABLE} WHERE tenant_id = :t"), {"t": tenant_a},
            )).scalar()
            guc = (await session.execute(text(_GUC_READ))).scalar()

        assert pid_a == pid_b, (
            f"the pool did not reuse the connection (backend {pid_a} then {pid_b}) — "
            f"this test proves nothing unless the SAME backend is reused; the pool "
            f"configuration has drifted"
        )
        assert by_pk == 0, (
            "POOLED-CONNECTION LEAK: on the very connection tenant A just used, "
            "tenant B read tenant A's row by primary key"
        )
        assert by_tenant == 0, (
            "POOLED-CONNECTION LEAK: tenant B enumerated tenant A's rows on the "
            "reused connection"
        )
        assert guc == tenant_b, (
            f"the reused connection came back carrying GUC {guc!r} instead of "
            f"tenant B's own context"
        )
    finally:
        await _cleanup(engine, tenant_a, app_a)
        await engine.dispose()


@needs_qec_db
def test_tenant_guc_does_not_survive_its_transaction():
    """The ``is_local=true`` guarantee, asserted directly.

    A raw checkout that sets NO context must read the GUC as EMPTY — not as the
    previous tenant's id. Empty is the fail-closed state: under FORCE RLS the
    policy compares ``tenant_id = ''`` and matches nothing.
    """
    require_db(QEC_DB_URL, ENV_QEC_DB)
    run(_drive_guc_scope())


async def _drive_guc_scope():
    engine = _single_connection_engine()
    await skip_unless_rls_enforceable(engine)
    tenant_a = _fresh("t-a")
    try:
        async with engine.connect() as conn:
            async with conn.begin():
                await conn.execute(text(_GUC_SET), {"t": tenant_a, "local": True})
                inside = (await conn.execute(text(_GUC_READ))).scalar()
            assert inside == tenant_a, "the GUC was not set inside its own transaction"
            pid_first = int((await conn.execute(text("SELECT pg_backend_pid()"))).scalar())

        # New checkout of the same (pooled) backend, no context established.
        async with engine.connect() as conn:
            pid_second = int((await conn.execute(text("SELECT pg_backend_pid()"))).scalar())
            after = (await conn.execute(text(_GUC_READ))).scalar()
            visible = (await conn.execute(
                text(f"SELECT count(*) FROM {_TABLE} WHERE tenant_id = :t"), {"t": tenant_a},
            )).scalar()

        assert pid_first == pid_second, "pool did not reuse the backend — test is vacuous"
        assert after == "", (
            f"TENANT CONTEXT LEAKED PAST COMMIT: a context-less checkout of the "
            f"reused backend reads nexus.current_tenant_id = {after!r}. The "
            f"set_config is_local flag is wrong somewhere."
        )
        assert visible == 0, (
            "a context-less session is not fail-closed — it can read tenant rows"
        )
    finally:
        await engine.dispose()


@needs_qec_db
def test_application_level_tenant_id_cannot_override_the_database_context():
    """M0.x §13 — the DB is authoritative, not the app's WHERE clause.

    An application bug (or a compromised handler) that asks for tenant A's rows
    while its session is bound to tenant B must get nothing. If this ever
    returned rows it would mean isolation depends on application filtering, and
    the database is not enforcing anything.
    """
    require_db(QEC_DB_URL, ENV_QEC_DB)
    run(_drive_app_override())


async def _drive_app_override():
    from app.db import _tenant_scoped

    engine = _single_connection_engine()
    await skip_unless_rls_enforceable(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant_a, tenant_b = _fresh("t-a"), _fresh("t-b")
    app_a = _fresh("app")

    try:
        async with _tenant_scoped(factory, tenant_a) as session:
            await session.execute(
                text(f"INSERT INTO {_TABLE} (app_id, tenant_id, name, base_url) "
                     f"VALUES (:a, :t, :n, :u)"),
                {"a": app_a, "t": tenant_a, "n": "override probe", "u": "https://ov.test/"},
            )

        # Session bound to B, but the query explicitly asks for A — the shape a
        # broken handler produces when it trusts a request-supplied tenant_id.
        async with _tenant_scoped(factory, tenant_b) as session:
            rows = (await session.execute(
                text(f"SELECT count(*) FROM {_TABLE} WHERE tenant_id = :asked"),
                {"asked": tenant_a},
            )).scalar()
            # And the same via the row's own primary key, bypassing any tenant
            # predicate entirely.
            by_pk = (await session.execute(
                text(f"SELECT count(*) FROM {_TABLE} WHERE app_id = :a"), {"a": app_a},
            )).scalar()

        assert rows == 0 and by_pk == 0, (
            f"an application-level tenant_id overrode the database context "
            f"(by_tenant={rows}, by_pk={by_pk}) — isolation is being provided by "
            f"the WHERE clause, not by RLS"
        )
    finally:
        await _cleanup(engine, tenant_a, app_a)
        await engine.dispose()


@needs_qec_db
def test_the_leak_detector_actually_detects_a_leak():
    """CANARY — deliberately leak a session-scoped GUC and prove we catch it.

    Every assertion above is "nothing leaked". That reads green both when the
    pooling is safe and when the harness is broken (wrong engine, no reuse, a
    connection silently recycled between checkouts). Here we write the GUC with
    ``is_local = false`` — the exact one-character mistake this module exists to
    catch — and assert it DOES survive into the next checkout of the same
    backend. If this test stops failing to leak, the other three are worthless.
    """
    require_db(QEC_DB_URL, ENV_QEC_DB)
    run(_drive_canary())


async def _drive_canary():
    engine = _single_connection_engine()
    leaked_tenant = _fresh("t-leak")
    try:
        async with engine.connect() as conn:
            async with conn.begin():
                # is_local = FALSE — session-scoped, survives COMMIT.
                await conn.execute(text(_GUC_SET), {"t": leaked_tenant, "local": False})
            pid_first = int((await conn.execute(text("SELECT pg_backend_pid()"))).scalar())

        async with engine.connect() as conn:
            pid_second = int((await conn.execute(text("SELECT pg_backend_pid()"))).scalar())
            observed = (await conn.execute(text(_GUC_READ))).scalar()

        assert pid_first == pid_second, (
            "the pool handed out a different backend, so this canary could not "
            "have observed a leak either way — the pooling assumptions in this "
            "module no longer hold and the sibling tests are vacuous"
        )
        assert observed == leaked_tenant, (
            "an is_local=false GUC did NOT survive into the next checkout of the "
            "same backend. That means this harness cannot observe a leak at all, "
            "so the passing leak tests above prove nothing."
        )
    finally:
        await engine.dispose()


async def _cleanup(engine, tenant: str, app_id: str) -> None:
    """Best-effort removal of the probe row (disposable DB, but keep it tidy)."""
    try:
        async with engine.connect() as conn:
            async with conn.begin():
                await conn.execute(text(_GUC_SET), {"t": tenant, "local": True})
                await conn.execute(
                    text(f"DELETE FROM {_TABLE} WHERE app_id = :a"), {"a": app_id},
                )
    except Exception:  # pragma: no cover — cleanup must never mask a real result
        pass


if __name__ == "__main__":  # pragma: no cover — ad-hoc local run
    pytest.main([__file__, "-v"])
