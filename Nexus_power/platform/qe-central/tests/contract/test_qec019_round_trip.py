"""GATE 3 / A20 — ``qec_019`` survives the full round trip, in the CI database.

WHY THIS EXISTS SEPARATELY FROM ``test_migration_roundtrip.py``
==============================================================
That module proves two generic properties of the chain: the CURRENT head steps
back and forward without drift, and the WHOLE chain unwinds to base leaving
nothing behind. Neither is a statement about ``qec_019``.

  * ``test_head_revision_round_trips`` only ever exercises head. ``qec_019``
    stopped being head the moment ``qec_020`` landed, and has not been
    round-tripped by that test since.
  * ``test_full_chain_round_trips_to_base`` walks THROUGH ``qec_019`` in both
    directions, but its assertions are about the terminal states (nothing
    orphaned at base, identical at head). A revision can be crossed in a chain
    walk and still be wrong in isolation — the classic case is a downgrade that
    only appears to work because a LATER revision's downgrade, running first,
    already dropped the table its columns hang off.

``qec_019`` is exactly that shape: five ADDITIVE COLUMNS on ``catalog_questions``,
a table created back in ``qec_012``. Dropping the table drops the columns, so a
chain walk to base cannot distinguish a correct ``qec_019.downgrade()`` from an
empty one. This module pins it directly, at its own revision, with the table
still standing.

WHAT "VALIDATE" MEANS HERE — SCHEMA **AND** DATA
================================================
A20 asks for validation after each leg, and a schema fingerprint alone is not
enough for this migration. ``qec_019`` adds two columns with SERVER defaults
precisely so that existing rows are not rewritten by an UPDATE (see the
migration's own note: the table can hold a question per control per application
per tenant). The claim "existing rows keep their values and read back exactly as
before" is a claim about DATA, and it is only tested by having a row in the table
before the migration runs.

So a real row is seeded at ``qec_018`` and is asserted, at every leg, to still
carry the values it was written with. A downgrade that dropped and recreated the
table would satisfy every structural assertion in this file and destroy that row.

THE ROUND TRIP, AS A20 SPECIFIES IT
===================================
    qec_018 (before state)  →  seed a row
        ↓  qec_019 UP
    validate    (columns exist, types/defaults/nullability, row intact,
                 defaults supplied without a rewrite, RLS untouched)
        ↓  qec_019 DOWN
    validate restoration  (columns gone, fingerprint == before state EXACTLY,
                           row still intact)
        ↓  qec_019 UP
    final validation  (fingerprint == first-UP state EXACTLY, row still intact)

Runs the real ``python -m alembic`` command line against a throwaway database on
the CI Postgres — the same instrument, the same scratch-DB lifecycle and the same
fingerprint as ``test_migration_roundtrip.py``, imported rather than re-written
so the two can never drift into asking different questions.
"""
from __future__ import annotations

import pytest
from _dbgate import ADMIN_DB_URL, ENV_ADMIN_DB, db_gate, require_db, run
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

# The scratch-database lifecycle, the real-CLI runner and the schema fingerprint
# are the round-trip module's, deliberately: a second copy would be a second
# definition of "identical schema" and the two would drift.
from test_migration_roundtrip import (  # noqa: E402
    _alembic,
    _create_scratch_db,
    _current_revision,
    _diff,
    _drop_scratch_db,
    _fingerprint,
)

needs_admin_db = db_gate(
    ADMIN_DB_URL, ENV_ADMIN_DB,
    "the qec_019 round trip needs a superuser DSN to create a throwaway database",
)

#: The revision under test, and the one immediately before it. Both named as
#: constants so a future re-parenting of the chain shows up as one edit.
REVISION = "qec_019"
PRIOR = "qec_018"

TABLE = "catalog_questions"

#: The five columns qec_019 adds, with what the DATABASE must say about each
#: after the upgrade: (column, data_type, is_nullable, column_default fragment).
#:
#: The default fragments are substring matches, not equality: PostgreSQL renders
#: a varchar default as ``'UNVERIFIED'::character varying`` and an integer one as
#: plain ``0``, and pinning the full rendering would make this a test of pg's
#: deparser. What matters is the VALUE, and that it is present at all — a
#: NOT NULL column added to a populated table without a default cannot even be
#: created, so a missing default here would mean the migration is untested
#: against any application that already has rows.
ADDED_COLUMNS = (
    ("depends_on", "character varying", "YES", None),
    ("locator", "jsonb", "YES", None),
    ("options_total", "integer", "NO", "0"),
    ("business_rule_state", "character varying", "NO", "UNVERIFIED"),
    ("business_rule_evidence", "jsonb", "YES", None),
)

