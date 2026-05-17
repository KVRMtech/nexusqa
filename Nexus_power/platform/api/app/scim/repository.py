"""SCIM repository — DB access for org_users + org_groups + memberships.

The repository is the only writer of these tables. It owns mapping
between the wire-format SCIM models and the columns in migration 024,
preserves unmodelled SCIM attributes inside ``scim_metadata``, and
exposes idempotent ``put`` and patch-application semantics.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .errors import SCIMError
from .filters import SCIMFilter
from .models import (
    SCIMGroupResource,
    SCIMGroupMember,
    SCIMEmail,
    SCIMEnterpriseUser,
    SCIMMeta,
    SCIMName,
    SCIMUserResource,
    USER_SCHEMA_URN,
    GROUP_SCHEMA_URN,
    ENTERPRISE_SCHEMA_URN,
)

logger = logging.getLogger(__name__)


_md = sa.MetaData()


org_users = sa.Table(
    "org_users",
    _md,
    sa.Column("org_user_id", sa.String(64), primary_key=True),
    sa.Column("tenant_id", sa.String(64), nullable=False),
    sa.Column("external_id", sa.String(128), nullable=False),
    sa.Column("user_name", sa.String(256), nullable=False),
    sa.Column("display_name", sa.String(256)),
    sa.Column("email", sa.String(256)),
    sa.Column("active", sa.Boolean, nullable=False),
    sa.Column("department", sa.String(128)),
    sa.Column("team", sa.String(128)),
    sa.Column("region", sa.String(128)),
    sa.Column("location", sa.String(128)),
    sa.Column("role", sa.String(128)),
    sa.Column("title", sa.String(256)),
    sa.Column("hire_date", sa.Date),
    sa.Column("manager_org_user_id", sa.String(64)),
    sa.Column("jurisdiction", sa.String(64)),
    sa.Column("external_ids", JSONB, nullable=False),
    sa.Column("scim_metadata", JSONB, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("version", sa.Integer, nullable=False),
)


org_groups = sa.Table(
    "org_groups",
    _md,
    sa.Column("org_group_id", sa.String(64), primary_key=True),
    sa.Column("tenant_id", sa.String(64), nullable=False),
    sa.Column("external_id", sa.String(128), nullable=False),
    sa.Column("display_name", sa.String(256), nullable=False),
    sa.Column("description", sa.Text),
    sa.Column("group_kind", sa.String(32), nullable=False),
    sa.Column("metadata_json", JSONB, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)


org_user_groups = sa.Table(
    "org_user_groups",
    _md,
    sa.Column("tenant_id", sa.String(64), primary_key=True),
    sa.Column("org_user_id", sa.String(64), primary_key=True),
    sa.Column("org_group_id", sa.String(64), primary_key=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)


# ── DTOs returned to the route layer ───────────────────────────


@dataclass(frozen=True)
class StoredUser:
    org_user_id: str
    external_id: str
    user_name: str
    display_name: Optional[str]
    email: Optional[str]
    active: bool
    version: int
    created_at: datetime
    updated_at: datetime
    scim_metadata: dict[str, Any]


@dataclass(frozen=True)
class StoredGroup:
    org_group_id: str
    external_id: str
    display_name: str
    description: Optional[str]
    group_kind: str
    created_at: datetime
    updated_at: datetime


# ── Repository ─────────────────────────────────────────────────


class SCIMRepository:
    """All operations are tenant-scoped via the standard RLS pattern."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        base_location: str = "/scim/v2",
    ):
        self._sf = session_factory
        self._base_location = base_location.rstrip("/")

    # ── User CRUD ───────────────────────────────────────────────

    async def create_or_replace_user(
        self,
        *,
        tenant_id: str,
        payload: SCIMUserResource,
        replace_id: Optional[str] = None,
    ) -> SCIMUserResource:
        external_id = (payload.externalId or payload.userName).strip()
        if not external_id:
            raise SCIMError(
                status=400,
                detail="userName or externalId required",
                scim_type="invalidValue",
            )
        # Manager id, if SCIM gave one, comes pre-resolved by the
        # provisioner as a tenant-local external_id reference; we
        # look it up locally and refuse if unknown.
        manager_org_user_id = await self._resolve_manager(
            tenant_id, payload.enterpriseUser
        )

        now = _now()
        async with self._sf() as session:
            await _set_tenant(session, tenant_id)
            existing = await self._fetch_user_by_external(
                session, tenant_id, external_id
            )
            row_id = (
                replace_id
                or (existing["org_user_id"] if existing else uuid.uuid4().hex)
            )
            row_values = _user_to_row(
                tenant_id=tenant_id,
                org_user_id=row_id,
                external_id=external_id,
                payload=payload,
                manager_org_user_id=manager_org_user_id,
                now=now,
            )
            stmt = pg_insert(org_users).values(**row_values)
            stmt = stmt.on_conflict_do_update(
                index_elements=[
                    org_users.c.tenant_id,
                    org_users.c.external_id,
                ],
                set_={
                    "user_name": stmt.excluded.user_name,
                    "display_name": stmt.excluded.display_name,
                    "email": stmt.excluded.email,
                    "active": stmt.excluded.active,
                    "department": stmt.excluded.department,
                    "team": stmt.excluded.team,
                    "region": stmt.excluded.region,
                    "location": stmt.excluded.location,
                    "role": stmt.excluded.role,
                    "title": stmt.excluded.title,
                    "manager_org_user_id": stmt.excluded.manager_org_user_id,
                    "external_ids": stmt.excluded.external_ids,
                    "scim_metadata": stmt.excluded.scim_metadata,
                    # updated_at + version maintained by trigger.
                },
            ).returning(org_users)
            row = (await session.execute(stmt)).mappings().first()
            await session.commit()
        if row is None:
            raise SCIMError(status=500, detail="user upsert failed")
        return self._row_to_user_resource(row)

    async def get_user(
        self, *, tenant_id: str, org_user_id: str
    ) -> Optional[SCIMUserResource]:
        async with self._sf() as session:
            await _set_tenant(session, tenant_id)
            row = (
                await session.execute(
                    sa.select(org_users).where(
                        org_users.c.tenant_id == tenant_id,
                        org_users.c.org_user_id == org_user_id,
                    )
                )
            ).mappings().first()
        return self._row_to_user_resource(row) if row else None

    async def list_users(
        self,
        *,
        tenant_id: str,
        scim_filter: Optional[SCIMFilter],
        start_index: int,
        count: int,
    ) -> tuple[list[SCIMUserResource], int]:
        # Translate the filter to SQL where-clauses where we can.
        clauses, py_filter = _split_filter_for_users(scim_filter)
        async with self._sf() as session:
            await _set_tenant(session, tenant_id)
            stmt = sa.select(org_users).where(
                org_users.c.tenant_id == tenant_id, *clauses
            )
            total = int(
                (
                    await session.execute(
                        sa.select(sa.func.count()).select_from(
                            stmt.subquery()
                        )
                    )
                ).scalar_one()
            )
            stmt = (
                stmt.order_by(org_users.c.user_name.asc())
                .offset(max(0, start_index - 1))
                .limit(max(1, min(count, 1000)))
            )
            rows = (await session.execute(stmt)).mappings().all()
        resources = [self._row_to_user_resource(r) for r in rows]
        if py_filter is not None:
            resources = [
                r
                for r in resources
                if py_filter.matches(r.model_dump(by_alias=True))
            ]
        return resources, total

    async def delete_user(
        self, *, tenant_id: str, org_user_id: str
    ) -> bool:
        async with self._sf() as session:
            await _set_tenant(session, tenant_id)
            result = await session.execute(
                sa.delete(org_users).where(
                    org_users.c.tenant_id == tenant_id,
                    org_users.c.org_user_id == org_user_id,
                )
            )
            await session.commit()
            return bool(result.rowcount)

    async def soft_deactivate_user(
        self, *, tenant_id: str, org_user_id: str
    ) -> Optional[SCIMUserResource]:
        async with self._sf() as session:
            await _set_tenant(session, tenant_id)
            await session.execute(
                sa.update(org_users)
                .where(
                    org_users.c.tenant_id == tenant_id,
                    org_users.c.org_user_id == org_user_id,
                )
                .values(active=False)
            )
            await session.commit()
            row = (
                await session.execute(
                    sa.select(org_users).where(
                        org_users.c.tenant_id == tenant_id,
                        org_users.c.org_user_id == org_user_id,
                    )
                )
            ).mappings().first()
        return self._row_to_user_resource(row) if row else None

    async def patch_user(
        self,
        *,
        tenant_id: str,
        org_user_id: str,
        operations: list[tuple[str, Optional[str], Any]],
    ) -> Optional[SCIMUserResource]:
        async with self._sf() as session:
            await _set_tenant(session, tenant_id)
            row = (
                await session.execute(
                    sa.select(org_users).where(
                        org_users.c.tenant_id == tenant_id,
                        org_users.c.org_user_id == org_user_id,
                    )
                )
            ).mappings().first()
            if row is None:
                return None
            current = dict(row)
            scim_meta = dict(current["scim_metadata"] or {})
            for op, path, value in operations:
                column, jsonb_update = _apply_patch_to_user(
                    current, scim_meta, op, path, value
                )
                # Apply column edits + scim_meta update in-memory then write below.
                if column is not None and column != "scim_metadata":
                    current[column] = jsonb_update
            current["scim_metadata"] = scim_meta
            update_values = {
                k: current[k]
                for k in (
                    "user_name",
                    "display_name",
                    "email",
                    "active",
                    "department",
                    "team",
                    "region",
                    "location",
                    "role",
                    "title",
                    "manager_org_user_id",
                    "scim_metadata",
                    "external_ids",
                )
            }
            await session.execute(
                sa.update(org_users)
                .where(
                    org_users.c.tenant_id == tenant_id,
                    org_users.c.org_user_id == org_user_id,
                )
                .values(**update_values)
            )
            await session.commit()
            row = (
                await session.execute(
                    sa.select(org_users).where(
                        org_users.c.tenant_id == tenant_id,
                        org_users.c.org_user_id == org_user_id,
                    )
                )
            ).mappings().first()
        return self._row_to_user_resource(row) if row else None

    # ── Group CRUD ──────────────────────────────────────────────

    async def create_or_replace_group(
        self,
        *,
        tenant_id: str,
        payload: SCIMGroupResource,
    ) -> SCIMGroupResource:
        external_id = (payload.externalId or payload.displayName).strip()
        if not external_id:
            raise SCIMError(
                status=400,
                detail="displayName or externalId required",
                scim_type="invalidValue",
            )
        now = _now()
        group_kind = _detect_group_kind(payload.displayName)
        async with self._sf() as session:
            await _set_tenant(session, tenant_id)
            existing = (
                await session.execute(
                    sa.select(org_groups).where(
                        org_groups.c.tenant_id == tenant_id,
                        org_groups.c.external_id == external_id,
                    )
                )
            ).mappings().first()
            row_id = (
                existing["org_group_id"] if existing else uuid.uuid4().hex
            )
            stmt = pg_insert(org_groups).values(
                org_group_id=row_id,
                tenant_id=tenant_id,
                external_id=external_id,
                display_name=payload.displayName[:256],
                description=None,
                group_kind=group_kind,
                metadata_json={},
                created_at=now,
                updated_at=now,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[
                    org_groups.c.tenant_id,
                    org_groups.c.external_id,
                ],
                set_={
                    "display_name": stmt.excluded.display_name,
                    "group_kind": stmt.excluded.group_kind,
                    "updated_at": stmt.excluded.updated_at,
                },
            ).returning(org_groups)
            row = (await session.execute(stmt)).mappings().first()
            if row is None:
                raise SCIMError(status=500, detail="group upsert failed")
            await self._replace_members(
                session=session,
                tenant_id=tenant_id,
                org_group_id=row["org_group_id"],
                members=payload.members,
                now=now,
            )
            await session.commit()
        return await self._build_group_resource(
            tenant_id=tenant_id, org_group_id=row["org_group_id"]
        )

    async def get_group(
        self, *, tenant_id: str, org_group_id: str
    ) -> Optional[SCIMGroupResource]:
        async with self._sf() as session:
            await _set_tenant(session, tenant_id)
            row = (
                await session.execute(
                    sa.select(org_groups).where(
                        org_groups.c.tenant_id == tenant_id,
                        org_groups.c.org_group_id == org_group_id,
                    )
                )
            ).mappings().first()
            if row is None:
                return None
            members = (
                await session.execute(
                    sa.select(org_user_groups.c.org_user_id).where(
                        org_user_groups.c.tenant_id == tenant_id,
                        org_user_groups.c.org_group_id == org_group_id,
                    )
                )
            ).scalars().all()
        return SCIMGroupResource(
            schemas=[GROUP_SCHEMA_URN],
            id=row["org_group_id"],
            externalId=row["external_id"],
            displayName=row["display_name"],
            members=[
                SCIMGroupMember(value=mid, type="User") for mid in members
            ],
            meta=SCIMMeta(
                resourceType="Group",
                created=row["created_at"],
                lastModified=row["updated_at"],
                location=f"{self._base_location}/Groups/{row['org_group_id']}",
            ),
        )

    async def list_groups(
        self,
        *,
        tenant_id: str,
        scim_filter: Optional[SCIMFilter],
        start_index: int,
        count: int,
    ) -> tuple[list[SCIMGroupResource], int]:
        async with self._sf() as session:
            await _set_tenant(session, tenant_id)
            stmt = sa.select(org_groups).where(
                org_groups.c.tenant_id == tenant_id
            )
            clauses = []
            if scim_filter is not None and scim_filter.clauses:
                for clause in scim_filter.clauses:
                    if clause.attribute == "displayName" and clause.operator == "eq":
                        clauses.append(org_groups.c.display_name == clause.value)
                    elif clause.attribute == "externalId" and clause.operator == "eq":
                        clauses.append(org_groups.c.external_id == clause.value)
            if clauses:
                stmt = stmt.where(*clauses)
            total = int(
                (
                    await session.execute(
                        sa.select(sa.func.count()).select_from(
                            stmt.subquery()
                        )
                    )
                ).scalar_one()
            )
            stmt = (
                stmt.order_by(org_groups.c.display_name.asc())
                .offset(max(0, start_index - 1))
                .limit(max(1, min(count, 1000)))
            )
            rows = (await session.execute(stmt)).mappings().all()
        resources: list[SCIMGroupResource] = []
        for row in rows:
            r = await self.get_group(
                tenant_id=tenant_id, org_group_id=row["org_group_id"]
            )
            if r is not None:
                resources.append(r)
        return resources, total

    async def delete_group(
        self, *, tenant_id: str, org_group_id: str
    ) -> bool:
        async with self._sf() as session:
            await _set_tenant(session, tenant_id)
            result = await session.execute(
                sa.delete(org_groups).where(
                    org_groups.c.tenant_id == tenant_id,
                    org_groups.c.org_group_id == org_group_id,
                )
            )
            await session.commit()
            return bool(result.rowcount)

    async def patch_group(
        self,
        *,
        tenant_id: str,
        org_group_id: str,
        operations: list[tuple[str, Optional[str], Any]],
    ) -> Optional[SCIMGroupResource]:
        existing = await self.get_group(
            tenant_id=tenant_id, org_group_id=org_group_id
        )
        if existing is None:
            return None
        new_members = {m.value for m in existing.members}
        new_name = existing.displayName
        for op, path, value in operations:
            if path is None and isinstance(value, dict):
                if "displayName" in value:
                    new_name = str(value["displayName"])
                if "members" in value:
                    members_value = value["members"]
                    if op == "replace":
                        new_members = {
                            m["value"] for m in members_value if isinstance(m, dict)
                        }
                    elif op == "add":
                        new_members.update(
                            m["value"]
                            for m in members_value
                            if isinstance(m, dict)
                        )
                    elif op == "remove":
                        for m in members_value:
                            if isinstance(m, dict):
                                new_members.discard(m["value"])
            elif path == "members":
                if op == "remove":
                    new_members = set()
                elif op == "replace" and isinstance(value, list):
                    new_members = {
                        m["value"] for m in value if isinstance(m, dict)
                    }
                elif op == "add" and isinstance(value, list):
                    new_members.update(
                        m["value"] for m in value if isinstance(m, dict)
                    )
            elif path == "displayName":
                new_name = str(value)

        return await self.create_or_replace_group(
            tenant_id=tenant_id,
            payload=SCIMGroupResource(
                schemas=[GROUP_SCHEMA_URN],
                id=org_group_id,
                externalId=existing.externalId or new_name,
                displayName=new_name,
                members=[
                    SCIMGroupMember(value=v, type="User")
                    for v in sorted(new_members)
                ],
            ),
        )

    # ── Internals ───────────────────────────────────────────────

    async def _replace_members(
        self,
        *,
        session: AsyncSession,
        tenant_id: str,
        org_group_id: str,
        members: Iterable[SCIMGroupMember],
        now: datetime,
    ) -> None:
        await session.execute(
            sa.delete(org_user_groups).where(
                org_user_groups.c.tenant_id == tenant_id,
                org_user_groups.c.org_group_id == org_group_id,
            )
        )
        for m in members:
            value = (m.value or "").strip()
            if not value:
                continue
            user_id = await self._resolve_member_user_id(session, tenant_id, value)
            if user_id is None:
                logger.info(
                    "scim.skip_unknown_member group=%s user_ref=%s",
                    org_group_id, value,
                )
                continue
            await session.execute(
                pg_insert(org_user_groups).values(
                    tenant_id=tenant_id,
                    org_user_id=user_id,
                    org_group_id=org_group_id,
                    created_at=now,
                ).on_conflict_do_nothing()
            )

    async def _resolve_member_user_id(
        self,
        session: AsyncSession,
        tenant_id: str,
        ref_value: str,
    ) -> Optional[str]:
        # Try as our internal org_user_id first.
        row = (
            await session.execute(
                sa.select(org_users.c.org_user_id).where(
                    org_users.c.tenant_id == tenant_id,
                    org_users.c.org_user_id == ref_value,
                )
            )
        ).first()
        if row is not None:
            return row[0]
        # Fall back to external_id.
        row = (
            await session.execute(
                sa.select(org_users.c.org_user_id).where(
                    org_users.c.tenant_id == tenant_id,
                    org_users.c.external_id == ref_value,
                )
            )
        ).first()
        return row[0] if row else None

    async def _resolve_manager(
        self,
        tenant_id: str,
        enterprise: Optional[SCIMEnterpriseUser],
    ) -> Optional[str]:
        if enterprise is None or not enterprise.manager:
            return None
        ref = (enterprise.manager.get("value") or "").strip()
        if not ref:
            return None
        async with self._sf() as session:
            await _set_tenant(session, tenant_id)
            return await self._resolve_member_user_id(session, tenant_id, ref)

    async def _fetch_user_by_external(
        self,
        session: AsyncSession,
        tenant_id: str,
        external_id: str,
    ) -> Optional[dict[str, Any]]:
        row = (
            await session.execute(
                sa.select(org_users).where(
                    org_users.c.tenant_id == tenant_id,
                    org_users.c.external_id == external_id,
                )
            )
        ).mappings().first()
        return dict(row) if row else None

    async def _build_group_resource(
        self, *, tenant_id: str, org_group_id: str
    ) -> SCIMGroupResource:
        resource = await self.get_group(
            tenant_id=tenant_id, org_group_id=org_group_id
        )
        if resource is None:
            raise SCIMError(status=500, detail="group not found after upsert")
        return resource

    def _row_to_user_resource(self, row: Any) -> SCIMUserResource:
        if row is None:
            raise SCIMError(status=500, detail="empty user row")
        scim_metadata = dict(row["scim_metadata"] or {})
        emails: list[SCIMEmail] = []
        if row["email"]:
            emails.append(SCIMEmail(value=row["email"], primary=True, type="work"))
        name_obj: Optional[SCIMName] = None
        scim_name = scim_metadata.get("name")
        if isinstance(scim_name, dict):
            name_obj = SCIMName(**{
                k: v for k, v in scim_name.items() if k in SCIMName.model_fields
            })
        enterprise = None
        ent = scim_metadata.get("enterprise") or {}
        if row["department"] or row["manager_org_user_id"] or ent:
            enterprise = SCIMEnterpriseUser(
                department=row["department"],
                organization=ent.get("organization") if isinstance(ent, dict) else None,
                division=ent.get("division") if isinstance(ent, dict) else None,
                manager=(
                    {"value": row["manager_org_user_id"]}
                    if row["manager_org_user_id"]
                    else None
                ),
            )
        return SCIMUserResource(
            schemas=[
                USER_SCHEMA_URN,
                *(
                    [ENTERPRISE_SCHEMA_URN]
                    if enterprise is not None
                    else []
                ),
            ],
            id=row["org_user_id"],
            externalId=row["external_id"],
            userName=row["user_name"],
            displayName=row["display_name"],
            title=row["title"],
            active=bool(row["active"]),
            emails=emails,
            name=name_obj,
            enterpriseUser=enterprise,
            meta=SCIMMeta(
                resourceType="User",
                created=row["created_at"],
                lastModified=row["updated_at"],
                location=f"{self._base_location}/Users/{row['org_user_id']}",
                version=f"W/\"{row['version']}\"",
            ),
        )


