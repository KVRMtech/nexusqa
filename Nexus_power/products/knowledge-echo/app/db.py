"""SQLAlchemy Core projections + async lifecycle for the echo product.

Authoritative schema is in alembic migrations:
    * 019_knowledge_foundation     — integration_installations (READ for tokens)
    * 021_echo_mvp                  — echo_dispatches, echo_feedback, echo_dedup
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, BYTEA, JSONB
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

logger = logging.getLogger(__name__)

metadata = sa.MetaData()


echo_dispatches = sa.Table(
    "echo_dispatches",
    metadata,
    sa.Column("dispatch_id", sa.String(64), primary_key=True),
    sa.Column("tenant_id", sa.String(64), nullable=False),
    sa.Column("trace_id", sa.String(64), nullable=False),
    sa.Column("trigger_surface", sa.String(32), nullable=False),
    sa.Column("trigger_plugin_event_id", sa.String(128)),
    sa.Column("trigger_user_id_ext", sa.String(128)),
    sa.Column("trigger_channel_ext", sa.String(128)),
    sa.Column("trigger_text_hash", sa.String(64), nullable=False),
    sa.Column("classifier_output", JSONB),
    sa.Column("match_candidates", JSONB),
    sa.Column("top_similarity", sa.Float),
    sa.Column("confidence_band", sa.String(16)),
    sa.Column("decision", sa.String(32), nullable=False),
    sa.Column("decision_reason", sa.String(256)),
    sa.Column("effective_mode", sa.String(32)),
    sa.Column("rendered_payload_hash", sa.String(64)),
    sa.Column("posted_at", sa.DateTime(timezone=True)),
    sa.Column("posted_message_ref", sa.String(256)),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)


echo_feedback = sa.Table(
    "echo_feedback",
    metadata,
    sa.Column("id", sa.BigInteger, primary_key=True),
    sa.Column("dispatch_id", sa.String(64), nullable=False),
    sa.Column("tenant_id", sa.String(64), nullable=False),
    sa.Column("user_id_ext", sa.String(128)),
    sa.Column("signal", sa.String(32), nullable=False),
    sa.Column("metadata_json", JSONB, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)


echo_dedup = sa.Table(
    "echo_dedup",
    metadata,
    sa.Column("tenant_id", sa.String(64), primary_key=True),
    sa.Column("dedup_key", sa.String(128), primary_key=True),
    sa.Column("dispatch_id", sa.String(64), nullable=False),
    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)


integration_installations = sa.Table(
    "integration_installations",
    metadata,
    sa.Column("installation_id", sa.String(64), primary_key=True),
    sa.Column("tenant_id", sa.String(64), nullable=False),
    sa.Column("integration_id", sa.String(128), nullable=False),
    sa.Column("integration_version", sa.String(32), nullable=False),
    sa.Column("display_name", sa.String(256)),
    sa.Column("config", JSONB, nullable=False),
    sa.Column("encrypted_credentials", BYTEA),
    sa.Column("dek_id", sa.String(128)),
    sa.Column("kek_id", sa.String(128)),
    sa.Column("scopes_granted", ARRAY(sa.String(64)), nullable=False),
    sa.Column("status", sa.String(32), nullable=False),
)


class Database:
    """Async lifecycle for the echo product."""

    def __init__(self, url: str):
        self._url = url
        self._engine: AsyncEngine | None = None
        self._factory: async_sessionmaker[AsyncSession] | None = None

    async def connect(self) -> None:
        self._engine = create_async_engine(
            self._url,
            pool_size=10,
            max_overflow=10,
            pool_pre_ping=True,
            pool_recycle=3600,
        )
        async with self._engine.connect() as conn:
            await conn.execute(sa.text("SELECT 1"))
        self._factory = async_sessionmaker(self._engine, expire_on_commit=False)
        logger.info("echo.db_connected")

    async def disconnect(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
            self._factory = None

    def factory(self) -> async_sessionmaker[AsyncSession]:
        if self._factory is None:
            raise RuntimeError("Database not connected")
        return self._factory

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.factory()() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    @asynccontextmanager
    async def tenant_session(
        self, tenant_id: str
    ) -> AsyncIterator[AsyncSession]:
        async with self.factory()() as session:
            try:
                await session.execute(
                    sa.text(
                        "SELECT set_config('nexus.current_tenant_id', :tid, true)"
                    ),
                    {"tid": tenant_id},
                )
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def health(self) -> str:
        if not self._engine:
            return "disconnected"
        try:
            async with self._engine.connect() as conn:
                await conn.execute(sa.text("SELECT 1"))
            return "healthy"
        except Exception as exc:
            return f"unhealthy: {exc}"
