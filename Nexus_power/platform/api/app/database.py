"""
Platform API — Database Layer.

Async SQLAlchemy setup with connection pooling and helper utilities.
"""
from __future__ import annotations

import re
import uuid
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncIterator, Optional

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

        # Verify connectivity only — schema managed by Alembic migrations
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


@asynccontextmanager
async def tenant_scoped_session(tenant_id: str) -> AsyncIterator[AsyncSession]:
    """Open a DB session with the PostgreSQL RLS variable already set.

    Migration ``029_visual_evidence_rls`` (and the earlier ``010_row_level_security``)
    rely on ``current_setting('nexus.current_tenant_id', true)`` to enforce
    tenant isolation at the database level.  This helper sets that variable
    once at session start using ``set_config(..., is_local=true)`` — which
    behaves like ``SET LOCAL`` but supports bound parameters (asyncpg
    rejects bound parameters in raw ``SET LOCAL`` syntax).

    The setting lives for the duration of the surrounding transaction and
    is automatically discarded when the connection returns to the pool,
    so there is no risk of cross-request leakage.

    Routes that touch tenant-scoped tables should prefer this helper over
    ``require_db()`` so RLS has the context it needs.
    """
    from fastapi import HTTPException

    if not tenant_id:
        raise HTTPException(status_code=401, detail="tenant_id missing from auth context")

    factory = require_db()
    async with factory() as session:
        try:
            await session.execute(
                text("SELECT set_config('nexus.current_tenant_id', :tid, true)"),
                {"tid": str(tenant_id)},
            )
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# Frame-asset paths are written by the orchestrator in the shape
# "{tenant_id}/{session_id}/{job_id}_frames/frame_NNNNN.png".  We reject
# anything that starts with a path-traversal sequence or doesn't begin
# with the requesting tenant's id, so a corrupted/malicious row in
# visual_frames can't surface cross-tenant content through a signed URL.
_FRAME_ASSET_PATH_RE = re.compile(
    r"^[A-Za-z0-9_\-]+/[A-Za-z0-9_\-]+/[A-Za-z0-9_\-./]+\.(png|jpe?g|webp)$",
    re.IGNORECASE,
)


def safe_frame_asset_path(tenant_id: str, asset_path: str | None) -> str:
    """Return ``asset_path`` if it is well-formed AND belongs to ``tenant_id``.

    Otherwise return an empty string.  Callers can safely render the value
    in API responses without further checks.

    Rules:
      * empty / None → "" (no asset present)
      * must match ``{tenant}/{session}/.../*.{png,jpg,jpeg,webp}``
      * must not contain ``..`` or backslashes
      * leading segment must equal ``tenant_id``
    """
    if not asset_path:
        return ""
    # Reject explicit traversal sequences before regex (cheaper, clearer).
    if ".." in asset_path or "\\" in asset_path:
        logger.warning(
            "platform_api.frame_asset_path.traversal_rejected",
            extra={"tenant_id": tenant_id, "path": asset_path[:200]},
        )
        return ""
    if not _FRAME_ASSET_PATH_RE.match(asset_path):
        return ""
    head, _, _ = asset_path.partition("/")
    if head != str(tenant_id):
        logger.warning(
            "platform_api.frame_asset_path.cross_tenant_rejected",
            extra={"tenant_id": tenant_id, "path_head": head},
        )
        return ""
    return asset_path