#: The row seeded at qec_018 and asserted across every leg. Only columns that
#: exist at qec_018 — writing one of the new ones would be writing to a column
#: the "before" state does not have.
SEED = {
    "cq_id": "a20-round-trip-row",
    "tenant_id": "a20-gate3",
    "app_id": "a20-app",
    "question_id": "q-a20-0001",
    "name": "Date of birth",
    "answer_type": "date",
    "required": True,
    "business_rule": "an applicant over 60 cannot bind above $500,000",
    "expected_next_page": "/review",
    "semantic_type": "dob",
}


async def _seed_row(url: str) -> None:
    """Write the pre-existing row, as the application's own role would see it.

    ``catalog_questions`` has FORCE ROW LEVEL SECURITY (qec_012), which applies
    to the table owner too — but not to a superuser, and this scratch database is
    only ever reached through the admin DSN. That is correct for a STRUCTURAL
    test and is stated here so nobody reads this file as an isolation proof: RLS
    is asserted structurally below (the policy survives the round trip) and
    behaviourally in ``test_rls_isolation.py``, through the least-privilege role.
    """
    cols = ", ".join(SEED)
    binds = ", ".join(f":{k}" for k in SEED)
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(f"INSERT INTO {TABLE} ({cols}) VALUES ({binds})"), SEED)
    finally:
        await engine.dispose()


async def _read_seed(url: str, *, columns: tuple[str, ...]) -> dict | None:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            row = (await conn.execute(
                text(f"SELECT {', '.join(columns)} FROM {TABLE} "
                     f"WHERE cq_id = :cq_id"),
                {"cq_id": SEED["cq_id"]},
            )).mappings().first()
            return dict(row) if row is not None else None
    finally:
        await engine.dispose()


async def _column_facts(url: str) -> dict[str, tuple[str, str, str]]:
    """``{column: (data_type, is_nullable, column_default)}`` for the table."""
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            rows = (await conn.execute(text(
                "SELECT column_name, data_type, is_nullable, "
                "       coalesce(column_default, '') "
                "FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = :t"
            ), {"t": TABLE})).all()
    finally:
        await engine.dispose()
    return {r[0]: (r[1], r[2], r[3]) for r in rows}


def _assert_seed_intact(row: dict | None, leg: str) -> None:
    assert row is not None, (
        f"[{leg}] the row seeded at {PRIOR} is GONE. {REVISION} adds columns to "
        f"an existing table; any leg of its round trip that loses a row has "
        f"dropped and recreated {TABLE} rather than altering it, and on a real "
        f"deployment that is the catalogue of every tenant."
    )
    for key, expected in SEED.items():
        assert row[key] == expected, (
            f"[{leg}] {TABLE}.{key} read back as {row[key]!r}, was written as "
            f"{expected!r}. The migration rewrote data it does not own."
        )


# ── THE ROUND TRIP ───────────────────────────────────────────────────────────

@needs_admin_db
def test_qec019_round_trips_with_its_table_still_standing():
    """UP → validate → DOWN → validate restoration → UP → final validation."""
    require_db(ADMIN_DB_URL, ENV_ADMIN_DB)
    run(_drive())


