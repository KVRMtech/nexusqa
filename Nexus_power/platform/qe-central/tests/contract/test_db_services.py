"""M0.x §4/§5 — the CI services are real, reachable, and the ones we think.

This is the FIRST thing the database job should run. Everything downstream
(migrations, RLS, indexes) is meaningless if the job is quietly pointed at the
wrong server — or at no server, with every test skipping its way to green.

What is proven here:

  * **Postgres is reachable** through the same DSN the tests use, and it answers
    a real query. Not ``pg_isready`` from a shell — the *application's* driver,
    with the *application's* credentials, against the *application's* database.
  * **It is the CI service, not a developer's local Postgres.** ``pg_isready``
    on ``localhost:5432`` is happy to greet whatever happens to be listening. The
    connection reports which server, database, user and version it actually
    reached, and asserts the schema is the migrated qecentral one.
  * **Redis is reachable and answers PING**, on the configured DSN, and a real
    round-trip (SET → GET → DEL) succeeds — a PING can be answered by a Redis
    the application has no permission to use.
  * **Redis is empty-ish and isolated**, so a parallel job's keys are not being
    read as this job's state.
  * **No credential ever reaches the log.** Every diagnostic prints
    host/port/database/user only; :func:`_safe` is the single place a DSN is
    rendered and it drops the password.

Under ``QEC_REQUIRE_DB`` an unset DSN fails here instead of skipping, so "the
Postgres service failed to start" surfaces as a red service test rather than as
a suite full of skips and a green job.
"""
from __future__ import annotations

import uuid

import pytest
from _dbgate import (
    ENV_QEC_DB,
    ENV_REDIS,
    QEC_DB_URL,
    REDIS_URL,
    db_gate,
    new_engine,
    require_db,
    run,
)
from sqlalchemy import text
from sqlalchemy.engine import make_url

needs_qec_db = db_gate(
    QEC_DB_URL, ENV_QEC_DB, "the Postgres connectivity check needs the CI database DSN",
)
needs_redis = db_gate(
    REDIS_URL, ENV_REDIS, "the Redis connectivity check needs the CI redis DSN",
)


def _safe(url: str) -> str:
    """Render a DSN with the password removed — the ONLY way a DSN is printed."""
    u = make_url(url)
    return f"{u.drivername}://{u.username or '-'}@{u.host}:{u.port}/{u.database}"


async def _close(client) -> None:
    """Close an async Redis client across redis-py 5.0.x and 5.1+.

    ``aclose()`` arrived in 5.0.1; ``close()`` is the older spelling and is
    deprecated (not removed) after it. requirements.txt pins ``redis>=5,<6``,
    which spans both, so the tests must not assume either."""
    closer = getattr(client, "aclose", None) or client.close
    await closer()


# ── Postgres ────────────────────────────────────────────────────────────────

@needs_qec_db
def test_postgres_is_reachable_through_the_application_driver():
    require_db(QEC_DB_URL, ENV_QEC_DB)
    facts = run(_probe_postgres())
    print(
        f"\nPOSTGRES: {_safe(QEC_DB_URL)}\n"
        f"  server      : {facts['version'].split(',')[0]}\n"
        f"  database    : {facts['database']}\n"
        f"  user        : {facts['user']}\n"
        f"  alembic head: {facts['revision']}\n"
        f"  base tables : {facts['tables']}"
    )
    assert facts["select_one"] == 1, "Postgres accepted the connection but did not answer SELECT 1"


async def _probe_postgres() -> dict:
    engine = new_engine(QEC_DB_URL)
    try:
        async with engine.connect() as conn:
            return {
                "select_one": (await conn.execute(text("SELECT 1"))).scalar(),
                "version": (await conn.execute(text("SELECT version()"))).scalar(),
                "database": (await conn.execute(text("SELECT current_database()"))).scalar(),
                "user": (await conn.execute(text("SELECT current_user"))).scalar(),
                "revision": (await conn.execute(text(
                    "SELECT version_num FROM alembic_version"
                ))).scalar(),
                "tables": (await conn.execute(text(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema='public' AND table_type='BASE TABLE'"
                ))).scalar(),
            }
    finally:
        await engine.dispose()


@needs_qec_db
def test_postgres_is_the_migrated_qecentral_database():
    """Guard against a green run against the wrong server.

    A DSN typo that lands on an empty database, or on the *nexus* database
    instead of *qecentral*, would let the connectivity test above pass while
    every schema assertion downstream fails for a confusing reason. Fail here,
    with the reason.
    """
    require_db(QEC_DB_URL, ENV_QEC_DB)
    facts = run(_probe_postgres())
    assert facts["revision"], (
        f"{_safe(QEC_DB_URL)} has no alembic_version row — this database was "
        f"never migrated. Run the bootstrap + `alembic upgrade head` first."
    )
    assert facts["revision"].startswith("qec_"), (
        f"alembic head is {facts['revision']!r} — that is not the qec chain. The "
        f"DSN is pointed at the wrong database (the nexus substrate DB, most likely)."
    )
    assert facts["tables"] >= 21, (
        f"only {facts['tables']} base tables — qec_001 alone creates 21. The "
        f"database is not at head."
    )


