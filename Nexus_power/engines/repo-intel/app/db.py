"""repo-intel — Database layer (two engines, RLS-disciplined sessions).

Mirrors ``platform/qe-central/app/db`` (design R-7): repo-intel talks to
TWO databases —

  * ``qec_engine``       → the carved-out ``qecentral`` logical DB (role
                           ``qec``); repo-intel's 5 tables live here.
  * ``substrate_engine`` → the ``nexus`` DB (least-privilege role); used
                           READ-ONLY to SELECT ``page_visits`` for drift
                           reports.  repo-intel NEVER writes substrate rows.

Both session helpers set the ``nexus.current_tenant_id`` GUC inside the
transaction (verbatim mirror of ``tenant_scoped_session``,
platform/api/app/database.py:110-144) so RLS policies have context.  The
ORM models (``app/model/store.py``) bind against the engines exposed here;
this module owns connection lifecycle + health, never the schema (schema
is managed by the ``qec_001`` Alembic chain — no migration is written by
repo-intel).
"""
from __future__ import annotations

import logging
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

from app.config import settings

logger = logging.getLogger("repo_intel.db")


def _make_engine(url: str) -> AsyncEngine:
    """Create an async engine with the platform pool posture.

    Engine creation is lazy (no connection until first use), so importing
    this module is safe even when the database is unreachable.
    """
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

    Failure is logged, never raised — ``/health`` reports the honest state
    and request-time use raises loudly.  Mirrors ``platform_api.init_db``.
    """
    global _qec_connected, _substrate_connected
    try:
        async with qec_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        _qec_connected = True
        logger.info("repo_intel.db_qec_connected")
    except Exception as exc:
        _qec_connected = False
        logger.warning("repo_intel.db_qec_failed error=%s", str(exc)[:300])
    try:
        async with substrate_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        _substrate_connected = True
        logger.info("repo_intel.db_substrate_connected")
    except Exception as exc:
        _substrate_connected = False
        logger.warning("repo_intel.db_substrate_failed error=%s", str(exc)[:300])


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


async def ping_qec() -> str:
    """Live liveness probe on the qecentral engine (health check callable).

    Returns ``"connected"`` on success; raises on failure so the SDK
    ``/health/ready`` route records it as unhealthy — honest, never masked.
    """
    global _qec_connected
    async with qec_engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    _qec_connected = True
    return "connected"


async def ping_substrate() -> str:
    """Live liveness probe on the nexus (substrate) engine (health check)."""
    global _substrate_connected
    async with substrate_engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    _substrate_connected = True
    return "connected"


def qec_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the qecentral-DB session factory (ORM store binds to this)."""
    return _qec_session_factory


def substrate_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the nexus-DB (substrate) session factory."""
    return _substrate_session_factory


# ─── Helpers shared by route/service modules (platform idioms) ────────

def new_id() -> str:
    """Generate a new UUID string."""
    return str(uuid.uuid4())


def utc_now() -> datetime:
    """Return the current UTC datetime (timezone-aware)."""
    return datetime.now(timezone.utc)


@asynccontextmanager
async def _tenant_scoped(
    factory: async_sessionmaker[AsyncSession], tenant_id: str,
) -> AsyncIterator[AsyncSession]:
    """Open a session with the PostgreSQL RLS variable already set.

    Verbatim mirror of platform/api/app/database.py:110-144 —
    ``set_config('nexus.current_tenant_id', :tid, true)`` behaves like
    ``SET LOCAL`` but supports bound parameters; the setting lives for the
    surrounding transaction only, so there is no cross-request leak.
    """
    if not tenant_id:
        raise ValueError("tenant_id is required for a tenant-scoped session")

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
    """Tenant-scoped session on the nexus DB (READ-ONLY page_visits drift)."""
    async with _tenant_scoped(_substrate_session_factory, tenant_id) as session:
        yield session


async def guc_self_check() -> dict:
    """Round-trip the RLS GUC on the qecentral engine (health self-check).

    Sets ``nexus.current_tenant_id`` inside a transaction and reads it back;
    ``ok`` is True only when the echo matches.  Any failure is reported
    honestly, never masked.
    """
    probe = "__repo_intel_health__"
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
