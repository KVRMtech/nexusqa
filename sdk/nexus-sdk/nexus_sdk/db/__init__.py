"""
Nexus Database Layer — Async SQLAlchemy + PostgreSQL.

Provides:
- Async engine and session factory
- Declarative base for all models
- Tenant-scoped query helpers
- Connection pooling with health checks

Every engine that needs persistence imports from here.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from nexus_sdk.config import PostgresConfig

logger = logging.getLogger(__name__)


# ─── Declarative Base ──────────────────────────────────────────

class Base(DeclarativeBase):
    """Shared declarative base for all Nexus ORM models."""
    pass


# ─── Database Manager ─────────────────────────────────────────

class Database:
    """
    Manages async SQLAlchemy engine and session lifecycle.

    Usage:
        db = Database(config)
        await db.connect()

        async with db.session() as session:
            result = await session.execute(...)

        await db.disconnect()
    """

    def __init__(self, config: Optional[PostgresConfig] = None):
        self._config = config or PostgresConfig()
        self._engine: Optional[AsyncEngine] = None
        self._session_factory: Optional[async_sessionmaker[AsyncSession]] = None

    async def connect(self) -> None:
        """Create engine and session factory."""
        self._engine = create_async_engine(
            self._config.database_url,
            pool_size=20,
            max_overflow=10,
            pool_pre_ping=True,       # Detect stale connections
            pool_recycle=3600,         # Recycle connections every hour
            echo=False,
        )
        self._session_factory = async_sessionmaker(
            bind=self._engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
        logger.info("database.connected", extra={"url": self._config.postgres_host})

    async def disconnect(self) -> None:
        """Dispose engine and close all connections."""
        if self._engine:
            await self._engine.dispose()
            self._engine = None
            self._session_factory = None
            logger.info("database.disconnected")

    @asynccontextmanager
    async def session(self) -> AsyncGenerator[AsyncSession, None]:
        """Provide a transactional async session scope."""
        if not self._session_factory:
            raise RuntimeError("Database not connected. Call connect() first.")
        async with self._session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def create_tables(self) -> None:
        """Create all tables (for dev/test; use Alembic in production)."""
        if not self._engine:
            raise RuntimeError("Database not connected.")
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("database.tables_created")

    async def health_check(self) -> str:
        """Check database connectivity."""
        if not self._engine:
            return "not_connected"
        try:
            async with self._engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return "connected"
        except Exception as e:
            return f"error: {e}"

    @property
    def engine(self) -> Optional[AsyncEngine]:
        return self._engine


# ─── Singleton for shared access ──────────────────────────────

_db_instance: Optional[Database] = None


def get_database() -> Database:
    """Get the shared database instance."""
    global _db_instance
    if _db_instance is None:
        _db_instance = Database()
    return _db_instance


async def init_database(config: Optional[PostgresConfig] = None) -> Database:
    """Initialize and connect the shared database."""
    global _db_instance
    _db_instance = Database(config)
    await _db_instance.connect()
    return _db_instance