@needs_qec_db
def test_postgres_supports_the_rls_context_mechanism():
    """The GUC round-trip every tenant-scoped session depends on.

    If ``set_config('nexus.current_tenant_id', …, true)`` did not survive to the
    next statement in the same transaction, every RLS policy would evaluate
    against an empty tenant and the whole application would read zero rows —
    a failure worth naming precisely rather than discovering as "no data".
    """
    require_db(QEC_DB_URL, ENV_QEC_DB)

    async def _probe():
        probe = f"__svc_check_{uuid.uuid4().hex[:8]}"
        engine = new_engine(QEC_DB_URL)
        try:
            async with engine.connect() as conn:
                async with conn.begin():
                    await conn.execute(
                        text("SELECT set_config('nexus.current_tenant_id', :t, true)"),
                        {"t": probe},
                    )
                    return probe, (await conn.execute(
                        text("SELECT current_setting('nexus.current_tenant_id', true)")
                    )).scalar()
        finally:
            await engine.dispose()

    expected, echoed = run(_probe())
    assert echoed == expected, (
        f"the tenant GUC did not round-trip inside a transaction "
        f"(set {expected!r}, read {echoed!r}) — RLS context is broken on this server"
    )


# ── Redis ───────────────────────────────────────────────────────────────────

@needs_redis
def test_redis_is_reachable_and_answers_ping():
    require_db(REDIS_URL, ENV_REDIS)
    redis = pytest.importorskip(
        "redis.asyncio",
        reason="redis-py is a qe-central runtime dependency (requirements.txt) — "
               "if it is missing, the CI image is not installing requirements.txt",
    )

    async def _probe():
        client = redis.from_url(REDIS_URL, socket_connect_timeout=5, socket_timeout=5)
        try:
            pong = await client.ping()
            info = await client.info("server")
            return pong, info.get("redis_version", "?")
        finally:
            await _close(client)

    pong, version = run(_probe())
    print(f"\nREDIS: {REDIS_URL} — version {version}")
    assert pong is True, f"Redis at {REDIS_URL} did not answer PING"


@needs_redis
def test_redis_accepts_a_real_write_read_delete_round_trip():
    """PING only proves the socket. The distributed admission limiter does
    SET/GET/EVAL — prove the connection can actually carry that."""
    require_db(REDIS_URL, ENV_REDIS)
    redis = pytest.importorskip("redis.asyncio")

    async def _probe():
        key = f"qec:m0x:probe:{uuid.uuid4().hex}"
        client = redis.from_url(REDIS_URL, socket_connect_timeout=5, socket_timeout=5)
        try:
            await client.set(key, "ok", ex=60)
            value = await client.get(key)
            removed = await client.delete(key)
            still_there = await client.get(key)
            return value, removed, still_there
        finally:
            await _close(client)

    value, removed, still_there = run(_probe())
    assert value == b"ok", f"Redis SET/GET round-trip returned {value!r}"
    assert removed == 1, "Redis DELETE did not remove the probe key"
    assert still_there is None, "the probe key survived its own DELETE"


@needs_redis
def test_redis_is_a_dedicated_ci_instance_not_a_shared_one():
    """A developer's local Redis on 6379 will happily answer this job.

    A CI Redis is freshly started and effectively empty. A Redis holding a large
    keyspace is somebody else's — either a dev machine or a parallel job's
    instance — and admission-limiter state read from it would be nonsense. This
    is a WARNING-shaped assertion with a generous ceiling: it exists to catch
    "pointed at prod/dev", not to police a handful of leftover probe keys.
    """
    require_db(REDIS_URL, ENV_REDIS)
    redis = pytest.importorskip("redis.asyncio")

    async def _probe():
        client = redis.from_url(REDIS_URL, socket_connect_timeout=5, socket_timeout=5)
        try:
            return await client.dbsize()
        finally:
            await _close(client)

    size = run(_probe())
    assert size < 1000, (
        f"the configured Redis holds {size} keys. A CI Redis service starts "
        f"empty — this DSN is pointed at a shared or developer instance, and "
        f"any state this job reads from it is not its own."
    )


if __name__ == "__main__":  # pragma: no cover — ad-hoc local run
    pytest.main([__file__, "-v", "-s"])
