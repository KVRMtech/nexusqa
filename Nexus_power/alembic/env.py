"""
Alembic environment configuration for async PostgreSQL.
"""

import asyncio
import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

# Add SDK to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "sdk", "nexus-sdk"))

from nexus_sdk.db import Base
from nexus_sdk.db.models import (  # noqa: F401 — import to register models
    TenantRow,
    UserRow,
    WorkflowInstanceRow,
    WorkflowContextRow,
    AuditLogRow,
    ReportRow,
    ShieldAuditRow,
    SessionRow,
    SMEProfileRow,
    ContradictionRow,
    GuardrailPipelineRow,
    ReviewQueueRow,
    TrustTrendRow,
    TraceRow,
    TestSuiteRow,
    TestRunRow,
    ForgeConfigRow,
    ForgeResultRow,
    JurisdictionRow,
    TestCaseRow,
    TestCaseStepRow,
    TestCasePreconditionRow,
    DataWorkbookEntryRow,
    ExportJobRow,
    CanonicalArtifactRow,
    VisualFlowRow,
    EvidenceStepRow,
    CursorEventRow,
    E2EScenarioStateRow,
    E2ETestRunRow,
    E2ETestRunStepRow,
)

config = context.config

# Override URL from env var if available
db_url = os.environ.get("DATABASE_URL")
if db_url:
    config.set_main_option("sqlalchemy.url", db_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


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
