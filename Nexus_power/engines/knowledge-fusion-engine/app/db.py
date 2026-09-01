"""SQLAlchemy Core projections of the tables this engine reads/writes.

The authoritative schema lives in alembic migrations:

* ``006_canonical_artifacts`` / ``008_canonical_provenance`` —
  ``canonical_artifacts`` (READ ONLY)
* ``019_knowledge_foundation``                              —
  ``products`` (READ for product tagging)
* ``020_knowledge_substrate``                               —
  ``transcript_segments``, ``media_clips``, ``indexing_jobs``
  (READ/WRITE)

Keep these projections in sync with the migrations on every schema change.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSON, JSONB
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

logger = logging.getLogger(__name__)

metadata = sa.MetaData()


canonical_artifacts = sa.Table(
    "canonical_artifacts",
    metadata,
    sa.Column("artifact_id", sa.String(64), primary_key=True),
    sa.Column("tenant_id", sa.String(64), nullable=False),
    sa.Column("session_id", sa.String(64), nullable=False),
    sa.Column("media_fingerprint", sa.String(128)),
    sa.Column("status", sa.String(30)),
    sa.Column("duration_seconds", sa.Float),
    sa.Column("scene_count", sa.Integer),
    sa.Column("frame_count", sa.Integer),
    sa.Column("safe_transcript_text", sa.Text),
    sa.Column("visual_summary", sa.Text),
    sa.Column("application_types_seen", JSON),
    sa.Column("full_artifact_json", JSON),
    sa.Column("workflow_id", sa.String(64)),
    sa.Column("source_filename", sa.String(500)),
    sa.Column("created_by", sa.String(200)),
    sa.Column("quality_gate_outcome", sa.String(30)),
    sa.Column("completed_at", sa.DateTime(timezone=True)),
)


products = sa.Table(
    "products",
    metadata,
    sa.Column("product_id", sa.String(64), primary_key=True),
    sa.Column("tenant_id", sa.String(64), nullable=False),
    sa.Column("name", sa.String(256), nullable=False),
    sa.Column("slug", sa.String(128), nullable=False),
    sa.Column("aliases", ARRAY(sa.String(128)), nullable=False),
    sa.Column("description", sa.Text),
    sa.Column("status", sa.String(16), nullable=False),
    sa.Column("metadata_json", JSONB, nullable=False),
)


transcript_segments = sa.Table(
    "transcript_segments",
    metadata,
    sa.Column("segment_id", sa.String(64), primary_key=True),
    sa.Column("tenant_id", sa.String(64), nullable=False),
    sa.Column("session_id", sa.String(64), nullable=False),
    sa.Column("artifact_id", sa.String(64), nullable=False),
    sa.Column("ordinal", sa.Integer, nullable=False),
    sa.Column("speaker_id", sa.String(64)),
    sa.Column("speaker_role", sa.String(64)),
    sa.Column("text_redacted", sa.Text, nullable=False),
    sa.Column("text_hash", sa.String(64), nullable=False),
    sa.Column("start_ms", sa.BigInteger, nullable=False),
    sa.Column("end_ms", sa.BigInteger, nullable=False),
    sa.Column("token_count", sa.Integer),
    sa.Column("confidence", sa.Float),
    sa.Column("product_ids", ARRAY(sa.String(64)), nullable=False),
    sa.Column("topic_label", sa.String(256)),
    sa.Column("backbone_node_id", sa.String(64)),
    sa.Column("embedding_status", sa.String(16), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)


media_clips = sa.Table(
    "media_clips",
    metadata,
    sa.Column("clip_id", sa.String(64), primary_key=True),
    sa.Column("tenant_id", sa.String(64), nullable=False),
    sa.Column("session_id", sa.String(64), nullable=False),
    sa.Column("artifact_id", sa.String(64)),
    sa.Column("segment_id", sa.String(64)),
    sa.Column("kind", sa.String(16), nullable=False),
    sa.Column("start_ms", sa.BigInteger, nullable=False),
    sa.Column("end_ms", sa.BigInteger, nullable=False),
    sa.Column("duration_ms", sa.Integer, nullable=False),
    sa.Column("s3_bucket", sa.String(256), nullable=False),
    sa.Column("s3_key", sa.String(512), nullable=False),
    sa.Column("content_type", sa.String(64), nullable=False),
    sa.Column("size_bytes", sa.BigInteger),
    sa.Column("checksum_sha256", sa.String(64), nullable=False),
    sa.Column("thumbnail_key", sa.String(512)),
    sa.Column("hits", sa.Integer, nullable=False),
    sa.Column("status", sa.String(16), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("last_served_at", sa.DateTime(timezone=True)),
)


indexing_jobs = sa.Table(
    "indexing_jobs",
    metadata,
    sa.Column("job_id", sa.String(64), primary_key=True),
    sa.Column("tenant_id", sa.String(64), nullable=False),
    sa.Column("session_id", sa.String(64), nullable=False),
    sa.Column("artifact_id", sa.String(64), nullable=False),
    sa.Column("status", sa.String(16), nullable=False),
    sa.Column("attempts", sa.Integer, nullable=False),
    sa.Column("max_attempts", sa.Integer, nullable=False),
    sa.Column("last_error", sa.Text),
    sa.Column("locked_by", sa.String(128)),
    sa.Column("locked_until", sa.DateTime(timezone=True)),
    sa.Column("trace_id", sa.String(64)),
    sa.Column("input", JSONB, nullable=False),
    sa.Column("result", JSONB),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("started_at", sa.DateTime(timezone=True)),
    sa.Column("completed_at", sa.DateTime(timezone=True)),
)


# ── Engine + session lifecycle ─────────────────────────────────


class Database:
    """Async DB lifecycle owned by the fusion engine."""

    def __init__(self, url: str):
        self._url = url
        self._engine: AsyncEngine | None = None
        self._session_factory: async_sessionmaker[AsyncSession] | None = None

    async def connect(self) -> None:
        self._engine = create_async_engine(
            self._url,
            pool_size=10,
            max_overflow=10,
            pool_pre_ping=True,
            pool_recycle=3600,
        )
        # Smoke test
        async with self._engine.connect() as conn:
            await conn.execute(sa.text("SELECT 1"))
        self._session_factory = async_sessionmaker(
            self._engine, expire_on_commit=False
        )
        logger.info("fusion.db_connected")

    async def disconnect(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
            self._session_factory = None

    def factory(self) -> async_sessionmaker[AsyncSession]:
        if self._session_factory is None:
            raise RuntimeError("Database not connected")
        return self._session_factory

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
    async def tenant_session(self, tenant_id: str) -> AsyncIterator[AsyncSession]:
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
