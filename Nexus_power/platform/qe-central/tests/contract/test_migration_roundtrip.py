"""M0.x T-DB-04 — every migration survives ``upgrade → downgrade → upgrade``.

    clean database → alembic upgrade head → verify schema
                   → alembic downgrade     → verify the objects are GONE
                   → alembic upgrade head → verify the schema is IDENTICAL

"Identical" is not a hand-written checklist here. Each verify step takes a
**schema fingerprint** — every table, every column with its type/nullability/
default, every index with its full ``indexdef``, every constraint, every RLS
policy with its ``USING``/``WITH CHECK`` expressions, and the RLS enable/force
flags — and the post-round-trip fingerprint must equal the pre-round-trip one
*exactly*. A downgrade that forgets to drop an index, or an upgrade that
re-creates a policy with a subtly different expression, is a diff in that
fingerprint and fails here. A checklist can only catch what someone remembered
to list; a fingerprint catches what nobody thought of.

Two depths, because they fail differently:

  * :func:`test_head_revision_round_trips` — head → head-1 → head. This is the
    gate a NEW migration must clear, and it is the one that would have caught a
    ``downgrade()`` that was written as ``pass``.
  * :func:`test_full_chain_round_trips_to_base` — head → base → head. Proves the
    WHOLE chain is reversible and, at base, that the database is genuinely empty
    (only ``alembic_version`` survives — no orphaned index, no orphaned policy,
    no table left behind by a partial ``drop_table`` list).

Each test builds its OWN throwaway database via the admin DSN, so nothing here
can disturb the shared CI database the other contract tests are asserting
against — and two of these can never race each other over one schema.

Runs the real ``python -m alembic ... upgrade/downgrade`` command line, not the
programmatic API, so what CI proves is the command an operator actually types.

Gated on ``QEC_TEST_ADMIN_DATABASE_URL`` — a SUPERUSER DSN on the CI Postgres,
used only to CREATE/DROP the throwaway databases. It is deliberately NOT used for
any RLS assertion (a superuser bypasses RLS); this module asserts structure only.
"""
from __future__ import annotations

import os
import subprocess
import sys
import uuid

import pytest
from _dbgate import ADMIN_DB_URL, ENV_ADMIN_DB, db_gate, require_db, run
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

needs_admin_db = db_gate(
    ADMIN_DB_URL, ENV_ADMIN_DB,
    "the migration round-trip needs a superuser DSN to create throwaway databases",
)

_HERE = os.path.dirname(os.path.abspath(__file__))
_SERVICE_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
_ALEMBIC_INI = os.path.join(_SERVICE_ROOT, "alembic_qec", "alembic.ini")


# ── running the real alembic CLI ────────────────────────────────────────────

def _alembic(db_url: str, *args: str) -> str:
    """Invoke ``python -m alembic -c alembic_qec/alembic.ini <args>``.

    Fails the test with alembic's own stdout+stderr on a non-zero exit — a
    migration error message is the single most useful artifact when this gate
    goes red, so it is surfaced verbatim rather than summarised.
    """
    env = {**os.environ, "QEC_DATABASE_URL": db_url}
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", _ALEMBIC_INI, *args],
        cwd=_SERVICE_ROOT, env=env, capture_output=True, text=True,
    )
    assert proc.returncode == 0, (
        f"`alembic {' '.join(args)}` FAILED (exit {proc.returncode}).\n"
        f"── stdout ──\n{proc.stdout}\n── stderr ──\n{proc.stderr}"
    )
    return proc.stdout + proc.stderr


def _chain_head() -> str:
    """The head revision, read from the migration chain itself.

    Never hardcoded: a pinned expectation is how the pre-M0.x
    ``test_migration_applies.py`` came to assert ``qec_003`` thirteen revisions
    after qec_003 stopped being head.
    """
    out = _alembic("postgresql+asyncpg://unused/unused", "heads")
    for line in out.splitlines():
        token = line.strip().split(" ")[0]
        if token.startswith("qec_"):
            return token
    raise AssertionError(f"could not derive the chain head from `alembic heads`:\n{out}")


# ── throwaway database lifecycle ────────────────────────────────────────────