# ── Helpers ─────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _set_tenant(session: AsyncSession, tenant_id: str) -> None:
    await session.execute(
        sa.text("SELECT set_config('nexus.current_tenant_id', :tid, true)"),
        {"tid": tenant_id},
    )


def _user_to_row(
    *,
    tenant_id: str,
    org_user_id: str,
    external_id: str,
    payload: SCIMUserResource,
    manager_org_user_id: Optional[str],
    now: datetime,
) -> dict[str, Any]:
    enterprise = payload.enterpriseUser
    department = (enterprise.department if enterprise else None)
    scim_metadata: dict[str, Any] = {
        "raw_schemas": list(payload.schemas),
    }
    if payload.name is not None:
        scim_metadata["name"] = payload.name.model_dump(exclude_none=True)
    if enterprise is not None:
        scim_metadata["enterprise"] = enterprise.model_dump(exclude_none=True)
    if payload.locale:
        scim_metadata["locale"] = payload.locale
    if payload.timezone:
        scim_metadata["timezone"] = payload.timezone
    if payload.addresses:
        scim_metadata["addresses"] = payload.addresses

    return {
        "org_user_id": org_user_id,
        "tenant_id": tenant_id,
        "external_id": external_id,
        "user_name": payload.userName[:256],
        "display_name": payload.displayName[:256] if payload.displayName else None,
        "email": payload.primary_email(),
        "active": bool(payload.active),
        "department": department[:128] if department else None,
        "team": None,
        "region": None,
        "location": None,
        "role": payload.title[:128] if payload.title else None,
        "title": payload.title[:256] if payload.title else None,
        "hire_date": None,
        "manager_org_user_id": manager_org_user_id,
        "jurisdiction": None,
        "external_ids": {},
        "scim_metadata": scim_metadata,
        "created_at": now,
        "updated_at": now,
        "version": 1,
    }


