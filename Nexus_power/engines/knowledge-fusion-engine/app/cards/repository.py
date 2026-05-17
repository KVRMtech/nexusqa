"""SQLAlchemy Core projections + CRUD for knowledge cards.

The DB triggers handle ``updated_at`` and ``version`` bumps on
``knowledge_cards``; other tables get explicit ``updated_at`` writes.

History is written by the repository on every meaningful state change
so audits / promotion-ladder dashboards can reconstruct timelines.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Optional

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ..db import Database

logger = logging.getLogger(__name__)


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
    sa.Column("backbone_node_id", sa.String(64)),
    sa.Column("session_id", sa.String(64)),
    sa.Column("artifact_id", sa.String(64)),
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


# ── Exceptions ──────────────────────────────────────────────────


class CardConflict(Exception):
    """Optimistic-lock conflict on ``knowledge_cards.version``."""


# ── DTOs returned to callers ───────────────────────────────────


@dataclass(frozen=True)
class StoredCardSource:
    id: int
    card_id: str
    source_type: str
    source_id: str
    status: str
    weight: float


# ── Repository ─────────────────────────────────────────────────


class CardRepository:
    def __init__(self, db: Database):
        self._db = db

    # ── Authority matrix ────────────────────────────────────────

    async def get_authority_overrides(self, tenant_id: str) -> dict[str, float]:
        async with self._db.tenant_session(tenant_id) as session:
            rows = (
                await session.execute(
                    sa.select(
                        tenant_authority_matrix.c.role,
                        tenant_authority_matrix.c.weight,
                    ).where(
                        tenant_authority_matrix.c.tenant_id == tenant_id
                    )
                )
            ).all()
        return {row.role: float(row.weight) for row in rows}

    async def upsert_authority(
        self,
        *,
        tenant_id: str,
        role: str,
        weight: float,
        actor: Optional[str] = None,
    ) -> None:
        if weight <= 0:
            raise ValueError("weight must be > 0")
        if not role or not isinstance(role, str):
            raise ValueError("role must be a non-empty string")
        now = _now()
        async with self._db.tenant_session(tenant_id) as session:
            stmt = pg_insert(tenant_authority_matrix).values(
                tenant_id=tenant_id,
                role=role.lower(),
                weight=weight,
                metadata_json={"updated_by": actor} if actor else {},
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

    # ── Card lookups ────────────────────────────────────────────

    async def get(
        self, *, tenant_id: str, card_id: str
    ) -> Optional[dict[str, Any]]:
        async with self._db.tenant_session(tenant_id) as session:
            row = (
                await session.execute(
                    sa.select(knowledge_cards).where(
                        knowledge_cards.c.tenant_id == tenant_id,
                        knowledge_cards.c.card_id == card_id,
                    )
                )
            ).mappings().first()
        return dict(row) if row else None

    async def get_by_slug(
        self, *, tenant_id: str, topic_slug: str
    ) -> Optional[dict[str, Any]]:
        async with self._db.tenant_session(tenant_id) as session:
            row = (
                await session.execute(
                    sa.select(knowledge_cards).where(
                        knowledge_cards.c.tenant_id == tenant_id,
                        knowledge_cards.c.topic_slug == topic_slug,
                    )
                )
            ).mappings().first()
        return dict(row) if row else None

    async def list_for_tenant(
        self,
        *,
        tenant_id: str,
        states: Optional[Iterable[str]] = None,
        product_id: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        async with self._db.tenant_session(tenant_id) as session:
            stmt = sa.select(knowledge_cards).where(
                knowledge_cards.c.tenant_id == tenant_id
            )
            if states:
                state_list = [s for s in states if isinstance(s, str)]
                if state_list:
                    stmt = stmt.where(
                        knowledge_cards.c.lifecycle_state.in_(state_list)
                    )
            if product_id:
                stmt = stmt.where(
                    knowledge_cards.c.product_id == product_id
                )
            stmt = stmt.order_by(
                knowledge_cards.c.updated_at.desc()
            ).limit(max(1, min(500, int(limit))))
            rows = (await session.execute(stmt)).mappings().all()
        return [dict(r) for r in rows]

    async def list_sources(
        self, *, tenant_id: str, card_id: str
    ) -> list[dict[str, Any]]:
        async with self._db.tenant_session(tenant_id) as session:
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
        return [dict(r) for r in rows]

    # ── Card writes ─────────────────────────────────────────────

    async def create_card(
        self,
        *,
        tenant_id: str,
        topic_slug: str,
        topic_label: str,
        canonical_statement: str,
        product_id: Optional[str],
        jurisdiction: Optional[str],
        tags: list[str],
        halflife_days: int,
        changed_by: Optional[str],
    ) -> dict[str, Any]:
        """Create a new tribal-state card with no sources yet."""
        card_id = uuid.uuid4().hex
        now = _now()
        verify_due = (
            now + timedelta(days=halflife_days) if halflife_days > 0 else None
        )
        async with self._db.tenant_session(tenant_id) as session:
            await session.execute(
                sa.insert(knowledge_cards).values(
                    card_id=card_id,
                    tenant_id=tenant_id,
                    topic_slug=topic_slug,
                    topic_label=topic_label[:512],
                    canonical_statement=canonical_statement,
                    canonical_confidence=0.0,
                    consensus_score=0.0,
                    lifecycle_state="tribal",
                    authority_chain=[],
                    contributing_count=0,
                    dissent_count=0,
                    product_id=product_id,
                    jurisdiction=jurisdiction,
                    halflife_days=halflife_days,
                    verify_due_at=verify_due,
                    tags=tags or [],
                    metadata_json={},
                    created_at=now,
                    updated_at=now,
                    version=1,
                )
            )
            await session.execute(
                sa.insert(knowledge_card_history).values(
                    card_id=card_id,
                    tenant_id=tenant_id,
                    change_type="created",
                    changed_by=changed_by,
                    snapshot={
                        "topic_slug": topic_slug,
                        "topic_label": topic_label,
                        "canonical_statement": canonical_statement,
                        "product_id": product_id,
                        "jurisdiction": jurisdiction,
                        "tags": list(tags or []),
                    },
                    changed_at=now,
                )
            )
            row = (
                await session.execute(
                    sa.select(knowledge_cards).where(
                        knowledge_cards.c.card_id == card_id,
                    )
                )
            ).mappings().first()
        if row is None:
            raise RuntimeError("card insert returned no row")
        return dict(row)

    async def update_card(
        self,
        *,
        tenant_id: str,
        card_id: str,
        expected_version: int,
        changes: dict[str, Any],
        change_type: str,
        changed_by: Optional[str],
        note: Optional[str] = None,
        snapshot_extra: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Apply ``changes`` to a card with optimistic locking + history."""
        if not changes:
            existing = await self.get(tenant_id=tenant_id, card_id=card_id)
            if existing is None:
                raise CardConflict(f"card {card_id} not found")
            return existing

        async with self._db.tenant_session(tenant_id) as session:
            existing = (
                await session.execute(
                    sa.select(knowledge_cards).where(
                        knowledge_cards.c.tenant_id == tenant_id,
                        knowledge_cards.c.card_id == card_id,
                    )
                )
            ).mappings().first()
            if existing is None:
                raise CardConflict(f"card {card_id} not found")
            if existing["version"] != expected_version:
                raise CardConflict(
                    f"card {card_id} version mismatch: "
                    f"expected={expected_version} actual={existing['version']}"
                )

            updated = await session.execute(
                sa.update(knowledge_cards)
                .where(
                    knowledge_cards.c.tenant_id == tenant_id,
                    knowledge_cards.c.card_id == card_id,
                    knowledge_cards.c.version == expected_version,
                )
                .values(**changes)
                .returning(knowledge_cards)
            )
            row = updated.mappings().first()
            if row is None:
                raise CardConflict(
                    f"card {card_id} version drift after lock check"
                )

            snapshot = {
                "before": _snapshot_subset(existing),
                "after": _snapshot_subset(dict(row)),
            }
            if snapshot_extra:
                snapshot.update(snapshot_extra)
            await session.execute(
                sa.insert(knowledge_card_history).values(
                    card_id=card_id,
                    tenant_id=tenant_id,
                    change_type=change_type,
                    changed_by=changed_by,
                    snapshot=snapshot,
                    note=note,
                    changed_at=_now(),
                )
            )

        return dict(row)

    # ── Source writes ───────────────────────────────────────────

    async def add_source(
        self,
        *,
        tenant_id: str,
        card_id: str,
        source_type: str,
        source_id: str,
        backbone_node_id: Optional[str],
        session_id: Optional[str],
        artifact_id: Optional[str],
        sme_id: Optional[str],
        sme_role: Optional[str],
        stated_at: Optional[date],
        similarity_to_canonical: Optional[float],
        weight: float,
        status: str = "active",
        metadata: Optional[dict[str, Any]] = None,
        changed_by: Optional[str] = None,
    ) -> Optional[StoredCardSource]:
        """Idempotent: returns None when the source row already exists."""
        now = _now()
        async with self._db.tenant_session(tenant_id) as session:
            stmt = pg_insert(knowledge_card_sources).values(
                card_id=card_id,
                tenant_id=tenant_id,
                source_type=source_type,
                source_id=source_id,
                backbone_node_id=backbone_node_id,
                session_id=session_id,
                artifact_id=artifact_id,
                sme_id=sme_id,
                sme_role=sme_role,
                stated_at=stated_at,
                similarity_to_canonical=similarity_to_canonical,
                weight=weight,
                status=status,
                metadata_json=metadata or {},
                created_at=now,
                updated_at=now,
            ).on_conflict_do_nothing(
                constraint="uq_kcs_card_source"
            ).returning(knowledge_card_sources)
            row = (await session.execute(stmt)).mappings().first()
            if row is None:
                return None
            await session.execute(
                sa.insert(knowledge_card_history).values(
                    card_id=card_id,
                    tenant_id=tenant_id,
                    change_type="source_added",
                    changed_by=changed_by,
                    snapshot={
                        "source_id": source_id,
                        "source_type": source_type,
                        "sme_id": sme_id,
                        "sme_role": sme_role,
                        "weight": weight,
                        "status": status,
                        "similarity_to_canonical": similarity_to_canonical,
                    },
                    changed_at=now,
                )
            )
        return StoredCardSource(
            id=int(row["id"]),
            card_id=row["card_id"],
            source_type=row["source_type"],
            source_id=row["source_id"],
            status=row["status"],
            weight=float(row["weight"]),
        )

    async def update_source_status(
        self,
        *,
        tenant_id: str,
        card_id: str,
        source_id_pk: int,
        new_status: str,
        changed_by: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        now = _now()
        async with self._db.tenant_session(tenant_id) as session:
            existing = (
                await session.execute(
                    sa.select(knowledge_card_sources).where(
                        knowledge_card_sources.c.tenant_id == tenant_id,
                        knowledge_card_sources.c.id == source_id_pk,
                    )
                )
            ).mappings().first()
            if existing is None:
                return None
            if existing["status"] == new_status:
                return dict(existing)
            await session.execute(
                sa.update(knowledge_card_sources)
                .where(
                    knowledge_card_sources.c.id == source_id_pk,
                    knowledge_card_sources.c.tenant_id == tenant_id,
                )
                .values(status=new_status, updated_at=now)
            )
            row = (
                await session.execute(
                    sa.select(knowledge_card_sources).where(
                        knowledge_card_sources.c.id == source_id_pk,
                    )
                )
            ).mappings().first()
            await session.execute(
                sa.insert(knowledge_card_history).values(
                    card_id=card_id,
                    tenant_id=tenant_id,
                    change_type="source_status_changed",
                    changed_by=changed_by,
                    snapshot={
                        "source_pk": source_id_pk,
                        "from": existing["status"],
                        "to": new_status,
                    },
                    changed_at=now,
                )
            )
        return dict(row) if row else None

    # ── Counters ───────────────────────────────────────────────

    async def count_sources_by_status(
        self, *, tenant_id: str, card_id: str
    ) -> dict[str, int]:
        async with self._db.tenant_session(tenant_id) as session:
            rows = (
                await session.execute(
                    sa.select(
                        knowledge_card_sources.c.status,
                        sa.func.count().label("count"),
                    )
                    .where(
                        knowledge_card_sources.c.tenant_id == tenant_id,
                        knowledge_card_sources.c.card_id == card_id,
                    )
                    .group_by(knowledge_card_sources.c.status)
                )
            ).all()
        return {row.status: int(row.count) for row in rows}

    async def sum_active_weight(
        self, *, tenant_id: str, card_id: str
    ) -> float:
        async with self._db.tenant_session(tenant_id) as session:
            row = (
                await session.execute(
                    sa.select(
                        sa.func.coalesce(
                            sa.func.sum(knowledge_card_sources.c.weight),
                            0.0,
                        )
                    ).where(
                        knowledge_card_sources.c.tenant_id == tenant_id,
                        knowledge_card_sources.c.card_id == card_id,
                        knowledge_card_sources.c.status == "active",
                    )
                )
            ).first()
        return float(row[0] if row else 0.0)

    # ── History accessor ───────────────────────────────────────

    async def history(
        self, *, tenant_id: str, card_id: str, limit: int = 100
    ) -> list[dict[str, Any]]:
        async with self._db.tenant_session(tenant_id) as session:
            rows = (
                await session.execute(
                    sa.select(knowledge_card_history)
                    .where(
                        knowledge_card_history.c.tenant_id == tenant_id,
                        knowledge_card_history.c.card_id == card_id,
                    )
                    .order_by(knowledge_card_history.c.changed_at.desc())
                    .limit(max(1, min(500, int(limit))))
                )
            ).mappings().all()
        return [dict(r) for r in rows]


# ── Helpers ─────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(timezone.utc)


_SNAPSHOT_KEYS = (
    "lifecycle_state",
    "canonical_statement",
    "canonical_confidence",
    "consensus_score",
    "contributing_count",
    "dissent_count",
    "topic_label",
    "product_id",
    "jurisdiction",
    "superseded_by",
    "tags",
    "version",
)


def _snapshot_subset(row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in _SNAPSHOT_KEYS:
        v = row.get(k)
        if isinstance(v, (datetime, date)):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return out