async def _admin_exec(sql: str) -> None:
    """Run one statement on the admin DSN in AUTOCOMMIT (CREATE/DROP DATABASE
    cannot run inside a transaction block)."""
    engine = create_async_engine(ADMIN_DB_URL, poolclass=NullPool, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            await conn.execute(text(sql))
    finally:
        await engine.dispose()


def _child_url(db_name: str) -> str:
    # render_as_string(hide_password=False), NOT str(): SQLAlchemy's URL.__str__
    # renders the password as '***', which produces a DSN that authenticates as
    # literally "***" and fails with a password error that looks like a CI
    # credentials problem. The value is only ever handed to a subprocess env var
    # — it is never logged (see _alembic, which surfaces alembic's output but
    # never the DSN).
    return make_url(ADMIN_DB_URL).set(database=db_name).render_as_string(hide_password=False)


async def _create_scratch_db() -> tuple[str, str]:
    name = f"qec_rt_{uuid.uuid4().hex[:10]}"
    await _admin_exec(f'CREATE DATABASE "{name}"')
    return name, _child_url(name)


async def _drop_scratch_db(name: str) -> None:
    # FORCE (PG13+) evicts any connection alembic's engine failed to close, so a
    # failing test never leaves an undroppable database behind in CI.
    await _admin_exec(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


# ── the schema fingerprint ──────────────────────────────────────────────────

_FINGERPRINT_SQL = {
    "tables": (
        "SELECT c.relname || '|rls=' || c.relrowsecurity || '|force=' || c.relforcerowsecurity "
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relkind = 'r' ORDER BY 1"
    ),
    "columns": (
        "SELECT table_name || '.' || column_name || '|' || data_type || '|' || "
        "       is_nullable || '|' || coalesce(column_default, '-') "
        "FROM information_schema.columns WHERE table_schema = 'public' ORDER BY 1"
    ),
    "indexes": (
        "SELECT indexname || '|' || indexdef FROM pg_indexes "
        "WHERE schemaname = 'public' ORDER BY 1"
    ),
    "constraints": (
        "SELECT con.conname || '|' || con.contype::text || '|' || "
        "       pg_get_constraintdef(con.oid) "
        "FROM pg_constraint con JOIN pg_namespace n ON n.oid = con.connamespace "
        "WHERE n.nspname = 'public' ORDER BY 1"
    ),
    "policies": (
        "SELECT tablename || '.' || policyname || '|' || cmd || '|' || permissive || "
        "       '|USING=' || coalesce(qual, '-') || '|CHECK=' || coalesce(with_check, '-') "
        "FROM pg_policies WHERE schemaname = 'public' ORDER BY 1"
    ),
}


async def _fingerprint(db_url: str) -> dict[str, list[str]]:
    engine = create_async_engine(db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            return {
                key: list((await conn.execute(text(sql))).scalars().all())
                for key, sql in _FINGERPRINT_SQL.items()
            }
    finally:
        await engine.dispose()


def _diff(before: dict, after: dict) -> dict:
    """Human-readable set difference per fingerprint section (empty == identical)."""
    out = {}
    for key in _FINGERPRINT_SQL:
        gone = sorted(set(before[key]) - set(after[key]))
        extra = sorted(set(after[key]) - set(before[key]))
        if gone or extra:
            out[key] = {"missing_after": gone, "unexpected_after": extra}
    return out


# ── T-DB-04 ─────────────────────────────────────────────────────────────────

@needs_admin_db
def test_head_revision_round_trips():
    """head → head-1 → head. The gate every NEW migration must clear."""
    require_db(ADMIN_DB_URL, ENV_ADMIN_DB)
    run(_drive_head_round_trip())


async def _drive_head_round_trip():
    head = _chain_head()
    name, url = await _create_scratch_db()
    try:
        # ── 1. clean database → upgrade head → verify ───────────────────────
        _alembic(url, "upgrade", "head")
        at_head = await _fingerprint(url)
        assert await _current_revision(url) == head
        assert at_head["tables"], "upgrade head produced no tables"

        # The T-DB-02 index must be present at head — this is the object the
        # round-trip is really exercising.
        assert any("ix_qe_explorations_status_updated" in i for i in at_head["indexes"]), (
            "the qec_017 reaper index is absent at head"
        )

        # ── 2. downgrade one revision → verify the objects are GONE ─────────
        _alembic(url, "downgrade", "-1")
        after_down = await _fingerprint(url)
        assert not any("ix_qe_explorations_status_updated" in i for i in after_down["indexes"]), (
            "downgrade -1 left the qec_017 index behind — an orphaned index is "
            "exactly the half-applied schema this gate exists to catch"
        )
        # …and nothing ELSE moved. A downgrade that drops unrelated objects is
        # as broken as one that drops nothing.
        removed = set(at_head["indexes"]) - set(after_down["indexes"])
        assert len(removed) == 1, (
            f"downgrade -1 removed {len(removed)} indexes, expected exactly the "
            f"one qec_017 owns: {sorted(removed)}"
        )
        for section in ("tables", "columns", "constraints", "policies"):
            assert at_head[section] == after_down[section], (
                f"downgrade -1 modified '{section}', which qec_017 does not own:\n"
                f"{_diff({section: at_head[section]}, {section: after_down[section]})}"
            )

        # ── 3. upgrade head again → verify the schema is IDENTICAL ──────────
        _alembic(url, "upgrade", "head")
        back_at_head = await _fingerprint(url)
        assert await _current_revision(url) == head
        diff = _diff(at_head, back_at_head)
        assert not diff, (
            f"schema is NOT identical after upgrade → downgrade → upgrade:\n{diff}"
        )
    finally:
        await _drop_scratch_db(name)


@needs_admin_db
def test_full_chain_round_trips_to_base():
    """head → base → head. Proves the entire chain is reversible and leaves
    nothing behind at base."""
    require_db(ADMIN_DB_URL, ENV_ADMIN_DB)
    run(_drive_full_round_trip())


async def _drive_full_round_trip():
    head = _chain_head()
    name, url = await _create_scratch_db()
    try:
        _alembic(url, "upgrade", "head")
        at_head = await _fingerprint(url)

        _alembic(url, "downgrade", "base")
        at_base = await _fingerprint(url)

        # Only alembic's own bookkeeping may survive a downgrade to base.
        leftover_tables = [t for t in at_base["tables"] if not t.startswith("alembic_version|")]
        leftover_indexes = [i for i in at_base["indexes"] if not i.startswith("alembic_version_pkc|")]
        assert not leftover_tables, (
            f"downgrade to base left orphaned TABLES: {leftover_tables}"
        )
        assert not leftover_indexes, (
            f"downgrade to base left orphaned INDEXES: {leftover_indexes}"
        )
        assert not at_base["policies"], (
            f"downgrade to base left orphaned RLS POLICIES: {at_base['policies']} — "
            f"a policy outliving its table is impossible, so this means a table "
            f"outlived its drop_table"
        )

        _alembic(url, "upgrade", "head")
        back_at_head = await _fingerprint(url)
        assert await _current_revision(url) == head
        diff = _diff(at_head, back_at_head)
        assert not diff, (
            f"the full chain is not reproducible — rebuilding from base produced "
            f"a DIFFERENT schema than the first upgrade:\n{diff}"
        )
    finally:
        await _drop_scratch_db(name)


@needs_admin_db
def test_every_revision_declares_a_downgrade():
    """A static guard so a new migration cannot ship with a stub ``downgrade``.

    ``test_head_revision_round_trips`` only exercises the HEAD revision. This
    reads every file in ``alembic_qec/versions`` and rejects a ``downgrade()``
    whose body is ``pass`` / ``...`` / ``raise NotImplementedError`` — the three
    ways a chain silently becomes one-way.
    """
    import ast
    import pathlib

    versions = pathlib.Path(_SERVICE_ROOT) / "alembic_qec" / "versions"
    files = sorted(p for p in versions.glob("qec_*.py"))
    assert files, f"no migration files found under {versions}"

    offenders = []
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        fn = next(
            (n for n in tree.body
             if isinstance(n, ast.FunctionDef) and n.name == "downgrade"), None,
        )
        if fn is None:
            offenders.append(f"{path.name}: no downgrade() at all")
            continue
        body = [n for n in fn.body if not (isinstance(n, ast.Expr)
                                           and isinstance(n.value, ast.Constant)
                                           and isinstance(n.value.value, str))]
        if not body or all(isinstance(n, ast.Pass) for n in body):
            offenders.append(f"{path.name}: downgrade() body is empty / `pass`")
        elif any(isinstance(n, ast.Raise) for n in body):
            offenders.append(f"{path.name}: downgrade() raises — the chain is one-way")
    assert not offenders, (
        "Migration(s) without a real downgrade — the round-trip contract cannot "
        "hold for these:\n  " + "\n  ".join(offenders)
    )


async def _current_revision(db_url: str) -> str:
    engine = create_async_engine(db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            return (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar()
    finally:
        await engine.dispose()


if __name__ == "__main__":  # pragma: no cover — ad-hoc local run
    pytest.main([__file__, "-v"])