def _split_filter_for_users(
    f: Optional[SCIMFilter],
) -> tuple[list[sa.ColumnElement], Optional[SCIMFilter]]:
    """Split filter clauses into SQL-able ones + leftover for Python eval."""
    if f is None or not f.clauses:
        return [], None
    if f.connective == "or":
        # 'or' is rare in SCIM clients; we fall back to Python eval.
        return [], f
    sql_clauses: list[sa.ColumnElement] = []
    leftover: list = []
    for clause in f.clauses:
        attr = clause.attribute.split(":")[-1]
        col = _USER_FILTER_COLUMNS.get(attr)
        if col is None or clause.value is None:
            leftover.append(clause)
            continue
        if clause.operator == "eq":
            sql_clauses.append(col == clause.value)
        elif clause.operator == "ne":
            sql_clauses.append(col != clause.value)
        elif clause.operator == "sw":
            sql_clauses.append(col.ilike(clause.value + "%"))
        elif clause.operator == "co":
            sql_clauses.append(col.ilike(f"%{clause.value}%"))
        else:
            leftover.append(clause)
    if not leftover:
        return sql_clauses, None
    return sql_clauses, SCIMFilter(
        clauses=tuple(leftover), connective=f.connective
    )


_USER_FILTER_COLUMNS = {
    "userName": org_users.c.user_name,
    "externalId": org_users.c.external_id,
    "displayName": org_users.c.display_name,
    "emails.value": org_users.c.email,
    "active": org_users.c.active,
    "title": org_users.c.title,
}


