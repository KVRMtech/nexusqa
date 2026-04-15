"""
Auth Service — Persistent Store.

AuthStore abstracts storage with PostgreSQL as primary and in-memory
fallback for development / CI where the DB is unavailable.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import bcrypt
from sqlalchemy import select, update

from nexus_sdk.config import production_guard
from nexus_sdk.db import Database
from nexus_sdk.db.models import TenantRow, UserRow

logger = logging.getLogger(__name__)


class AuthStore:
    """Dual-backend storage: PostgreSQL primary, in-memory fallback."""

    def __init__(self):
        self._db: Optional[Database] = None
        # In-memory fallbacks (keyed by id / email)
        self._tenants: dict[str, dict] = {}
        self._users: dict[str, dict] = {}  # keyed by email

    async def connect(self, pg_config) -> None:
        """Attempt PostgreSQL connection. Falls back to in-memory on failure."""
        try:
            self._db = Database(pg_config)
            await self._db.connect()
            logger.info("auth.store.postgres_connected")
        except Exception as exc:
            logger.warning("auth.store.postgres_failed — using in-memory: %s", exc)
            self._db = None

        # Refuse in-memory fallback in production environments
        production_guard(
            "PostgreSQL (auth-service)",
            available=self._db is not None,
        )

    @property
    def using_postgres(self) -> bool:
        return self._db is not None

    # ── Tenant operations ──────────────────────────────────

    async def get_tenant(self, tenant_id: str) -> Optional[dict]:
        if self._db:
            async with self._db.session() as session:
                row = await session.get(TenantRow, tenant_id)
                return self._tenant_to_dict(row) if row else None
        return self._tenants.get(tenant_id)

    async def list_tenants(self, *, filter_tenant_id: Optional[str] = None) -> list[dict]:
        if self._db:
            async with self._db.session() as session:
                stmt = select(TenantRow)
                if filter_tenant_id:
                    stmt = stmt.where(TenantRow.tenant_id == filter_tenant_id)
                result = await session.execute(stmt)
                return [self._tenant_to_dict(r) for r in result.scalars().all()]
        if filter_tenant_id:
            t = self._tenants.get(filter_tenant_id)
            return [t] if t else []
        return list(self._tenants.values())

    async def save_tenant(self, data: dict) -> None:
        if self._db:
            async with self._db.session() as session:
                row = TenantRow(**{k: v for k, v in data.items() if hasattr(TenantRow, k)})
                session.add(row)
            return
        self._tenants[data["tenant_id"]] = data

    # ── User operations ────────────────────────────────────

    async def get_user_by_email(self, email: str) -> Optional[dict]:
        if self._db:
            async with self._db.session() as session:
                stmt = select(UserRow).where(UserRow.email == email)
                result = await session.execute(stmt)
                row = result.scalar_one_or_none()
                return self._user_to_dict(row) if row else None
        return self._users.get(email)

    async def save_user(self, data: dict) -> None:
        if self._db:
            async with self._db.session() as session:
                row = UserRow(**{k: v for k, v in data.items() if hasattr(UserRow, k)})
                session.add(row)
            return
        self._users[data["email"]] = data

    async def email_exists(self, email: str) -> bool:
        if self._db:
            async with self._db.session() as session:
                stmt = select(UserRow.user_id).where(UserRow.email == email)
                result = await session.execute(stmt)
                return result.scalar_one_or_none() is not None
        return email in self._users

    async def update_last_login(self, email: str) -> None:
        if self._db:
            async with self._db.session() as session:
                stmt = (
                    update(UserRow)
                    .where(UserRow.email == email)
                    .values(last_login=datetime.now(timezone.utc))
                )
                await session.execute(stmt)

    # ── Serialization helpers ──────────────────────────────

    @staticmethod
    def _tenant_to_dict(row: TenantRow) -> dict:
        return {
            "tenant_id": row.tenant_id,
            "name": row.name,
            "domain": row.domain,
            "plan": row.plan,
            "status": row.status,
            "created_at": row.created_at.isoformat() if row.created_at else "",
        }

    @staticmethod
    def _user_to_dict(row: UserRow) -> dict:
        return {
            "user_id": row.user_id,
            "tenant_id": row.tenant_id,
            "email": row.email,
            "name": row.name,
            "role": row.role,
            "permissions": row.permissions or [],
            "password_hash": row.password_hash,
            "created_at": row.created_at.isoformat() if row.created_at else "",
        }
