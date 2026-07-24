"""QE-Central — Database Layer (two engines, RLS-disciplined sessions).

Mirrors ``platform/api/app/database.py`` with ONE structural difference:
QE-Central talks to TWO databases (design R-7):

  * ``qec_engine``        → the carved-out ``qecentral`` logical DB
                            (role ``qec``; all QE-Central-owned tables).
  * ``substrate_engine``  → the ``nexus`` DB (role ``qec_substrate``;
                            least-privilege INSERT/SELECT on the seven
                            substrate tables + tenant bootstrap).

Both session helpers set the ``nexus.current_tenant_id`` GUC inside the
transaction (verbatim mirror of ``tenant_scoped_session``,
platform/api/app/database.py:110-144) so RLS policies have context.

``safe_frame_asset_path`` is mirrored here (platform.api.app is not
importable across containers) so every frame path QE-Central emits is
validated by the SAME rules the serving side enforces
(platform/api/app/database.py:147-188).
"""
from __future__ import annotations

import logging
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ..config import settings

logger = logging.getLogger(__name__)


def _make_engine(url: str) -> AsyncEngine:
    """Create an async engine with the platform-api pool posture.

    Engine creation is lazy (no connection is attempted until first use),
    so module-import is safe even when the database is unreachable.

    TEST-ONLY NullPool escape hatch (``QEC_TEST_DB_NULLPOOL``): the DB-gated
    contract suite drives each test through its own ``asyncio.run()`` (a fresh
    event loop per test), but a POOLED connection binds to the first loop —
    every later test then raises ``RuntimeError: got Future attached to a
    different loop`` (the sole reason qec-ci never went green end-to-end).
    NullPool opens a fresh connection per checkout bound to the CURRENT loop, so
    each test is loop-independent. Guarded by an env flag set only in CI's test
    job — production keeps its long-lived pool untouched.
    """
    import os
    if os.getenv("QEC_TEST_DB_NULLPOOL"):
        from sqlalchemy.pool import NullPool
        return create_async_engine(url, poolclass=NullPool, pool_pre_ping=True)
    return create_async_engine(
        url,
        pool_size=10,
        max_overflow=5,
        pool_pre_ping=True,
        pool_recycle=3600,
    )


# ─── Module-level engines + session factories (shared conventions) ────
qec_engine: AsyncEngine = _make_engine(settings.qec_database_url)
substrate_engine: AsyncEngine = _make_engine(settings.nexus_database_url_substrate)

_qec_session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    qec_engine, expire_on_commit=False,
)
_substrate_session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    substrate_engine, expire_on_commit=False,
)

_qec_connected: bool = False
_substrate_connected: bool = False


async def init_db() -> None:
    """Verify connectivity on both engines (schema managed by Alembic).

    Mirrors ``platform_api.init_db``: failure is logged, never raised —
    /health reports the honest state and request-time use raises loudly.
    """
    global _qec_connected, _substrate_connected
    try:
        async with qec_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        _qec_connected = True
        logger.info("qe_central.db_qec_connected")
    except Exception as exc:
        _qec_connected = False
        logger.warning("qe_central.db_qec_failed", extra={"error": str(exc)[:300]})
    try:
        async with substrate_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        _substrate_connected = True
        logger.info("qe_central.db_substrate_connected")
    except Exception as exc:
        _substrate_connected = False
        logger.warning(
            "qe_central.db_substrate_failed", extra={"error": str(exc)[:300]},
        )


async def close_db() -> None:
    """Dispose both engine connection pools."""
    await qec_engine.dispose()
    await substrate_engine.dispose()


def is_qec_connected() -> bool:
    """True when the last connectivity check on the qecentral DB succeeded."""
    return _qec_connected


def is_substrate_connected() -> bool:
    """True when the last connectivity check on the nexus DB succeeded."""
    return _substrate_connected


def qec_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the qecentral-DB session factory."""
    return _qec_session_factory


def substrate_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the substrate (nexus-DB, qec_substrate role) session factory."""
    return _substrate_session_factory


