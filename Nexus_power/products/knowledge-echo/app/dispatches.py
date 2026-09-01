"""Persistence for echo_dispatches + echo_feedback + echo_dedup."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from .db import Database, echo_dedup, echo_dispatches, echo_feedback

logger = logging.getLogger(__name__)


# ── DTOs ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DispatchRecord:
    dispatch_id: str
    tenant_id: str
    trace_id: str
    decision: str
    confidence_band: Optional[str]
    top_similarity: Optional[float]
    posted_message_ref: Optional[str]


@dataclass(frozen=True)
class DedupHit:
    existing_dispatch_id: str
    expires_at: datetime


# ── Repository ─────────────────────────────────────────────────


class DispatchRepository:
    """All writes are tenant-scoped via RLS; reads filter explicitly too."""

    def __init__(self, db: Database):
        self._db = db

    # ── echo_dispatches ─────────────────────────────────────────

    async def create(
        self,
        *,
        tenant_id: str,
        trace_id: str,
        trigger_surface: str,
        trigger_plugin_event_id: Optional[str],
        trigger_user_id_ext: Optional[str],
        trigger_channel_ext: Optional[str],
        trigger_text_hash: str,
        classifier_output: Optional[dict[str, Any]],
        match_candidates: Optional[list[dict[str, Any]]],
        top_similarity: Optional[float],
        confidence_band: Optional[str],
        decision: str,
        decision_reason: Optional[str],
        effective_mode: Optional[str],
        rendered_payload_hash: Optional[str],
        posted_at: Optional[datetime],
        posted_message_ref: Optional[str],
    ) -> DispatchRecord:
        dispatch_id = uuid.uuid4().hex
        async with self._db.tenant_session(tenant_id) as session:
            await session.execute(
                sa.insert(echo_dispatches).values(
                    dispatch_id=dispatch_id,
                    tenant_id=tenant_id,
                    trace_id=trace_id,
                    trigger_surface=trigger_surface,
                    trigger_plugin_event_id=trigger_plugin_event_id,
                    trigger_user_id_ext=trigger_user_id_ext,
                    trigger_channel_ext=trigger_channel_ext,
                    trigger_text_hash=trigger_text_hash,
                    classifier_output=classifier_output,
                    match_candidates=match_candidates,
                    top_similarity=top_similarity,
                    confidence_band=confidence_band,
                    decision=decision,
                    decision_reason=decision_reason,
                    effective_mode=effective_mode,
                    rendered_payload_hash=rendered_payload_hash,
                    posted_at=posted_at,
                    posted_message_ref=posted_message_ref,
                    created_at=_now(),
                )
            )
        return DispatchRecord(
            dispatch_id=dispatch_id,
            tenant_id=tenant_id,
            trace_id=trace_id,
            decision=decision,
            confidence_band=confidence_band,
            top_similarity=top_similarity,
            posted_message_ref=posted_message_ref,
        )

    # ── echo_dedup ──────────────────────────────────────────────

    async def claim_or_get_existing(
        self,
        *,
        tenant_id: str,
        dedup_key: str,
        dispatch_id: str,
        ttl_seconds: int,
    ) -> Optional[DedupHit]:
        """Try to claim a dedup slot. Returns the existing dispatch_id
        on collision (caller should treat as duplicate)."""
        now = _now()
        expires_at = now + timedelta(seconds=ttl_seconds)
        async with self._db.tenant_session(tenant_id) as session:
            stmt = pg_insert(echo_dedup).values(
                tenant_id=tenant_id,
                dedup_key=dedup_key,
                dispatch_id=dispatch_id,
                expires_at=expires_at,
                created_at=now,
            )
            stmt = stmt.on_conflict_do_nothing(
                index_elements=[
                    echo_dedup.c.tenant_id,
                    echo_dedup.c.dedup_key,
                ]
            )
            result = await session.execute(stmt)
            if result.rowcount and result.rowcount > 0:
                return None  # claimed successfully
            # Existing row — check if it's still valid.
            row = (
                await session.execute(
                    sa.select(echo_dedup).where(
                        echo_dedup.c.tenant_id == tenant_id,
                        echo_dedup.c.dedup_key == dedup_key,
                    )
                )
            ).mappings().first()
        if row is None:
            return None
        if row["expires_at"] < _now():
            # Expired — reclaim by deleting + retrying once.
            await self._reclaim_expired(tenant_id, dedup_key)
            return await self.claim_or_get_existing(
                tenant_id=tenant_id,
                dedup_key=dedup_key,
                dispatch_id=dispatch_id,
                ttl_seconds=ttl_seconds,
            )
        return DedupHit(
            existing_dispatch_id=row["dispatch_id"],
            expires_at=row["expires_at"],
        )

    async def _reclaim_expired(
        self, tenant_id: str, dedup_key: str
    ) -> None:
        async with self._db.tenant_session(tenant_id) as session:
            await session.execute(
                sa.delete(echo_dedup).where(
                    echo_dedup.c.tenant_id == tenant_id,
                    echo_dedup.c.dedup_key == dedup_key,
                    echo_dedup.c.expires_at < _now(),
                )
            )

    async def sweep_expired(self, batch: int = 1000) -> int:
        async with self._db.session() as session:
            await session.execute(
                sa.text("SELECT set_config('nexus.current_tenant_id', '', true)")
            )
            sub = (
                sa.select(
                    echo_dedup.c.tenant_id, echo_dedup.c.dedup_key
                )
                .where(echo_dedup.c.expires_at < _now())
                .limit(batch)
                .subquery()
            )
            result = await session.execute(
                sa.delete(echo_dedup).where(
                    sa.tuple_(
                        echo_dedup.c.tenant_id, echo_dedup.c.dedup_key
                    ).in_(
                        sa.select(sub.c.tenant_id, sub.c.dedup_key)
                    )
                )
            )
            return int(result.rowcount or 0)

    # ── echo_feedback ───────────────────────────────────────────

    async def record_feedback(
        self,
        *,
        tenant_id: str,
        dispatch_id: str,
        user_id_ext: Optional[str],
        signal: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> bool:
        """Idempotent: (dispatch_id, user_id_ext, signal) is unique.

        Returns True if the row was inserted; False if it was already
        present (caller can ack without re-processing).
        """
        async with self._db.tenant_session(tenant_id) as session:
            # First confirm the dispatch belongs to this tenant.
            row = (
                await session.execute(
                    sa.select(echo_dispatches.c.dispatch_id).where(
                        echo_dispatches.c.tenant_id == tenant_id,
                        echo_dispatches.c.dispatch_id == dispatch_id,
                    )
                )
            ).first()
            if row is None:
                logger.warning(
                    "feedback.dispatch_not_found tenant=%s dispatch=%s",
                    tenant_id, dispatch_id,
                )
                return False

            stmt = pg_insert(echo_feedback).values(
                dispatch_id=dispatch_id,
                tenant_id=tenant_id,
                user_id_ext=user_id_ext,
                signal=signal,
                metadata_json=metadata or {},
                created_at=_now(),
            )
            stmt = stmt.on_conflict_do_nothing(
                index_elements=[
                    echo_feedback.c.dispatch_id,
                    echo_feedback.c.user_id_ext,
                    echo_feedback.c.signal,
                ]
            )
            result = await session.execute(stmt)
            return bool(result.rowcount and result.rowcount > 0)

    # ── Stats ───────────────────────────────────────────────────

    async def recent_decisions(
        self, tenant_id: str, *, limit: int = 50
    ) -> list[dict[str, Any]]:
        async with self._db.tenant_session(tenant_id) as session:
            rows = (
                await session.execute(
                    sa.select(
                        echo_dispatches.c.dispatch_id,
                        echo_dispatches.c.decision,
                        echo_dispatches.c.confidence_band,
                        echo_dispatches.c.top_similarity,
                        echo_dispatches.c.posted_message_ref,
                        echo_dispatches.c.created_at,
                    )
                    .where(echo_dispatches.c.tenant_id == tenant_id)
                    .order_by(echo_dispatches.c.created_at.desc())
                    .limit(limit)
                )
            ).mappings().all()
        return [dict(r) for r in rows]


# ── Helpers ─────────────────────────────────────────────────────


_NORMALISE_RE = re.compile(r"\s+")


def compute_text_hash(text: str) -> str:
    normalised = _NORMALISE_RE.sub(" ", (text or "").strip().lower())
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def compute_dedup_key(*, channel_id: Optional[str], text_hash: str) -> str:
    """Per-channel dedup key. Channel-less inputs (DMs) fall back to a
    tenant-wide bucket keyed only by text hash."""
    ch = channel_id or "_"
    raw = f"{ch}:{text_hash}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:64]


def _now() -> datetime:
    return datetime.now(timezone.utc)
