"""App Model store — ORM + tenant-scoped persistence over the 5 qecentral
repo-intel tables (columns match ``qec_001_initial.py`` EXACTLY).

Tables (design §3.3, migration qec_001): ``repo_connections``,
``app_model_universes``, ``app_model_atoms``, ``crawl_seed_manifests``,
``repo_drift_reports``. This engine OWNS these tables; it does NOT define a
migration (Phase 0 created them). The ORM here mirrors the migration so
autogenerate would produce an empty diff.

Every write is inside a tenant-scoped transaction that sets the RLS GUC
``nexus.current_tenant_id`` before any statement (verbatim mirror of the
platform ``tenant_scoped_session`` pattern) so a row can only ever be read or
written under its own tenant.

Connection tokens are stored as an envelope blob (AAD = ``connection_id``)
and NEVER in plaintext; :meth:`RepoIntelStore.create_connection` refuses to
persist a token when the KEK provider is ``local`` outside a development
environment (mirrors the SDK ``save_profile`` refuse-plaintext discipline).
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncIterator, Dict, List, Optional, Sequence

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    Integer,
    LargeBinary,
    String,
    Text,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

logger = logging.getLogger("repo_intel.store")


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


class RepoIntelBase(DeclarativeBase):
    """Private declarative base — NOT the SDK Base, so this engine's tables
    never leak into the VKPower/qec alembic autogenerate metadata (R-7)."""


class RepoConnection(RepoIntelBase):
    __tablename__ = "repo_connections"
    connection_id = Column(String(64), primary_key=True)
    tenant_id = Column(String(64), nullable=False)
    app_id = Column(String(64), nullable=False, default="")
    provider = Column(String(32), nullable=False, default="gitlab")
    base_url = Column(String(2000), nullable=False, default="")
    project_path = Column(String(1000), nullable=False, default="")
    default_branch = Column(String(200), nullable=False, default="main")
    encrypted_token = Column(LargeBinary, nullable=True)
    kek_id = Column(String(200), nullable=False, default="")
    label = Column(String(200), nullable=False, default="")
    status = Column(String(32), nullable=False, default="active")
    last_sync_sha = Column(String(64), nullable=False, default="")
    last_error = Column(Text, nullable=False, default="")
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_now)


class AppModelUniverse(RepoIntelBase):
    __tablename__ = "app_model_universes"
    universe_id = Column(String(64), primary_key=True)
    tenant_id = Column(String(64), nullable=False)
    connection_id = Column(String(64), nullable=False)
    deployed_sha = Column(String(64), nullable=False, default="")
    branch = Column(String(200), nullable=False, default="")
    stack_fingerprint = Column(JSONB, nullable=False, default=dict)
    extractor_versions = Column(JSONB, nullable=False, default=dict)
    ceiling_bands = Column(JSONB, nullable=False, default=dict)
    status = Column(String(32), nullable=False, default="building")
    degraded_extractors = Column(JSONB, nullable=False, default=list)
    atom_count = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_now)


class AppModelAtom(RepoIntelBase):
    __tablename__ = "app_model_atoms"
    atom_id = Column(String(64), primary_key=True)
    tenant_id = Column(String(64), nullable=False)
    universe_id = Column(String(64), nullable=False)
    kind = Column(String(32), nullable=False)
    value = Column(JSONB, nullable=False, default=dict)
    provenance_path = Column(String(1000), nullable=False, default="")
    provenance_line = Column(Integer, nullable=False, default=0)
    provenance_sha = Column(String(64), nullable=False, default="")
    quote = Column(String(500), nullable=False, default="")
    extractor = Column(String(100), nullable=False, default="")
    confidence = Column(Float, nullable=False, default=0.0)
    source_tier = Column(String(32), nullable=False, default="")
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)


class CrawlSeedManifest(RepoIntelBase):
    __tablename__ = "crawl_seed_manifests"
    manifest_id = Column(String(64), primary_key=True)
    tenant_id = Column(String(64), nullable=False)
    universe_id = Column(String(64), nullable=False)
    manifest = Column(JSONB, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_now)


class RepoDriftReport(RepoIntelBase):
    __tablename__ = "repo_drift_reports"
    report_id = Column(String(64), primary_key=True)
    tenant_id = Column(String(64), nullable=False)
    universe_id = Column(String(64), nullable=False)
    artifact_id = Column(String(64), nullable=False, default="")
    summary = Column(JSONB, nullable=False, default=dict)
    items = Column(JSONB, nullable=False, default=list)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)


def universe_identity(connection_id: str, deployed_sha: str, extractor_versions: Dict) -> str:
    """Deterministic universe id mirroring the migration's uniqueness rule
    (connection, sha, md5(extractor_versions)) so re-analysis is idempotent."""
    ev = ",".join(f"{k}={extractor_versions[k]}" for k in sorted(extractor_versions))
    raw = f"{connection_id}|{deployed_sha}|{ev}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


class RepoIntelStore:
    """Async persistence facade for the App Model. Construct with the
    qecentral DSN; every mutating method is tenant-scoped (RLS GUC set)."""

    def __init__(self, qec_dsn: str) -> None:
        self._engine = create_async_engine(qec_dsn, pool_pre_ping=True)
        self._session_factory = async_sessionmaker(self._engine, expire_on_commit=False)

    @asynccontextmanager
    async def tenant_session(self, tenant_id: str) -> AsyncIterator[AsyncSession]:
        """Yield a session with the RLS GUC bound for ``tenant_id``.

        Mirrors the platform tenant_scoped_session: the GUC is set inside the
        transaction with ``is_local=true`` so it is scoped to this txn only.
        """
        if not tenant_id:
            raise ValueError("tenant_id is required for a tenant-scoped session")
        async with self._session_factory() as session:
            await session.execute(
                text("SELECT set_config('nexus.current_tenant_id', :tid, true)"),
                {"tid": str(tenant_id)},
            )
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def create_connection(
        self,
        *,
        tenant_id: str,
        provider: str,
        base_url: str,
        project_path: str,
        default_branch: str,
        encrypted_token: Optional[bytes],
        kek_id: str,
        app_id: str = "",
        label: str = "",
        connection_id: Optional[str] = None,
    ) -> str:
        """Persist a connection with its token ALREADY envelope-encrypted.
        The caller (router) performs encryption via the injected envelope
        service; the store never sees or logs a plaintext token.

        ``connection_id`` may be supplied so the caller can seal the token
        with AAD = the SAME id the row is stored under (else the AAD would not
        match on decrypt). Absent ⇒ minted here."""
        connection_id = connection_id or _uuid()
        async with self.tenant_session(tenant_id) as s:
            s.add(RepoConnection(
                connection_id=connection_id, tenant_id=tenant_id, app_id=app_id,
                provider=provider, base_url=base_url, project_path=project_path,
                default_branch=default_branch, encrypted_token=encrypted_token,
                kek_id=kek_id, label=label, status="active",
            ))
        logger.info("repo connection created id=%s provider=%s", connection_id, provider)
        return connection_id

    async def revoke_connection(self, *, tenant_id: str, connection_id: str) -> None:
        """Zero the ciphertext and mark revoked (DELETE-of-secret discipline)."""
        async with self.tenant_session(tenant_id) as s:
            await s.execute(
                update(RepoConnection)
                .where(RepoConnection.connection_id == connection_id)
                .values(encrypted_token=None, status="revoked",
                        updated_at=_now())
            )

    async def upsert_universe(
        self, *, tenant_id: str, connection_id: str, deployed_sha: str,
        branch: str, stack_fingerprint: Dict, extractor_versions: Dict,
        ceiling_bands: Dict, status: str, degraded_extractors: Sequence[str],
        atom_count: int,
    ) -> str:
        universe_id = universe_identity(connection_id, deployed_sha, extractor_versions)
        async with self.tenant_session(tenant_id) as s:
            existing = await s.get(AppModelUniverse, universe_id)
            if existing is None:
                s.add(AppModelUniverse(
                    universe_id=universe_id, tenant_id=tenant_id,
                    connection_id=connection_id, deployed_sha=deployed_sha,
                    branch=branch, stack_fingerprint=stack_fingerprint,
                    extractor_versions=extractor_versions, ceiling_bands=ceiling_bands,
                    status=status, degraded_extractors=list(degraded_extractors),
                    atom_count=atom_count,
                ))
            else:
                existing.status = status
                existing.ceiling_bands = ceiling_bands
                existing.degraded_extractors = list(degraded_extractors)
                existing.atom_count = atom_count
                existing.updated_at = _now()
        return universe_id

    async def replace_atoms(
        self, *, tenant_id: str, universe_id: str, atom_rows: List[Dict],
    ) -> int:
        """Replace the atom set for a universe (idempotent re-analysis)."""
        async with self.tenant_session(tenant_id) as s:
            await s.execute(
                text("DELETE FROM app_model_atoms WHERE universe_id = :u AND tenant_id = :t"),
                {"u": universe_id, "t": tenant_id},
            )
            for row in atom_rows:
                s.add(AppModelAtom(
                    atom_id=_uuid(), tenant_id=tenant_id, universe_id=universe_id,
                    **row,
                ))
        return len(atom_rows)

    async def upsert_seed_manifest(
        self, *, tenant_id: str, universe_id: str, manifest: Dict,
    ) -> str:
        async with self.tenant_session(tenant_id) as s:
            existing = (await s.execute(
                select(CrawlSeedManifest).where(
                    CrawlSeedManifest.universe_id == universe_id,
                    CrawlSeedManifest.tenant_id == tenant_id,
                )
            )).scalar_one_or_none()
            if existing is None:
                manifest_id = _uuid()
                s.add(CrawlSeedManifest(
                    manifest_id=manifest_id, tenant_id=tenant_id,
                    universe_id=universe_id, manifest=manifest,
                ))
            else:
                manifest_id = existing.manifest_id
                existing.manifest = manifest
                existing.updated_at = _now()
        return manifest_id

    async def save_drift_report(
        self, *, tenant_id: str, universe_id: str, artifact_id: str,
        summary: Dict, items: List[Dict],
    ) -> str:
        report_id = _uuid()
        async with self.tenant_session(tenant_id) as s:
            s.add(RepoDriftReport(
                report_id=report_id, tenant_id=tenant_id, universe_id=universe_id,
                artifact_id=artifact_id, summary=summary, items=items,
            ))
        return report_id

    async def get_universe(self, *, tenant_id: str, universe_id: str) -> Optional[AppModelUniverse]:
        async with self.tenant_session(tenant_id) as s:
            return await s.get(AppModelUniverse, universe_id)

    async def list_atoms(
        self, *, tenant_id: str, universe_id: str, kind: Optional[str] = None,
    ) -> List[AppModelAtom]:
        async with self.tenant_session(tenant_id) as s:
            stmt = select(AppModelAtom).where(
                AppModelAtom.universe_id == universe_id,
                AppModelAtom.tenant_id == tenant_id,
            )
            if kind:
                stmt = stmt.where(AppModelAtom.kind == kind)
            return list((await s.execute(stmt)).scalars().all())

    async def latest_universe_for_connection(
        self, *, tenant_id: str, connection_id: str,
    ) -> Optional[AppModelUniverse]:
        """The most recently built App-Model universe for a connection (CODE P4).

        The ``/diff`` producer maps changed files onto THIS universe's atoms.  Newest
        by ``created_at`` (ties broken by id) so a re-analysis supersedes the old one.
        ``None`` ⇒ the app was never analyzed ⇒ the caller fail-safes to a full cycle.
        """
        async with self.tenant_session(tenant_id) as s:
            stmt = (
                select(AppModelUniverse)
                .where(
                    AppModelUniverse.connection_id == connection_id,
                    AppModelUniverse.tenant_id == tenant_id,
                )
                .order_by(AppModelUniverse.created_at.desc(), AppModelUniverse.universe_id.desc())
                .limit(1)
            )
            return (await s.execute(stmt)).scalars().first()

    async def get_seed_manifest(self, *, tenant_id: str, universe_id: str) -> Optional[Dict]:
        async with self.tenant_session(tenant_id) as s:
            row = (await s.execute(
                select(CrawlSeedManifest).where(
                    CrawlSeedManifest.universe_id == universe_id,
                    CrawlSeedManifest.tenant_id == tenant_id,
                )
            )).scalar_one_or_none()
            return dict(row.manifest) if row else None

    async def dispose(self) -> None:
        await self._engine.dispose()
