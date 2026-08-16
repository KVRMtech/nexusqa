"""M0.x — the shared database gate for every DB-backed contract test.

THE PATTERN THIS EXISTS TO KILL::

    database unavailable  →  tests skipped  →  CI green

A skip is the correct answer on a laptop with no Postgres. It is the WRONG answer
in CI, where the whole point of the job is that the database guarantees hold. So
the gate is two-state:

  * ``QEC_REQUIRE_DB`` unset (laptop) — a missing DSN SKIPS, with the env var
    named in the reason so the developer knows what to set.
  * ``QEC_REQUIRE_DB=1`` (CI) — a missing DSN does NOT skip. The test runs and
    :func:`require_db` fails it with a message naming the variable. And
    ``tests/conftest.py`` additionally fails the whole session if ANY db-gated
    test skipped, so a silently-unwired DSN can never be mistaken for a pass.

Usage in a test module::

    from _dbgate import QEC_DB_URL, db_gate, require_db

    needs_qec_db = db_gate(QEC_DB_URL, "QEC_TEST_QEC_DATABASE_URL",
                           "the RLS coverage gate needs a migrated qecentral DB")

    @needs_qec_db
    def test_something():
        require_db(QEC_DB_URL, "QEC_TEST_QEC_DATABASE_URL")
        ...

The two calls are deliberate: the mark handles the laptop skip, the ``require_db``
call inside the body is what turns a missing DSN into a red test under
``QEC_REQUIRE_DB``.
"""
from __future__ import annotations

import asyncio
import os

import pytest

# ── The four DSNs the DB-backed suite consumes ──────────────────────────────
#: The qecentral DB at alembic head, connected as the least-privilege ``qec``
#: role (a superuser bypasses RLS, so RLS proofs through one prove nothing).
ENV_QEC_DB = "QEC_TEST_QEC_DATABASE_URL"
#: The nexus substrate DB as the least-privilege ``qec_substrate`` role.
ENV_SUBSTRATE_DB = "QEC_TEST_SUBSTRATE_DATABASE_URL"
#: A SUPERUSER DSN on the same server, used only to CREATE/DROP the throwaway
#: databases the migration round-trip needs. Never used for an RLS assertion.
ENV_ADMIN_DB = "QEC_TEST_ADMIN_DATABASE_URL"
#: The CI Redis instance (``redis://host:port/db``).
ENV_REDIS = "QEC_TEST_REDIS_URL"

#: Every DSN variable the gate knows about. ``tests/conftest.py`` uses this list
#: to recognise a db-gated skip in the terminal report.
DB_ENV_VARS: tuple[str, ...] = (
    ENV_QEC_DB,
    ENV_SUBSTRATE_DB,
    ENV_ADMIN_DB,
    ENV_REDIS,
    # The legacy variable the pre-M0.x DB-gated tests already use.
    "QEC_TEST_DATABASE_URL",
)

QEC_DB_URL = os.environ.get(ENV_QEC_DB, "")
SUBSTRATE_DB_URL = os.environ.get(ENV_SUBSTRATE_DB, "")
ADMIN_DB_URL = os.environ.get(ENV_ADMIN_DB, "")
REDIS_URL = os.environ.get(ENV_REDIS, "")


def db_required() -> bool:
    """True when CI has declared the database services MANDATORY."""
    return os.environ.get("QEC_REQUIRE_DB", "").strip().lower() in ("1", "true", "yes", "on")


def db_gate(url: str, env_name: str, purpose: str):
    """A skipif mark that STOPS being a skip once ``QEC_REQUIRE_DB`` is set.

    With the DSN present the mark never skips. With it absent the mark skips on a
    laptop and does NOT skip in CI — there the test body's :func:`require_db`
    turns the absence into a failure that names the variable.
    """
    return pytest.mark.skipif(
        not url and not db_required(),
        reason=f"{env_name} not set — {purpose}",
    )


def require_db(url: str, env_name: str) -> str:
    """Assert the DSN is present; return it. The CI-side half of :func:`db_gate`."""
    assert url, (
        f"{env_name} is not set, but QEC_REQUIRE_DB declares the database "
        f"services MANDATORY for this run. A database-gated test that cannot "
        f"execute is a CI FAILURE, never a silent skip — wire {env_name} to the "
        f"CI Postgres/Redis service or unset QEC_REQUIRE_DB."
    )
    return url


def run(coro):
    """Drive one coroutine in its own event loop.

    Every DB contract test uses this rather than pytest-asyncio so each test owns
    its loop; combined with ``NullPool`` engines no connection can ever outlive
    the loop it was opened on (the ``Future attached to a different loop`` class
    of flake).
    """
    return asyncio.run(coro)


def new_engine(url: str):
    """A NullPool async engine — one fresh connection per checkout, no reuse
    across the per-test event loops."""
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    return create_async_engine(url, poolclass=NullPool)


async def skip_unless_rls_enforceable(engine) -> None:
    """Refuse to assert isolation through a connection RLS does not apply to.

    PostgreSQL silently exempts superusers and ``BYPASSRLS`` roles from every
    policy. An isolation test run through such a role passes whether or not the
    policy exists — the definition of a false green. Detect and skip instead.

    Under ``QEC_REQUIRE_DB`` this is a FAILURE, not a skip: CI wiring the RLS
    proof to a superuser DSN would silently disarm the whole gate.
    """
    from sqlalchemy import text

    async with engine.connect() as conn:
        user = (await conn.execute(text("SELECT current_user"))).scalar()
        is_super = (await conn.execute(text("SELECT current_setting('is_superuser')"))).scalar()
        bypass = (await conn.execute(
            text("SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user")
        )).scalar()
    if str(is_super).lower() != "on" and bypass is not True:
        return

    message = (
        f"connected as '{user}', which BYPASSES RLS (superuser/BYPASSRLS) — an "
        f"isolation assertion through this role would pass with the policies "
        f"DELETED. Point the DSN at the least-privilege qec / qec_substrate role."
    )
    if db_required():
        raise AssertionError(
            "QEC_REQUIRE_DB is set and the RLS proof is wired to an RLS-exempt "
            "role, which would make the tenant-isolation gate vacuous. " + message
        )
    pytest.skip(message)
