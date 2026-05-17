"""Platform API — Knowledge Cards administration endpoints.

Operator surface:

    GET    /api/v1/knowledge-cards                    — list cards
    GET    /api/v1/knowledge-cards/{card_id}          — fetch one
    GET    /api/v1/knowledge-cards/{card_id}/sources  — list sources
    GET    /api/v1/knowledge-cards/{card_id}/history  — change log
    POST   /api/v1/knowledge-cards/{card_id}/promote  — set state=canonical
    POST   /api/v1/knowledge-cards/{card_id}/demote   — re-evaluate after demote
    POST   /api/v1/knowledge-cards/{card_id}/contest  — mark contested
    POST   /api/v1/knowledge-cards/{card_id}/supersede — terminal deprecate
    GET    /api/v1/knowledge-cards/authority          — list role weights
    PUT    /api/v1/knowledge-cards/authority/{role}   — upsert role weight

All endpoints require an authenticated tenant; mutations require
admin or manager role. Reads honor RLS just like every other tenant
table.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
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

router = APIRouter(tags=["Knowledge Cards"], prefix="/api/v1/knowledge-cards")


# ── Schema projections ─────────────────────────────────────────


_md = sa.MetaData()


knowledge_cards = sa.Table(
    "knowledge_cards",
    _md,
    sa.Column("card_id", sa.String(64), primary_key=True),
    sa.Column("tenant_id", sa.String(64), nullable=False),
    sa.Column("topic_slug", sa.String(256), nullable=False),
    sa.Column("topic_label", sa.String(512), nullable=False),
    sa.Column("canonical_statement", sa.Text, nullable=False),
    sa.Column("canonical_confidence", sa.Float, nullable=False),
    sa.Column("consensus_score", sa.Float, nullable=False),
    sa.Column("lifecycle_state", sa.String(16), nullable=False),
    sa.Column("authority_chain", JSONB, nullable=False),
    sa.Column("contributing_count", sa.Integer, nullable=False),
    sa.Column("dissent_count", sa.Integer, nullable=False),
    sa.Column("product_id", sa.String(64)),
    sa.Column("validity_start", sa.Date),
    sa.Column("validity_end", sa.Date),
    sa.Column("jurisdiction", sa.String(64)),
    sa.Column("superseded_by", sa.String(64)),
    sa.Column("halflife_days", sa.Integer, nullable=False),
    sa.Column("last_verified_at", sa.DateTime(timezone=True)),
    sa.Column("verify_due_at", sa.DateTime(timezone=True)),
    sa.Column("backbone_node_id", sa.String(64)),
    sa.Column("tags", ARRAY(sa.String(128)), nullable=False),
    sa.Column("metadata_json", JSONB, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("version", sa.Integer, nullable=False),
)

knowledge_card_sources = sa.Table(
    "knowledge_card_sources",
    _md,
    sa.Column("id", sa.BigInteger, primary_key=True),
    sa.Column("card_id", sa.String(64), nullable=False),
    sa.Column("tenant_id", sa.String(64), nullable=False),
    sa.Column("source_type", sa.String(32), nullable=False),
    sa.Column("source_id", sa.String(64), nullable=False),
    sa.Column("sme_id", sa.String(128)),
    sa.Column("sme_role", sa.String(128)),
    sa.Column("stated_at", sa.Date),
    sa.Column("similarity_to_canonical", sa.Float),
    sa.Column("weight", sa.Float, nullable=False),
    sa.Column("status", sa.String(16), nullable=False),
    sa.Column("metadata_json", JSONB, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)

knowledge_card_history = sa.Table(
    "knowledge_card_history",
    _md,
    sa.Column("id", sa.BigInteger, primary_key=True),
    sa.Column("card_id", sa.String(64), nullable=False),
    sa.Column("tenant_id", sa.String(64), nullable=False),
    sa.Column("change_type", sa.String(32), nullable=False),
    sa.Column("changed_by", sa.String(128)),
    sa.Column("snapshot", JSONB, nullable=False),
    sa.Column("note", sa.String(512)),
    sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
)

tenant_authority_matrix = sa.Table(
    "tenant_authority_matrix",
    _md,
    sa.Column("id", sa.BigInteger, primary_key=True),
    sa.Column("tenant_id", sa.String(64), nullable=False),
    sa.Column("role", sa.String(128), nullable=False),
    sa.Column("weight", sa.Float, nullable=False),
    sa.Column("metadata_json", JSONB, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)


# ── Helpers ────────────────────────────────────────────────────


_PRIVILEGED = frozenset({"admin", "manager"})


def _require_priv(user: dict) -> None:
    if user.get("role", "viewer") not in _PRIVILEGED:
        raise HTTPException(403, "admin or manager required")


async def _set_tenant(session: AsyncSession, tenant_id: str) -> None:
    await session.execute(
        sa.text("SELECT set_config('nexus.current_tenant_id', :tid, true)"),
        {"tid": tenant_id},
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── DTOs ───────────────────────────────────────────────────────


class CardOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    card_id: str
    tenant_id: str
    topic_slug: str
    topic_label: str
    canonical_statement: str
    canonical_confidence: float
    consensus_score: float
    lifecycle_state: str
    authority_chain: list[dict[str, Any]]
    contributing_count: int
    dissent_count: int
    product_id: Optional[str] = None
    validity_start: Optional[str] = None
    validity_end: Optional[str] = None
    jurisdiction: Optional[str] = None
    superseded_by: Optional[str] = None
    halflife_days: int
    last_verified_at: Optional[str] = None
    verify_due_at: Optional[str] = None
    backbone_node_id: Optional[str] = None
    tags: list[str]
    metadata: dict[str, Any]
    version: int
    created_at: str
    updated_at: str


def _to_card_out(row) -> CardOut:
    return CardOut(
        card_id=row["card_id"],
        tenant_id=row["tenant_id"],
        topic_slug=row["topic_slug"],
        topic_label=row["topic_label"],
        canonical_statement=row["canonical_statement"],
        canonical_confidence=float(row["canonical_confidence"]),
        consensus_score=float(row["consensus_score"]),
        lifecycle_state=row["lifecycle_state"],
        authority_chain=list(row["authority_chain"] or []),
        contributing_count=int(row["contributing_count"]),
        dissent_count=int(row["dissent_count"]),
        product_id=row["product_id"],
        validity_start=row["validity_start"].isoformat()
        if row["validity_start"]
        else None,
        validity_end=row["validity_end"].isoformat()
        if row["validity_end"]
        else None,
        jurisdiction=row["jurisdiction"],
        superseded_by=row["superseded_by"],
        halflife_days=int(row["halflife_days"]),
        last_verified_at=row["last_verified_at"].isoformat()
        if row["last_verified_at"]
        else None,
        verify_due_at=row["verify_due_at"].isoformat()
        if row["verify_due_at"]
        else None,
        backbone_node_id=row["backbone_node_id"],
        tags=list(row["tags"] or []),
        metadata=dict(row["metadata_json"] or {}),
        version=int(row["version"]),
        created_at=row["created_at"].isoformat(),
        updated_at=row["updated_at"].isoformat(),
    )


class ContestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=512)


class SupersedeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    superseded_by: str = Field(min_length=1, max_length=64)
    note: Optional[str] = Field(default=None, max_length=512)


class AuthorityWeightOut(BaseModel):
    role: str
    weight: float


class UpsertAuthorityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    weight: float = Field(gt=0.0, le=10.0)

    @field_validator("weight")
    @classmethod
    def _finite(cls, v: float) -> float:
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError("weight must be a finite positive number")
        return v


# ── Card endpoints ─────────────────────────────────────────────


@router.get("", response_model=list[CardOut])
async def list_cards(
    state: Optional[str] = None,
    product_id: Optional[str] = None,
    limit: int = 100,
    user: dict = Depends(get_current_user),
) -> list[CardOut]:
    factory = require_db()
    tenant_id = user["tenant_id"]
    limit = max(1, min(500, int(limit)))
    async with factory() as session:
        await _set_tenant(session, tenant_id)
        stmt = sa.select(knowledge_cards).where(
            knowledge_cards.c.tenant_id == tenant_id
        )
        if state:
            states = [s.strip() for s in state.split(",") if s.strip()]
            if states:
                stmt = stmt.where(
                    knowledge_cards.c.lifecycle_state.in_(states)
                )
        if product_id:
            stmt = stmt.where(knowledge_cards.c.product_id == product_id)
        stmt = stmt.order_by(knowledge_cards.c.updated_at.desc()).limit(limit)
        rows = (await session.execute(stmt)).mappings().all()
    return [_to_card_out(r) for r in rows]


@router.get("/{card_id}", response_model=CardOut)
async def get_card(
    card_id: str,
    user: dict = Depends(get_current_user),
) -> CardOut:
    factory = require_db()
    tenant_id = user["tenant_id"]
    async with factory() as session:
        await _set_tenant(session, tenant_id)
        row = (
            await session.execute(
                sa.select(knowledge_cards).where(
                    knowledge_cards.c.tenant_id == tenant_id,
                    knowledge_cards.c.card_id == card_id,
                )
            )
        ).mappings().first()
    if row is None:
        raise HTTPException(404, "card_not_found")
    return _to_card_out(row)


@router.get("/{card_id}/sources")
async def list_sources(
    card_id: str,
    user: dict = Depends(get_current_user),
) -> list[dict[str, Any]]:
    factory = require_db()
    tenant_id = user["tenant_id"]
    async with factory() as session:
        await _set_tenant(session, tenant_id)
        rows = (
            await session.execute(
                sa.select(knowledge_card_sources)
                .where(
                    knowledge_card_sources.c.tenant_id == tenant_id,
                    knowledge_card_sources.c.card_id == card_id,
                )
                .order_by(knowledge_card_sources.c.created_at.asc())
            )
        ).mappings().all()
    return [
        {
            "id": int(r["id"]),
            "source_type": r["source_type"],
            "source_id": r["source_id"],
            "sme_id": r["sme_id"],
            "sme_role": r["sme_role"],
            "stated_at": r["stated_at"].isoformat() if r["stated_at"] else None,
            "similarity_to_canonical": (
                float(r["similarity_to_canonical"])
                if r["similarity_to_canonical"] is not None
                else None
            ),
            "weight": float(r["weight"]),
            "status": r["status"],
            "metadata": dict(r["metadata_json"] or {}),
            "created_at": r["created_at"].isoformat(),
            "updated_at": r["updated_at"].isoformat(),
        }
        for r in rows
    ]


@router.get("/{card_id}/history")
async def card_history(
    card_id: str,
    limit: int = 100,
    user: dict = Depends(get_current_user),
) -> list[dict[str, Any]]:
    factory = require_db()
    tenant_id = user["tenant_id"]
    limit = max(1, min(500, int(limit)))
    async with factory() as session:
        await _set_tenant(session, tenant_id)
        rows = (
            await session.execute(
                sa.select(knowledge_card_history)
                .where(
                    knowledge_card_history.c.tenant_id == tenant_id,
                    knowledge_card_history.c.card_id == card_id,
                )
                .order_by(knowledge_card_history.c.changed_at.desc())
                .limit(limit)
            )
        ).mappings().all()
    return [
        {
            "id": int(r["id"]),
            "change_type": r["change_type"],
            "changed_by": r["changed_by"],
            "snapshot": dict(r["snapshot"] or {}),
            "note": r["note"],
            "changed_at": r["changed_at"].isoformat(),
        }
        for r in rows
    ]


# ── Lifecycle mutations ────────────────────────────────────────


async def _apply_card_change(
    *,
    tenant_id: str,
    card_id: str,
    actor: str,
    changes: dict[str, Any],
    change_type: str,
    note: Optional[str] = None,
) -> dict[str, Any]:
    factory = require_db()
    async with factory() as session:
        await _set_tenant(session, tenant_id)
        existing = (
            await session.execute(
                sa.select(knowledge_cards).where(
                    knowledge_cards.c.tenant_id == tenant_id,
                    knowledge_cards.c.card_id == card_id,
                )
            )
        ).mappings().first()
        if existing is None:
            raise HTTPException(404, "card_not_found")
        new_row = await session.execute(
            sa.update(knowledge_cards)
            .where(
                knowledge_cards.c.tenant_id == tenant_id,
                knowledge_cards.c.card_id == card_id,
                knowledge_cards.c.version == existing["version"],
            )
            .values(**changes)
            .returning(knowledge_cards)
        )
        row = new_row.mappings().first()
        if row is None:
            raise HTTPException(
                409, {"code": "version_conflict", "version": existing["version"]}
            )
        await session.execute(
            sa.insert(knowledge_card_history).values(
                card_id=card_id,
                tenant_id=tenant_id,
                change_type=change_type,
                changed_by=actor,
                snapshot={
                    "before": {
                        "lifecycle_state": existing["lifecycle_state"],
                        "version": existing["version"],
                    },
                    "after": {
                        "lifecycle_state": row["lifecycle_state"],
                        "version": row["version"],
                    },
                    **({"reason": note} if note else {}),
                },
                note=note,
                changed_at=_now(),
            )
        )
        await session.commit()
    return dict(row)


@router.post("/{card_id}/promote", response_model=CardOut)
async def promote_card(
    card_id: str,
    user: dict = Depends(get_current_user),
) -> CardOut:
    _require_priv(user)
    row = await _apply_card_change(
        tenant_id=user["tenant_id"],
        card_id=card_id,
        actor=user["user_id"],
        changes={
            "lifecycle_state": "canonical",
            "last_verified_at": _now(),
        },
        change_type="promoted",
    )
    return _to_card_out(row)


@router.post("/{card_id}/demote", response_model=CardOut)
async def demote_card(
    card_id: str,
    user: dict = Depends(get_current_user),
) -> CardOut:
    _require_priv(user)
    row = await _apply_card_change(
        tenant_id=user["tenant_id"],
        card_id=card_id,
        actor=user["user_id"],
        changes={"lifecycle_state": "tribal"},
        change_type="demoted",
    )
    return _to_card_out(row)


@router.post("/{card_id}/contest", response_model=CardOut)
async def contest_card(
    card_id: str,
    body: ContestRequest,
    user: dict = Depends(get_current_user),
) -> CardOut:
    _require_priv(user)
    row = await _apply_card_change(
        tenant_id=user["tenant_id"],
        card_id=card_id,
        actor=user["user_id"],
        changes={"lifecycle_state": "contested"},
        change_type="marked_contested",
        note=body.reason,
    )
    return _to_card_out(row)


@router.post("/{card_id}/supersede", response_model=CardOut)
async def supersede_card(
    card_id: str,
    body: SupersedeRequest,
    user: dict = Depends(get_current_user),
) -> CardOut:
    _require_priv(user)
    if body.superseded_by == card_id:
        raise HTTPException(400, "card cannot supersede itself")
    row = await _apply_card_change(
        tenant_id=user["tenant_id"],
        card_id=card_id,
        actor=user["user_id"],
        changes={
            "lifecycle_state": "deprecated",
            "superseded_by": body.superseded_by,
        },
        change_type="superseded",
        note=body.note,
    )
    return _to_card_out(row)


# ── Authority matrix ───────────────────────────────────────────


@router.get("/authority", response_model=list[AuthorityWeightOut])
async def list_authority(
    user: dict = Depends(get_current_user),
) -> list[AuthorityWeightOut]:
    factory = require_db()
    tenant_id = user["tenant_id"]
    async with factory() as session:
        await _set_tenant(session, tenant_id)
        rows = (
            await session.execute(
                sa.select(
                    tenant_authority_matrix.c.role,
                    tenant_authority_matrix.c.weight,
                )
                .where(tenant_authority_matrix.c.tenant_id == tenant_id)
                .order_by(tenant_authority_matrix.c.role.asc())
            )
        ).all()
    return [AuthorityWeightOut(role=r.role, weight=float(r.weight)) for r in rows]


@router.put("/authority/{role}", response_model=AuthorityWeightOut)
async def upsert_authority(
    role: str,
    body: UpsertAuthorityRequest,
    user: dict = Depends(get_current_user),
) -> AuthorityWeightOut:
    _require_priv(user)
    role_clean = role.strip().lower()
    if not role_clean or len(role_clean) > 128:
        raise HTTPException(400, "role must be 1..128 chars")
    factory = require_db()
    tenant_id = user["tenant_id"]
    now = _now()
    async with factory() as session:
        await _set_tenant(session, tenant_id)
        stmt = pg_insert(tenant_authority_matrix).values(
            tenant_id=tenant_id,
            role=role_clean,
            weight=body.weight,
            metadata_json={"updated_by": user.get("user_id")},
            created_at=now,
            updated_at=now,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[
                tenant_authority_matrix.c.tenant_id,
                tenant_authority_matrix.c.role,
            ],
            set_={
                "weight": stmt.excluded.weight,
                "metadata_json": stmt.excluded.metadata_json,
                "updated_at": stmt.excluded.updated_at,
            },
        )
        await session.execute(stmt)
        await session.commit()
    return AuthorityWeightOut(role=role_clean, weight=body.weight)
