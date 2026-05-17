"""Platform API — Product catalog endpoints.

The product catalog is a tenant-scoped registry of the products an
organisation owns (e.g., ``LT5``, ``WL3``). It powers:

* Product tagging on transcript segments (Phase 1).
* Product entity resolution for cross-demo fusion (Phase 5 Atlas).

Endpoints
---------

* ``GET    /api/v1/products``                      — list active + deprecated
* ``GET    /api/v1/products/{product_id}``         — fetch one
* ``POST   /api/v1/products``                      — create (admin/manager)
* ``PATCH  /api/v1/products/{product_id}``         — update (admin/manager)
* ``DELETE /api/v1/products/{product_id}``         — archive (admin/manager)

Tenant scoping is enforced both at the application layer
(``tenant_id`` filter) and via RLS (the session variable set in
``_set_tenant_context``).
"""
from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Optional

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import get_current_user
from ..database import require_db

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Products"], prefix="/api/v1/products")


# ── Schema projection (matches migration 019) ──────────────────


_md = sa.MetaData()

products_table = sa.Table(
    "products",
    _md,
    sa.Column("product_id", sa.String(64), primary_key=True),
    sa.Column("tenant_id", sa.String(64), nullable=False),
    sa.Column("name", sa.String(256), nullable=False),
    sa.Column("slug", sa.String(128), nullable=False),
    sa.Column("aliases", ARRAY(sa.String(128)), nullable=False),
    sa.Column("description", sa.Text),
    sa.Column("owner_user_id", sa.String(128)),
    sa.Column("status", sa.String(16), nullable=False),
    sa.Column("metadata_json", JSONB, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)


# ── DTOs ────────────────────────────────────────────────────────


_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")
_ALIAS_MAX = 128


class ProductOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_id: str
    tenant_id: str
    name: str
    slug: str
    aliases: list[str]
    description: Optional[str] = None
    owner_user_id: Optional[str] = None
    status: str
    metadata: dict
    created_at: str
    updated_at: str


class CreateProductRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=256)
    slug: str = Field(min_length=1, max_length=128)
    aliases: list[str] = Field(default_factory=list, max_length=32)
    description: Optional[str] = Field(default=None, max_length=4096)
    owner_user_id: Optional[str] = Field(default=None, max_length=128)
    metadata: dict = Field(default_factory=dict)

    @field_validator("slug")
    @classmethod
    def _slug_pattern(cls, v: str) -> str:
        if not _SLUG_RE.match(v):
            raise ValueError(
                "slug must match [a-z0-9][a-z0-9_.-]{0,127}"
            )
        return v

    @field_validator("aliases")
    @classmethod
    def _aliases_clean(cls, v: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for raw in v:
            s = (raw or "").strip()
            if not s:
                continue
            if len(s) > _ALIAS_MAX:
                raise ValueError(f"alias exceeds {_ALIAS_MAX} chars")
            if s.lower() in seen:
                continue
            seen.add(s.lower())
            out.append(s)
        return out


class UpdateProductRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Optional[str] = Field(default=None, max_length=256)
    aliases: Optional[list[str]] = Field(default=None, max_length=32)
    description: Optional[str] = Field(default=None, max_length=4096)
    owner_user_id: Optional[str] = Field(default=None, max_length=128)
    status: Optional[str] = Field(default=None, max_length=16)
    metadata: Optional[dict] = None

    @field_validator("aliases")
    @classmethod
    def _aliases_clean(
        cls, v: Optional[list[str]]
    ) -> Optional[list[str]]:
        if v is None:
            return None
        return CreateProductRequest._aliases_clean(v)  # type: ignore[arg-type]

    @field_validator("status")
    @classmethod
    def _status_valid(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        if v not in ("active", "deprecated", "archived"):
            raise ValueError("status must be active|deprecated|archived")
        return v


# ── Helpers ────────────────────────────────────────────────────


_PRIVILEGED_ROLES = frozenset({"admin", "manager"})


def _require_privileged(user: dict) -> None:
    if user.get("role", "viewer") not in _PRIVILEGED_ROLES:
        raise HTTPException(
            status_code=403,
            detail="product mutations require admin or manager role",
        )


async def _set_tenant_context(
    session: AsyncSession, tenant_id: str
) -> None:
    await session.execute(
        sa.text("SELECT set_config('nexus.current_tenant_id', :tid, true)"),
        {"tid": tenant_id},
    )


def _row_to_out(row) -> ProductOut:
    return ProductOut(
        product_id=row["product_id"],
        tenant_id=row["tenant_id"],
        name=row["name"],
        slug=row["slug"],
        aliases=list(row["aliases"] or []),
        description=row["description"],
        owner_user_id=row["owner_user_id"],
        status=row["status"],
        metadata=row["metadata_json"] or {},
        created_at=row["created_at"].isoformat(),
        updated_at=row["updated_at"].isoformat(),
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── Endpoints ───────────────────────────────────────────────────


@router.get("", response_model=list[ProductOut])
async def list_products(
    include_archived: bool = False,
    user: dict = Depends(get_current_user),
) -> list[ProductOut]:
    factory = require_db()
    tenant_id = user["tenant_id"]
    async with factory() as session:
        await _set_tenant_context(session, tenant_id)
        stmt = sa.select(products_table).where(
            products_table.c.tenant_id == tenant_id
        )
        if not include_archived:
            stmt = stmt.where(products_table.c.status != "archived")
        stmt = stmt.order_by(products_table.c.slug)
        rows = (await session.execute(stmt)).mappings().all()
    return [_row_to_out(r) for r in rows]


@router.get("/{product_id}", response_model=ProductOut)
async def get_product(
    product_id: str,
    user: dict = Depends(get_current_user),
) -> ProductOut:
    factory = require_db()
    tenant_id = user["tenant_id"]
    async with factory() as session:
        await _set_tenant_context(session, tenant_id)
        row = (
            await session.execute(
                sa.select(products_table).where(
                    products_table.c.tenant_id == tenant_id,
                    products_table.c.product_id == product_id,
                )
            )
        ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="product_not_found")
    return _row_to_out(row)


@router.post("", response_model=ProductOut, status_code=201)
async def create_product(
    body: CreateProductRequest,
    user: dict = Depends(get_current_user),
) -> ProductOut:
    _require_privileged(user)
    factory = require_db()
    tenant_id = user["tenant_id"]
    product_id = uuid.uuid4().hex
    now = _now()
    async with factory() as session:
        await _set_tenant_context(session, tenant_id)
        try:
            await session.execute(
                sa.insert(products_table).values(
                    product_id=product_id,
                    tenant_id=tenant_id,
                    name=body.name,
                    slug=body.slug,
                    aliases=body.aliases,
                    description=body.description,
                    owner_user_id=body.owner_user_id,
                    status="active",
                    metadata_json=body.metadata,
                    created_at=now,
                    updated_at=now,
                )
            )
            await session.commit()
        except sa.exc.IntegrityError as exc:
            await session.rollback()
            # Unique violation on (tenant_id, slug)
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "slug_conflict",
                    "slug": body.slug,
                },
            ) from exc
        row = (
            await session.execute(
                sa.select(products_table).where(
                    products_table.c.product_id == product_id,
                )
            )
        ).mappings().first()
    if row is None:
        raise HTTPException(status_code=500, detail="not_persisted")
    return _row_to_out(row)


@router.patch("/{product_id}", response_model=ProductOut)
async def update_product(
    product_id: str,
    body: UpdateProductRequest,
    user: dict = Depends(get_current_user),
) -> ProductOut:
    _require_privileged(user)
    tenant_id = user["tenant_id"]

    update_values: dict = {}
    if body.name is not None:
        update_values["name"] = body.name
    if body.aliases is not None:
        update_values["aliases"] = body.aliases
    if body.description is not None:
        update_values["description"] = body.description
    if body.owner_user_id is not None:
        update_values["owner_user_id"] = body.owner_user_id
    if body.status is not None:
        update_values["status"] = body.status
    if body.metadata is not None:
        update_values["metadata_json"] = body.metadata
    if not update_values:
        # Nothing to do — return current state.
        return await get_product(product_id, user)  # type: ignore[return-value]

    update_values["updated_at"] = _now()
    factory = require_db()
    async with factory() as session:
        await _set_tenant_context(session, tenant_id)
        result = await session.execute(
            sa.update(products_table)
            .where(
                products_table.c.tenant_id == tenant_id,
                products_table.c.product_id == product_id,
            )
            .values(**update_values)
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="product_not_found")
        await session.commit()
        row = (
            await session.execute(
                sa.select(products_table).where(
                    products_table.c.product_id == product_id,
                )
            )
        ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="product_not_found")
    return _row_to_out(row)


@router.delete("/{product_id}", status_code=204)
async def archive_product(
    product_id: str,
    user: dict = Depends(get_current_user),
) -> None:
    """Soft-delete: mark status='archived'. Row retained for traceability."""
    _require_privileged(user)
    factory = require_db()
    tenant_id = user["tenant_id"]
    async with factory() as session:
        await _set_tenant_context(session, tenant_id)
        result = await session.execute(
            sa.update(products_table)
            .where(
                products_table.c.tenant_id == tenant_id,
                products_table.c.product_id == product_id,
            )
            .values(status="archived", updated_at=_now())
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="product_not_found")
        await session.commit()