# ─── Helpers shared by route modules (platform-api idioms) ────────────

def new_id() -> str:
    """Generate a new UUID string."""
    return str(uuid.uuid4())


def utc_now() -> datetime:
    """Return the current UTC datetime (timezone-aware)."""
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
async def _tenant_scoped(
    factory: async_sessionmaker[AsyncSession], tenant_id: str,
) -> AsyncIterator[AsyncSession]:
    """Open a session with the PostgreSQL RLS variable already set.

    Verbatim mirror of platform/api/app/database.py:110-144 —
    ``set_config('nexus.current_tenant_id', :tid, true)`` behaves like
    ``SET LOCAL`` but supports bound parameters; the setting lives for
    the surrounding transaction only, so there is no cross-request leak.
    """
    from fastapi import HTTPException

    if not tenant_id:
        raise HTTPException(status_code=401, detail="tenant_id missing from auth context")

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


@asynccontextmanager
async def tenant_scoped_qec_session(tenant_id: str) -> AsyncIterator[AsyncSession]:
    """Tenant-scoped session on the qecentral DB (role ``qec``)."""
    async with _tenant_scoped(_qec_session_factory, tenant_id) as session:
        yield session


@asynccontextmanager
async def tenant_scoped_substrate_session(tenant_id: str) -> AsyncIterator[AsyncSession]:
    """Tenant-scoped session on the nexus DB (role ``qec_substrate``).

    This is THE only path QE-Central writes substrate rows through —
    all §2.1 rows for one crawl go into ONE transaction here.
    """
    async with _tenant_scoped(_substrate_session_factory, tenant_id) as session:
        yield session


async def guc_self_check() -> dict:
    """Round-trip the RLS GUC on the qecentral engine (health self-check).

    Sets ``nexus.current_tenant_id`` inside a transaction and reads it
    back; ``ok`` is True only when the echo matches.  Any failure is
    reported honestly, never masked.
    """
    probe = "__qec_health__"
    try:
        async with qec_engine.connect() as conn:
            async with conn.begin():
                await conn.execute(
                    text("SELECT set_config('nexus.current_tenant_id', :tid, true)"),
                    {"tid": probe},
                )
                echo = (await conn.execute(
                    text("SELECT current_setting('nexus.current_tenant_id', true)")
                )).scalar()
        return {"ok": echo == probe, "echo": echo or ""}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:300]}


# ─── Frame-asset path guard (mirror of platform-api database.py:147-188) ──
# Frame-asset paths are written in the shape
# "{tenant_id}/{session_id}/{job_id}_frames/frame_NNNNN.png".  We reject
# anything with traversal sequences or a foreign tenant prefix so a
# corrupted/malicious row can never surface cross-tenant content.
_FRAME_ASSET_PATH_RE = re.compile(
    r"^[A-Za-z0-9_\-]+/[A-Za-z0-9_\-]+/[A-Za-z0-9_\-./]+\.(png|jpe?g|webp)$",
    re.IGNORECASE,
)


def safe_frame_asset_path(tenant_id: str, asset_path: str | None) -> str:
    """Return ``asset_path`` if well-formed AND owned by ``tenant_id``, else "".

    Byte-for-byte the same rules as the platform-api serving side:
      * empty / None → "" (no asset present)
      * must match ``{tenant}/{session}/.../*.{png,jpg,jpeg,webp}``
      * must not contain ``..`` or backslashes
      * leading segment must equal ``tenant_id``
    """
    if not asset_path:
        return ""
    if ".." in asset_path or "\\" in asset_path:
        logger.warning(
            "qe_central.frame_asset_path.traversal_rejected",
            extra={"tenant_id": tenant_id, "path": asset_path[:200]},
        )
        return ""
    if not _FRAME_ASSET_PATH_RE.match(asset_path):
        return ""
    head, _, _ = asset_path.partition("/")
    if head != str(tenant_id):
        logger.warning(
            "qe_central.frame_asset_path.cross_tenant_rejected",
            extra={"tenant_id": tenant_id, "path_head": head},
        )
        return ""
    return asset_path