async def _drive():
    name, url = await _create_scratch_db()
    seed_cols = tuple(SEED)
    added = tuple(c for c, *_ in ADDED_COLUMNS)
    try:
        # ── BEFORE STATE ────────────────────────────────────────────────────
        _alembic(url, "upgrade", PRIOR)
        assert await _current_revision(url) == PRIOR
        before = await _fingerprint(url)

        at_prior = await _column_facts(url)
        already = [c for c in added if c in at_prior]
        assert not already, (
            f"{already} already exist on {TABLE} at {PRIOR}, so upgrading to "
            f"{REVISION} would not be adding them and this round trip would "
            f"prove nothing about the migration under test."
        )
        await _seed_row(url)
        _assert_seed_intact(await _read_seed(url, columns=seed_cols), "before")

        # ── LEG 1 · qec_019 UP ──────────────────────────────────────────────
        _alembic(url, "upgrade", REVISION)
        assert await _current_revision(url) == REVISION
        at_rev = await _fingerprint(url)
        facts = await _column_facts(url)

        missing = [c for c in added if c not in facts]
        assert not missing, (
            f"`alembic upgrade {REVISION}` reported success and {missing} are "
            f"not on {TABLE}. The catalogue's evidence columns would be absent "
            f"on a deployment whose migration log says they are present."
        )
        for column, data_type, nullable, default in ADDED_COLUMNS:
            got_type, got_null, got_default = facts[column]
            assert got_type == data_type, (
                f"{TABLE}.{column} is {got_type}, declared {data_type}")
            assert got_null == nullable, (
                f"{TABLE}.{column} is_nullable={got_null}, declared {nullable}")
            if default is None:
                continue
            assert default in got_default, (
                f"{TABLE}.{column} has default {got_default!r}, expected one "
                f"containing {default!r} — a NOT NULL column added to a table "
                f"that already has rows is only possible BECAUSE of its default, "
                f"so losing the default breaks the migration on exactly the "
                f"deployments that have data."
            )

        # THE DATA CLAIM. The pre-existing row kept every value it was written
        # with AND was handed the new defaults without an UPDATE having rewritten
        # it — which is the whole reason the migration uses server defaults.
        row = await _read_seed(url, columns=seed_cols + added)
        _assert_seed_intact(row, "after UP")
        assert row["options_total"] == 0, (
            f"the pre-existing row read back options_total={row['options_total']}"
            f", expected the server default 0. 0 means 'not counted'; anything "
            f"else would be a fabricated count on a row no crawl has re-read.")
        assert row["business_rule_state"] == "UNVERIFIED", (
            f"the pre-existing row read back business_rule_state="
            f"{row['business_rule_state']!r}. UNVERIFIED written explicitly is "
            f"what keeps 'no build has looked' distinguishable from 'no rule "
            f"exists' — a row arriving as 'observed' would claim evidence it "
            f"has none of.")
        assert row["depends_on"] is None and row["locator"] is None, (
            "an existing row was given a fabricated dependency or locator")

        # RLS is untouched by an ALTER TABLE ADD COLUMN — asserted rather than
        # assumed, because the one thing worse than a missing column on the
        # catalogue is a tenant-visible one.
        assert before["policies"] == at_rev["policies"], (
            f"{REVISION} changed the RLS policies:\n"
            f"{_diff(before, at_rev)['policies']}")
        # The fingerprint renders pg's booleans lowercase (`force=true`); the
        # match is against that rendering, not Python's repr.
        forced = [t for t in at_rev["tables"]
                  if t.startswith(f"{TABLE}|") and "force=true" in t]
        assert forced, (
            f"{TABLE} is no longer FORCE ROW LEVEL SECURITY after {REVISION}: "
            f"{[t for t in at_rev['tables'] if t.startswith(TABLE)]}")

        # ── LEG 2 · qec_019 DOWN → validate restoration ─────────────────────
        _alembic(url, "downgrade", PRIOR)
        assert await _current_revision(url) == PRIOR
        after_down = await _fingerprint(url)

        left = [c for c in added if c in await _column_facts(url)]
        assert not left, (
            f"`alembic downgrade {PRIOR}` left {left} behind on {TABLE}. An "
            f"orphaned column is the half-applied schema a rollback is supposed "
            f"to remove: the next `upgrade {REVISION}` fails on a duplicate "
            f"column and the deployment is stuck between two revisions."
        )
        restoration = _diff(before, after_down)
        assert not restoration, (
            f"the schema after `downgrade {PRIOR}` is NOT the schema that was "
            f"there before `upgrade {REVISION}`:\n{restoration}"
        )
        _assert_seed_intact(await _read_seed(url, columns=seed_cols), "after DOWN")

        # ── LEG 3 · qec_019 UP again → final validation ─────────────────────
        _alembic(url, "upgrade", REVISION)
        assert await _current_revision(url) == REVISION
        final = await _fingerprint(url)
        drift = _diff(at_rev, final)
        assert not drift, (
            f"re-applying {REVISION} produced a DIFFERENT schema than the first "
            f"application did:\n{drift}"
        )
        _assert_seed_intact(
            await _read_seed(url, columns=seed_cols + added), "after re-UP")
    finally:
        await _drop_scratch_db(name)


@needs_admin_db
def test_qec019_downgrade_is_not_a_no_op():
    """The negative control for the round trip above.

    Every assertion in ``_drive`` is satisfied by a ``downgrade()`` that does
    nothing EXCEPT the two that compare fingerprints — and those are easy to read
    as pedantry. This states the point directly: stepping back over ``qec_019``
    must actually remove something, or the round trip is a round trip over a
    migration that never applied.
    """
    require_db(ADMIN_DB_URL, ENV_ADMIN_DB)
    run(_drive_no_op_check())


async def _drive_no_op_check():
    name, url = await _create_scratch_db()
    try:
        _alembic(url, "upgrade", REVISION)
        at_rev = await _fingerprint(url)
        _alembic(url, "downgrade", PRIOR)
        after = await _fingerprint(url)

        removed = sorted(set(at_rev["columns"]) - set(after["columns"]))
        expected = {f"{TABLE}.{c}" for c, *_ in ADDED_COLUMNS}
        got = {r.split("|", 1)[0] for r in removed}
        assert got == expected, (
            f"stepping back over {REVISION} removed {sorted(got)}; {REVISION} "
            f"owns exactly {sorted(expected)}. Removing fewer leaves an orphan; "
            f"removing more takes a column another revision is responsible for."
        )
    finally:
        await _drop_scratch_db(name)


if __name__ == "__main__":  # pragma: no cover - convenience
    raise SystemExit(pytest.main([__file__, "-v"]))
