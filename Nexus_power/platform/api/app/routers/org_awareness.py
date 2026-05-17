"""Org-awareness admin endpoints — channel policies, subscriptions, gaps.

These complement the SCIM router; SCIM owns the directory (users +
groups) while this module owns the *behaviour* that the directory
drives: who gets DMed for what topic, which channels are muted, and
which questions still don't have answers.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import get_current_user
from ..database import require_db

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Org Awareness"], prefix="/api/v1/org")


# ── Schema projections ─────────────────────────────────────────


_md = sa.MetaData()


topic_subscriptions = sa.Table(
    "topic_subscriptions",
    _md,
    sa.Column("subscription_id", sa.String(64), primary_key=True),
    sa.Column("tenant_id", sa.String(64), nullable=False),
    sa.Column("org_user_id", sa.String(64), nullable=False),
    sa.Column("subscription_kind", sa.String(32), nullable=False),
    sa.Column("target_id", sa.String(256), nullable=False),
    sa.Column("mode", sa.String(32), nullable=False),
    sa.Column("delivery_surface", sa.String(32), nullable=False),
    sa.Column("delivery_address", sa.String(256)),
    sa.Column("bootstrap_source", sa.String(32), nullable=False),
    sa.Column("metadata_json", JSONB, nullable=False),
    sa.Column("active", sa.Boolean, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)

knowledge_gaps = sa.Table(
    "knowledge_gaps",
    _md,
    sa.Column("gap_id", sa.String(64), primary_key=True),
    sa.Column("tenant_id", sa.String(64), nullable=False),
    sa.Column("topic_hash", sa.String(64), nullable=False),
    sa.Column("topic_label", sa.String(256), nullable=False),
    sa.Column("topic_summary", sa.Text, nullable=False),
    sa.Column("question_count", sa.Integer, nullable=False),
    sa.Column("unique_askers_count", sa.Integer, nullable=False),
    sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("product_ids", ARRAY(sa.String(64)), nullable=False),
    sa.Column("suggested_sme_ids", ARRAY(sa.String(128)), nullable=False),
    sa.Column("status", sa.String(16), nullable=False),
    sa.Column("metadata_json", JSONB, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("version", sa.Integer, nullable=False),
)

surface_channel_policies = sa.Table(
    "surface_channel_policies",
    _md,
    sa.Column("tenant_id", sa.String(64), primary_key=True),
    sa.Column("surface", sa.String(32), primary_key=True),
    sa.Column("channel_id_ext", sa.String(128), primary_key=True),
    sa.Column("echo_mode", sa.String(16), nullable=False),
    sa.Column("min_confidence_override", sa.Float),
    sa.Column("allowlist_json", JSONB, nullable=False),
    sa.Column("blocklist_json", JSONB, nullable=False),
    sa.Column("quiet_hours_json", JSONB, nullable=False),
    sa.Column("metadata_json", JSONB, nullable=False),
    sa.Column("updated_by", sa.String(128)),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)

org_users = sa.Table(
    "org_users",
    _md,
    sa.Column("org_user_id", sa.String(64), primary_key=True),
    sa.Column("tenant_id", sa.String(64), nullable=False),
    sa.Column("user_name", sa.String(256), nullable=False),
    sa.Column("display_name", sa.String(256)),
    sa.Column("email", sa.String(256)),
    sa.Column("active", sa.Boolean, nullable=False),
    sa.Column("department", sa.String(128)),
    sa.Column("team", sa.String(128)),
    sa.Column("region", sa.String(128)),
    sa.Column("role", sa.String(128)),
    sa.Column("manager_org_user_id", sa.String(64)),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)


# ── Helpers ────────────────────────────────────────────────────


_PRIVILEGED = frozenset({"admin", "manager"})


def _require_priv(user: dict) -> None:
    if user.get("role", "viewer") not in _PRIVILEGED:
        raise HTTPException(403, "admin or manager required")


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _set_tenant(session: AsyncSession, tenant_id: str) -> None:
    await session.execute(
        sa.text("SELECT set_config('nexus.current_tenant_id', :tid, true)"),
        {"tid": tenant_id},
    )


# ── DTOs — Subscriptions ───────────────────────────────────────


class SubscriptionOut(BaseModel):
    subscription_id: str
    org_user_id: str
    subscription_kind: str
    target_id: str
    mode: str
    delivery_surface: str
    delivery_address: Optional[str] = None
    bootstrap_source: str
    active: bool
    metadata: dict[str, Any]
    created_at: str
    updated_at: str


class CreateSubscriptionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    org_user_id: str = Field(min_length=1, max_length=64)
    subscription_kind: str
    target_id: str = Field(min_length=1, max_length=256)
    mode: str = "all"
    delivery_surface: str = "slack"
    delivery_address: Optional[str] = Field(default=None, max_length=256)
    bootstrap_source: str = "manual"
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("subscription_kind")
    @classmethod
    def _kind(cls, v: str) -> str:
        if v not in ("topic", "product", "card", "jurisdiction", "channel"):
            raise ValueError(f"invalid subscription_kind: {v}")
        return v

    @field_validator("mode")
    @classmethod
    def _mode(cls, v: str) -> str:
        if v not in ("all", "high_confidence_only", "mute"):
            raise ValueError(f"invalid mode: {v}")
        return v

    @field_validator("delivery_surface")
    @classmethod
    def _surface(cls, v: str) -> str:
        if v not in ("slack", "teams", "email", "webhook"):
            raise ValueError(f"invalid delivery_surface: {v}")
        return v

    @field_validator("bootstrap_source")
    @classmethod
    def _bootstrap(cls, v: str) -> str:
        if v not in ("manual", "role", "team", "manager_chain", "sme", "admin"):
            raise ValueError(f"invalid bootstrap_source: {v}")
        return v


# ── DTOs — Knowledge Gaps ──────────────────────────────────────


class KnowledgeGapOut(BaseModel):
    gap_id: str
    topic_hash: str
    topic_label: str
    topic_summary: str
    question_count: int
    unique_askers_count: int
    product_ids: list[str]
    suggested_sme_ids: list[str]
    status: str
    metadata: dict[str, Any]
    first_seen_at: str
    last_seen_at: str
    created_at: str
    updated_at: str


class GapStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str
    suggested_sme_ids: Optional[list[str]] = None

    @field_validator("status")
    @classmethod
    def _status(cls, v: str) -> str:
        if v not in ("open", "addressed", "scheduled", "archived"):
            raise ValueError(f"invalid status: {v}")
        return v


# ── DTOs — Channel Policies ────────────────────────────────────


class ChannelPolicyOut(BaseModel):
    surface: str
    channel_id_ext: str
    echo_mode: str
    min_confidence_override: Optional[float] = None
    allowlist: dict[str, Any]
    blocklist: dict[str, Any]
    quiet_hours: dict[str, Any]
    metadata: dict[str, Any]
    updated_by: Optional[str] = None
    created_at: str
    updated_at: str


class UpsertChannelPolicyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    surface: str
    channel_id_ext: str = Field(min_length=1, max_length=128)
    echo_mode: str = "inherit"
    min_confidence_override: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    allowlist: dict[str, Any] = Field(default_factory=dict)
    blocklist: dict[str, Any] = Field(default_factory=dict)
    quiet_hours: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("echo_mode")
    @classmethod
    def _mode(cls, v: str) -> str:
        if v not in ("live", "dm_only", "shadow", "muted", "inherit"):
            raise ValueError(f"invalid echo_mode: {v}")
        return v

    @field_validator("surface")
    @classmethod
    def _surface(cls, v: str) -> str:
        if v not in ("slack", "teams", "email", "webhook"):
            raise ValueError(f"invalid surface: {v}")
        return v


# ── Subscriptions endpoints ────────────────────────────────────


@router.get("/subscriptions", response_model=list[SubscriptionOut])
async def list_subscriptions(
    org_user_id: Optional[str] = None,
    subscription_kind: Optional[str] = None,
    active_only: bool = True,
    limit: int = 200,
    user: dict = Depends(get_current_user),
) -> list[SubscriptionOut]:
    factory = require_db()
    tenant_id = user["tenant_id"]
    limit = max(1, min(1000, int(limit)))
    async with factory() as session:
        await _set_tenant(session, tenant_id)
        stmt = sa.select(topic_subscriptions).where(
            topic_subscriptions.c.tenant_id == tenant_id,
        )
        if org_user_id:
            stmt = stmt.where(
                topic_subscriptions.c.org_user_id == org_user_id
            )
        if subscription_kind:
            stmt = stmt.where(
                topic_subscriptions.c.subscription_kind == subscription_kind
            )
        if active_only:
            stmt = stmt.where(topic_subscriptions.c.active == True)  # noqa: E712
        stmt = stmt.order_by(
            topic_subscriptions.c.created_at.desc()
        ).limit(limit)
        rows = (await session.execute(stmt)).mappings().all()
    return [_subscription_to_out(r) for r in rows]


@router.post(
    "/subscriptions", response_model=SubscriptionOut, status_code=201
)
async def create_subscription(
    body: CreateSubscriptionRequest,
    user: dict = Depends(get_current_user),
) -> SubscriptionOut:
    _require_priv(user)
    factory = require_db()
    tenant_id = user["tenant_id"]
    now = _now()
    async with factory() as session:
        await _set_tenant(session, tenant_id)
        # Verify the user exists in the org directory.
        existing_user = (
            await session.execute(
                sa.select(org_users.c.org_user_id).where(
                    org_users.c.tenant_id == tenant_id,
                    org_users.c.org_user_id == body.org_user_id,
                )
            )
        ).first()
        if existing_user is None:
            raise HTTPException(404, "org_user_not_found")
        stmt = pg_insert(topic_subscriptions).values(
            subscription_id=uuid.uuid4().hex,
            tenant_id=tenant_id,
            org_user_id=body.org_user_id,
            subscription_kind=body.subscription_kind,
            target_id=body.target_id,
            mode=body.mode,
            delivery_surface=body.delivery_surface,
            delivery_address=body.delivery_address,
            bootstrap_source=body.bootstrap_source,
            metadata_json=body.metadata,
            active=True,
            created_at=now,
            updated_at=now,
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_sub_quad",
            set_={
                "mode": stmt.excluded.mode,
                "delivery_surface": stmt.excluded.delivery_surface,
                "delivery_address": stmt.excluded.delivery_address,
                "metadata_json": stmt.excluded.metadata_json,
                "active": True,
                "updated_at": stmt.excluded.updated_at,
            },
        ).returning(topic_subscriptions)
        row = (await session.execute(stmt)).mappings().first()
        await session.commit()
    if row is None:
        raise HTTPException(500, "subscription_upsert_failed")
    return _subscription_to_out(row)


@router.delete("/subscriptions/{subscription_id}", status_code=204)
async def delete_subscription(
    subscription_id: str,
    user: dict = Depends(get_current_user),
) -> None:
    _require_priv(user)
    factory = require_db()
    tenant_id = user["tenant_id"]
    async with factory() as session:
        await _set_tenant(session, tenant_id)
        result = await session.execute(
            sa.update(topic_subscriptions)
            .where(
                topic_subscriptions.c.tenant_id == tenant_id,
                topic_subscriptions.c.subscription_id == subscription_id,
            )
            .values(active=False, updated_at=_now())
        )
        if result.rowcount == 0:
            raise HTTPException(404, "subscription_not_found")
        await session.commit()


# ── Knowledge Gaps endpoints ────────────────────────────────────


@router.get("/gaps", response_model=list[KnowledgeGapOut])
async def list_gaps(
    status: Optional[str] = "open",
    limit: int = 100,
    user: dict = Depends(get_current_user),
) -> list[KnowledgeGapOut]:
    factory = require_db()
    tenant_id = user["tenant_id"]
    limit = max(1, min(500, int(limit)))
    async with factory() as session:
        await _set_tenant(session, tenant_id)
        stmt = sa.select(knowledge_gaps).where(
            knowledge_gaps.c.tenant_id == tenant_id
        )
        if status:
            stmt = stmt.where(knowledge_gaps.c.status == status)
        stmt = stmt.order_by(
            knowledge_gaps.c.last_seen_at.desc()
        ).limit(limit)
        rows = (await session.execute(stmt)).mappings().all()
    return [_gap_to_out(r) for r in rows]


@router.get("/gaps/{gap_id}", response_model=KnowledgeGapOut)
async def get_gap(
    gap_id: str,
    user: dict = Depends(get_current_user),
) -> KnowledgeGapOut:
    factory = require_db()
    tenant_id = user["tenant_id"]
    async with factory() as session:
        await _set_tenant(session, tenant_id)
        row = (
            await session.execute(
                sa.select(knowledge_gaps).where(
                    knowledge_gaps.c.tenant_id == tenant_id,
                    knowledge_gaps.c.gap_id == gap_id,
                )
            )
        ).mappings().first()
    if row is None:
        raise HTTPException(404, "gap_not_found")
    return _gap_to_out(row)


@router.post("/gaps/{gap_id}/status", response_model=KnowledgeGapOut)
async def set_gap_status(
    gap_id: str,
    body: GapStatusRequest,
    user: dict = Depends(get_current_user),
) -> KnowledgeGapOut:
    _require_priv(user)
    factory = require_db()
    tenant_id = user["tenant_id"]
    async with factory() as session:
        await _set_tenant(session, tenant_id)
        values = {"status": body.status}
        if body.suggested_sme_ids is not None:
            values["suggested_sme_ids"] = body.suggested_sme_ids
        result = await session.execute(
            sa.update(knowledge_gaps)
            .where(
                knowledge_gaps.c.tenant_id == tenant_id,
                knowledge_gaps.c.gap_id == gap_id,
            )
            .values(**values)
        )
        if result.rowcount == 0:
            raise HTTPException(404, "gap_not_found")
        await session.commit()
        row = (
            await session.execute(
                sa.select(knowledge_gaps).where(
                    knowledge_gaps.c.tenant_id == tenant_id,
                    knowledge_gaps.c.gap_id == gap_id,
                )
            )
        ).mappings().first()
    if row is None:
        raise HTTPException(500, "gap_lost")
    return _gap_to_out(row)


# ── Channel Policies endpoints ──────────────────────────────────


@router.get("/policies", response_model=list[ChannelPolicyOut])
async def list_policies(
    surface: Optional[str] = None,
    user: dict = Depends(get_current_user),
) -> list[ChannelPolicyOut]:
    factory = require_db()
    tenant_id = user["tenant_id"]
    async with factory() as session:
        await _set_tenant(session, tenant_id)
        stmt = sa.select(surface_channel_policies).where(
            surface_channel_policies.c.tenant_id == tenant_id
        )
        if surface:
            stmt = stmt.where(surface_channel_policies.c.surface == surface)
        stmt = stmt.order_by(
            surface_channel_policies.c.surface.asc(),
            surface_channel_policies.c.channel_id_ext.asc(),
        )
        rows = (await session.execute(stmt)).mappings().all()
    return [_policy_to_out(r) for r in rows]


@router.put("/policies", response_model=ChannelPolicyOut)
async def upsert_policy(
    body: UpsertChannelPolicyRequest,
    user: dict = Depends(get_current_user),
) -> ChannelPolicyOut:
    _require_priv(user)
    factory = require_db()
    tenant_id = user["tenant_id"]
    now = _now()
    async with factory() as session:
        await _set_tenant(session, tenant_id)
        stmt = pg_insert(surface_channel_policies).values(
            tenant_id=tenant_id,
            surface=body.surface,
            channel_id_ext=body.channel_id_ext,
            echo_mode=body.echo_mode,
            min_confidence_override=body.min_confidence_override,
            allowlist_json=body.allowlist,
            blocklist_json=body.blocklist,
            quiet_hours_json=body.quiet_hours,
            metadata_json=body.metadata,
            updated_by=user.get("user_id"),
            created_at=now,
            updated_at=now,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[
                surface_channel_policies.c.tenant_id,
                surface_channel_policies.c.surface,
                surface_channel_policies.c.channel_id_ext,
            ],
            set_={
                "echo_mode": stmt.excluded.echo_mode,
                "min_confidence_override": stmt.excluded.min_confidence_override,
                "allowlist_json": stmt.excluded.allowlist_json,
                "blocklist_json": stmt.excluded.blocklist_json,
                "quiet_hours_json": stmt.excluded.quiet_hours_json,
                "metadata_json": stmt.excluded.metadata_json,
                "updated_by": stmt.excluded.updated_by,
                "updated_at": stmt.excluded.updated_at,
            },
        ).returning(surface_channel_policies)
        row = (await session.execute(stmt)).mappings().first()
        await session.commit()
    if row is None:
        raise HTTPException(500, "policy_upsert_failed")
    return _policy_to_out(row)


@router.delete(
    "/policies/{surface}/{channel_id_ext}", status_code=204
)
async def delete_policy(
    surface: str,
    channel_id_ext: str,
    user: dict = Depends(get_current_user),
) -> None:
    _require_priv(user)
    factory = require_db()
    tenant_id = user["tenant_id"]
    async with factory() as session:
        await _set_tenant(session, tenant_id)
        result = await session.execute(
            sa.delete(surface_channel_policies).where(
                surface_channel_policies.c.tenant_id == tenant_id,
                surface_channel_policies.c.surface == surface,
                surface_channel_policies.c.channel_id_ext == channel_id_ext,
            )
        )
        if result.rowcount == 0:
            raise HTTPException(404, "policy_not_found")
        await session.commit()


# ── Mappers ────────────────────────────────────────────────────


def _subscription_to_out(row) -> SubscriptionOut:
    return SubscriptionOut(
        subscription_id=row["subscription_id"],
        org_user_id=row["org_user_id"],
        subscription_kind=row["subscription_kind"],
        target_id=row["target_id"],
        mode=row["mode"],
        delivery_surface=row["delivery_surface"],
        delivery_address=row["delivery_address"],
        bootstrap_source=row["bootstrap_source"],
        active=bool(row["active"]),
        metadata=dict(row["metadata_json"] or {}),
        created_at=row["created_at"].isoformat(),
        updated_at=row["updated_at"].isoformat(),
    )


def _gap_to_out(row) -> KnowledgeGapOut:
    return KnowledgeGapOut(
        gap_id=row["gap_id"],
        topic_hash=row["topic_hash"],
        topic_label=row["topic_label"],
        topic_summary=row["topic_summary"],
        question_count=int(row["question_count"]),
        unique_askers_count=int(row["unique_askers_count"]),
        product_ids=list(row["product_ids"] or []),
        suggested_sme_ids=list(row["suggested_sme_ids"] or []),
        status=row["status"],
        metadata=dict(row["metadata_json"] or {}),
        first_seen_at=row["first_seen_at"].isoformat(),
        last_seen_at=row["last_seen_at"].isoformat(),
        created_at=row["created_at"].isoformat(),
        updated_at=row["updated_at"].isoformat(),
    )


def _policy_to_out(row) -> ChannelPolicyOut:
    return ChannelPolicyOut(
        surface=row["surface"],
        channel_id_ext=row["channel_id_ext"],
        echo_mode=row["echo_mode"],
        min_confidence_override=(
            float(row["min_confidence_override"])
            if row["min_confidence_override"] is not None
            else None
        ),
        allowlist=dict(row["allowlist_json"] or {}),
        blocklist=dict(row["blocklist_json"] or {}),
        quiet_hours=dict(row["quiet_hours_json"] or {}),
        metadata=dict(row["metadata_json"] or {}),
        updated_by=row["updated_by"],
        created_at=row["created_at"].isoformat(),
        updated_at=row["updated_at"].isoformat(),
    )