def _apply_patch_to_user(
    current: dict[str, Any],
    scim_metadata: dict[str, Any],
    op: str,
    path: Optional[str],
    value: Any,
) -> tuple[Optional[str], Any]:
    """Apply one PatchOp to the user row dict + scim_metadata.

    Returns ``(column_name_changed, new_value)`` or ``(None, None)`` if
    only ``scim_metadata`` was updated. Unknown paths are written into
    ``scim_metadata`` so we don't lose data the IDP cares about.
    """
    if path is None:
        if not isinstance(value, dict):
            return None, None
        for k, v in value.items():
            _apply_patch_to_user(current, scim_metadata, op, k, v)
        return None, None

    bare = path.split(":")[-1]
    if bare == "active":
        if op == "remove":
            current["active"] = False
            return "active", False
        current["active"] = bool(value)
        return "active", bool(value)
    if bare == "userName":
        current["user_name"] = str(value)[:256] if op != "remove" else current["user_name"]
        return "user_name", current["user_name"]
    if bare == "displayName":
        current["display_name"] = None if op == "remove" else str(value)[:256]
        return "display_name", current["display_name"]
    if bare == "title":
        current["title"] = None if op == "remove" else str(value)[:256]
        return "title", current["title"]
    if bare in ("emails", "emails.value"):
        if op == "remove":
            current["email"] = None
        else:
            email = None
            if isinstance(value, list) and value:
                first = value[0]
                if isinstance(first, dict):
                    email = first.get("value")
                else:
                    email = str(first)
            elif isinstance(value, str):
                email = value
            current["email"] = email
        return "email", current["email"]
    if bare in ("department", "enterpriseUser.department"):
        current["department"] = (
            None if op == "remove" else str(value)[:128] if value else None
        )
        return "department", current["department"]
    # Unknown path → keep in scim_metadata for round-trip fidelity.
    scim_metadata.setdefault("_unmodeled", {})[path] = (
        None if op == "remove" else value
    )
    return None, None


def _detect_group_kind(name: str) -> str:
    norm = (name or "").lower()
    if any(kw in norm for kw in ("dept", "department")):
        return "department"
    if any(kw in norm for kw in ("team",)):
        return "team"
    if any(kw in norm for kw in ("region", "geo")):
        return "region"
    if any(kw in norm for kw in ("role", "title")):
        return "role"
    return "custom"
