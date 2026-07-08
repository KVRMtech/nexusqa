"""Alembic environment for the QE-Central ``qecentral`` database (async).

Mirrors the root ``alembic/env.py`` (async engine, env-var URL override)
but binds the QE-Central-private ``QecBase`` metadata and reads
``QEC_DATABASE_URL`` — the carved-out database is a separate bounded
context (R-7) and must never see VKPower metadata.
"""

import asyncio
import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

# Make the service package + the SDK importable when run from a checkout.
_HERE = os.path.dirname(os.path.abspath(__file__))
_SERVICE_ROOT = os.path.abspath(os.path.join(_HERE, ".."))
_SDK_PATH = os.path.abspath(os.path.join(_HERE, "..", "..", "..", "sdk", "nexus-sdk"))
for _p in (_SERVICE_ROOT, _SDK_PATH):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from app.db.models import QecBase  # noqa: E402 — registers the S1 ORM models

config = context.config

# Override URL from env var (the ini value is a dev placeholder only).
db_url = os.environ.get("QEC_DATABASE_URL")
if db_url:
    config.set_main_option("sqlalchemy.url", db_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = QecBase.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode — generates SQL script."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection):
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations in 'online' mode with async engine."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    """Entry point for online migrations."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
