"""M0.x §21 — the ORM and the migrated database describe the SAME schema.

Two artifacts define the qecentral schema and neither validates the other:
``alembic_qec/versions/*`` build it, and ``app/db/*_models.py`` reads and writes
it. Alembic autogenerate is not used here (every revision is hand-written), so
nothing has ever compared them. The failure that gap produces is well documented
in this codebase — qec_016's own docstring records it: ``client_apps
.login_recording`` existed as a hand-applied SQL script and as a
``nullable=False`` ORM column, but as no migration, so a fresh install came up
with a model that could not be written. That defect is exactly this test's shape.

Two directions, and they fail for different reasons:

  **model → database** (the dangerous one). A table, column or index the ORM
  declares but the migration never creates is a runtime error on a fresh install:
  ``UndefinedColumn`` at the first INSERT, in production, after deploy. Any miss
  here is a hard failure.

  **database → model**. An object the migration creates that no model mentions is
  not a crash, but it is either dead schema or a table accessed exclusively by
  raw SQL. Both are legitimate and both should be *declared*, so the
  ``_NO_ORM_MODEL`` list carries a reason for each — and is checked for stale
  entries, so the exemptions cannot outlive their tables.

Types are deliberately NOT compared. SQLAlchemy's ``String(64)`` and
information_schema's ``character varying`` need a dialect-aware normaliser to
compare honestly, and a half-right one produces noise that trains people to
ignore this gate. Names and presence are compared exactly; that is where the
outages come from.

This is not a second schema-management system: nothing here writes DDL, and
Alembic remains the only authority. It only reads both descriptions and diffs.
"""
from __future__ import annotations

import pytest
from _dbgate import ENV_QEC_DB, QEC_DB_URL, db_gate, new_engine, require_db, run
from sqlalchemy import text

needs_qec_db = db_gate(
    QEC_DB_URL, ENV_QEC_DB, "the schema drift gate needs a qecentral DB at alembic head",
)

#: Tables the migrations own that have NO SQLAlchemy model. Each needs a reason.
_NO_ORM_MODEL: dict[str, str] = {
    "alembic_version": "alembic's own bookkeeping table — not application schema",
    # The S3 repo-intelligence tables (qec_001) are read and written exclusively
    # through raw SQL in app/clients/repo_intel.py and app/services/*, so they
    # were never given a declarative model. Recorded here rather than silently
    # tolerated: if one of them ever gains a model, the stale-entry check below
    # forces this list to be updated.
    "repo_connections": "S3 repo-intel — raw-SQL access only, no declarative model",
    # M3.3 / T-FL-02. The explorer worker registry is FLEET INFRASTRUCTURE, read
    # and written exclusively through raw SQL in
    # app/controlplane/scheduling/worker_registry.py. The scheduling hot path is
    # a set of single conditional UPDATEs whose atomicity is the whole point
    # ("in_flight < capacity" evaluated under the row lock the UPDATE takes), and
    # an ORM round trip would reintroduce the check-then-write window that the
    # raw statement exists to close.
    "explorer_workers": "M3.3 worker registry — raw-SQL only; the atomic "
                        "capacity UPDATE must not become an ORM read-modify-write",
    "app_model_universes": "S3 repo-intel — raw-SQL access only, no declarative model",
    "app_model_atoms": "S3 repo-intel — raw-SQL access only, no declarative model",
    "crawl_seed_manifests": "S3 repo-intel — raw-SQL access only, no declarative model",
    "repo_drift_reports": "S3 repo-intel — raw-SQL access only, no declarative model",
}


def _orm_metadata():
    """The COMPLETE QecBase metadata.

    Importing ``app.db.models`` alone registers barely half the tables — each
    model module must be imported for its classes to attach themselves to
    ``QecBase.metadata``. ``alembic_qec/env.py`` imports only ``app.db.models``,
    which is harmless there (no autogenerate) but would make this test blind to
    every table outside that one module.
    """
    from app.db.models import QecBase

    for module in (
        "advance_models", "controlplane_models", "fleet_models",
        "gov_models", "journey_models", "journey_run_models",
    ):
        __import__(f"app.db.{module}")
    return QecBase.metadata


async def _live_schema(url: str) -> dict:
    engine = new_engine(url)
    try:
        async with engine.connect() as conn:
            columns = (await conn.execute(text(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = 'public'"
            ))).all()
            indexes = (await conn.execute(text(
                "SELECT indexname FROM pg_indexes WHERE schemaname = 'public'"
            ))).scalars().all()
    finally:
        await engine.dispose()

    by_table: dict[str, set[str]] = {}
    for table_name, column_name in columns:
        by_table.setdefault(table_name, set()).add(column_name)
    return {"columns": by_table, "indexes": set(indexes)}


