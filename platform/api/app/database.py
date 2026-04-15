"""
Platform API — Database Layer.

Async SQLAlchemy setup with connection pooling and helper utilities.
"""
from __future__ import annotations

import uuid
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    create_async_engine,
    async_sessionmaker,
)

from nexus_sdk.db.models import Base

from .config import PlatformAPIConfig

logger = logging.getLogger(__name__)

# ─── Module-level state ──────────────────────────────────────

_engine: Optional[AsyncEngine] = None
_session_factory: Optional[async_sessionmaker[AsyncSession]] = None
_db_connected: bool = False


async def init_db(config: PlatformAPIConfig) -> None:
    """Initialise the async database engine and create tables."""
    global _engine, _session_factory, _db_connected

    url = config.postgres_url
    try:
        _engine = create_async_engine(
            url,
            pool_size=20,
            max_overflow=10,
            pool_pre_ping=True,
            pool_recycle=3600,
        )
        _session_factory = async_sessionmaker(_engine, expire_on_commit=False)

        async with _engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with _engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        _db_connected = True
        logger.info("platform_api.db_connected", extra={"url": config.postgres_host})
    except Exception as e:
        logger.warning("platform_api.db_failed", extra={"error": str(e)})
        _db_connected = False


async def close_db() -> None:
    """Dispose the engine connection pool."""
    global _engine
    if _engine:
        await _engine.dispose()
        _engine = None


def get_session_factory() -> Optional[async_sessionmaker[AsyncSession]]:
    """Return the configured session factory (or None if DB not connected)."""
    return _session_factory


def is_db_connected() -> bool:
    """Return True if the database is connected."""
    return _db_connected


# ─── Helpers shared by route modules ─────────────────────────

def require_db() -> async_sessionmaker[AsyncSession]:
    """Raise 503 if the database is not connected."""
    from fastapi import HTTPException
    if not _session_factory:
        raise HTTPException(503, "Database not connected")
    return _session_factory


def new_id() -> str:
    """Generate a new UUID string."""
    return str(uuid.uuid4())


def utc_now() -> datetime:
    """Return the current UTC datetime."""
    return datetime.now(timezone.utc)


def row_to_dict(row) -> dict:
    """Convert an ORM row to a plain dict, serialising datetimes to ISO."""
    d = {}
    for c in row.__table__.columns:
        val = getattr(row, c.name)
        if isinstance(val, datetime):
            val = val.isoformat()
        d[c.name] = val
    return d