def _snapshot():
    require_db(QEC_DB_URL, ENV_QEC_DB)
    return _orm_metadata(), run(_live_schema(QEC_DB_URL))


@needs_qec_db
def test_every_model_table_exists_in_the_migrated_database():
    """model → database. A missing table is a fresh-install outage."""
    metadata, live = _snapshot()
    missing = sorted(set(metadata.tables) - set(live["columns"]))
    assert not missing, (
        "The ORM declares table(s) that NO migration creates. A fresh install "
        "would come up with models it cannot read or write:\n  "
        + "\n  ".join(missing)
    )


@needs_qec_db
def test_every_model_column_exists_in_the_migrated_database():
    """model → database, at column granularity — the qec_016 defect's shape."""
    metadata, live = _snapshot()
    offenders = []
    for name, table in sorted(metadata.tables.items()):
        live_columns = live["columns"].get(name)
        if live_columns is None:
            continue  # the table-level test above owns this failure
        for column in table.columns:
            if column.name not in live_columns:
                nullability = "NOT NULL" if not column.nullable else "nullable"
                offenders.append(
                    f"{name}.{column.name} ({nullability}) — declared by the "
                    f"model, created by no migration"
                )
    assert not offenders, (
        "Column(s) the ORM expects but the migration chain never creates. Every "
        "NOT NULL entry below is a guaranteed INSERT failure on a fresh "
        "install:\n  " + "\n  ".join(offenders)
    )


@needs_qec_db
def test_every_model_declared_index_exists_in_the_database():
    """model → database, for indexes.

    The model modules state that their ``Index`` declarations "reproduce the
    migration's index NAMES so ORM metadata is a faithful description of the
    deployed schema". Nothing enforced that claim until now — the qec_017 index
    was added to both sides precisely because this test would otherwise have
    caught it on one.
    """
    metadata, live = _snapshot()
    missing = []
    for name, table in sorted(metadata.tables.items()):
        for index in table.indexes:
            if index.name not in live["indexes"]:
                missing.append(f"{name}: {index.name}")
    assert not missing, (
        "Index(es) declared in the ORM that do not exist in the database. Either "
        "the migration was never written, or the ORM name is a typo — in both "
        "cases the model is lying about the deployed schema:\n  "
        + "\n  ".join(missing)
    )


@needs_qec_db
def test_every_database_table_is_modelled_or_declared_unmodelled():
    """database → model. Undeclared schema is either dead or invisible."""
    metadata, live = _snapshot()
    unexplained = sorted(set(live["columns"]) - set(metadata.tables) - set(_NO_ORM_MODEL))
    assert not unexplained, (
        "Table(s) in the database with no ORM model and no declared reason. Add "
        "a model, or add a reason-annotated entry to _NO_ORM_MODEL if the table "
        "is genuinely raw-SQL-only:\n  " + "\n  ".join(unexplained)
    )

    stale = sorted(t for t in _NO_ORM_MODEL if t not in live["columns"])
    assert not stale, (
        f"_NO_ORM_MODEL exempts table(s) that no longer exist: {stale}"
    )

    now_modelled = sorted(t for t in _NO_ORM_MODEL if t in metadata.tables)
    assert not now_modelled, (
        f"Table(s) declared model-less have GAINED a model — remove them from "
        f"_NO_ORM_MODEL so the column-level drift checks start covering "
        f"them: {now_modelled}"
    )


@needs_qec_db
def test_no_migration_created_column_is_invisible_to_its_model():
    """database → model, at column granularity.

    A column a migration adds but the model never learns about is silently
    unreadable through the ORM — the writes that were supposed to populate it
    never happen, and the defect surfaces months later as "that field is always
    empty". Only tables that HAVE a model are checked; unmodelled tables are the
    previous test's business.
    """
    metadata, live = _snapshot()
    offenders = []
    for name, table in sorted(metadata.tables.items()):
        live_columns = live["columns"].get(name)
        if live_columns is None:
            continue
        model_columns = {c.name for c in table.columns}
        for extra in sorted(live_columns - model_columns):
            offenders.append(f"{name}.{extra}")
    assert not offenders, (
        "Column(s) the migrations create that the ORM model does not declare — "
        "unreachable through the ORM, so nothing ever writes them:\n  "
        + "\n  ".join(offenders)
    )


if __name__ == "__main__":  # pragma: no cover — ad-hoc local run
    pytest.main([__file__, "-v"])
