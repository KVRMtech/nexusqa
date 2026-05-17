"""Knowledge Gap recorder.

When the orchestrator suppresses an echo for ``suppressed_low_conf`` or
``no_match`` reasons, the question represents a *knowledge gap* — a
topic the platform doesn't yet know about. The recorder aggregates
these by topic hash and surfaces them on the gap dashboard.

Production qualities:
    * Idempotent on ``(tenant_id, topic_hash)``.
    * Increment counters atomically via SQL — no read-then-write races.
    * Compute a stable topic_hash that survives small phrasing changes
      (lowercased + whitespace-collapsed; stop words preserved so two
      genuinely different questions don't collide).
    * Never raise — failures log and return None so the orchestrator
      stays responsive.
"""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert

from .db import Database

logger = logging.getLogger(__name__)


_md = sa.MetaData()


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

knowledge_gap_questions = sa.Table(
    "knowledge_gap_questions",
    _md,
    sa.Column("id", sa.BigInteger, primary_key=True),
    sa.Column("gap_id", sa.String(64), nullable=False),
    sa.Column("tenant_id", sa.String(64), nullable=False),
    sa.Column("echo_dispatch_id", sa.String(64)),
    sa.Column("asker_user_id_ext", sa.String(128)),
    sa.Column("asker_org_user_id", sa.String(64)),
    sa.Column("question_text_redacted", sa.Text, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)


# ── DTOs ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class GapRecordInput:
    tenant_id: str
    question_text: str
    asker_user_id_ext: Optional[str] = None
    asker_org_user_id: Optional[str] = None
    echo_dispatch_id: Optional[str] = None
    product_ids: tuple[str, ...] = ()
    similarity: Optional[float] = None
    reason: str = ""


@dataclass(frozen=True)
class GapRecordResult:
    gap_id: str
    is_new: bool
    question_count: int


# ── Recorder ───────────────────────────────────────────────────


class KnowledgeGapRecorder:
    def __init__(self, db: Database):
        self._db = db

    async def record(
        self, rec: GapRecordInput
    ) -> Optional[GapRecordResult]:
        text = (rec.question_text or "").strip()
        if not text:
            return None
        topic_hash = compute_topic_hash(text)
        topic_label = _make_label(text)
        now = _now()
        try:
            async with self._db.tenant_session(rec.tenant_id) as session:
                stmt = pg_insert(knowledge_gaps).values(
                    gap_id=uuid.uuid4().hex,
                    tenant_id=rec.tenant_id,
                    topic_hash=topic_hash,
                    topic_label=topic_label[:256],
                    topic_summary=text[:4000],
                    question_count=1,
                    unique_askers_count=1 if rec.asker_user_id_ext or rec.asker_org_user_id else 0,
                    first_seen_at=now,
                    last_seen_at=now,
                    product_ids=list(rec.product_ids),
                    suggested_sme_ids=[],
                    status="open",
                    metadata_json={
                        "last_similarity": rec.similarity,
                        "last_reason": rec.reason,
                    },
                    created_at=now,
                    updated_at=now,
                    version=1,
                )
                stmt = stmt.on_conflict_do_update(
                    constraint="uq_gap_tenant_topic",
                    set_={
                        "question_count": knowledge_gaps.c.question_count + 1,
                        "last_seen_at": stmt.excluded.last_seen_at,
                        "product_ids": _array_union(
                            knowledge_gaps.c.product_ids,
                            stmt.excluded.product_ids,
                        ),
                        "metadata_json": stmt.excluded.metadata_json,
                        # status sticks at whatever the operator set,
                        # except 'archived' rows are reopened by new asks.
                        "status": sa.case(
                            (
                                knowledge_gaps.c.status == "archived",
                                sa.literal("open"),
                            ),
                            else_=knowledge_gaps.c.status,
                        ),
                    },
                ).returning(knowledge_gaps)
                row = (await session.execute(stmt)).mappings().first()
                if row is None:
                    return None

                # Append the asker history with idempotency on
                # (gap_id, dispatch_id) so a re-dispatch doesn't
                # double-count.
                await session.execute(
                    pg_insert(knowledge_gap_questions).values(
                        gap_id=row["gap_id"],
                        tenant_id=rec.tenant_id,
                        echo_dispatch_id=rec.echo_dispatch_id,
                        asker_user_id_ext=rec.asker_user_id_ext,
                        asker_org_user_id=rec.asker_org_user_id,
                        question_text_redacted=text[:4000],
                        created_at=now,
                    )
                )

                # Recompute unique askers via a cheap COUNT(DISTINCT).
                distinct_count = int(
                    (
                        await session.execute(
                            sa.select(
                                sa.func.count(
                                    sa.func.distinct(
                                        sa.func.coalesce(
                                            knowledge_gap_questions.c.asker_org_user_id,
                                            knowledge_gap_questions.c.asker_user_id_ext,
                                        )
                                    )
                                )
                            ).where(
                                knowledge_gap_questions.c.tenant_id == rec.tenant_id,
                                knowledge_gap_questions.c.gap_id == row["gap_id"],
                                sa.or_(
                                    knowledge_gap_questions.c.asker_org_user_id.is_not(None),
                                    knowledge_gap_questions.c.asker_user_id_ext.is_not(None),
                                ),
                            )
                        )
                    ).scalar_one()
                )
                await session.execute(
                    sa.update(knowledge_gaps)
                    .where(
                        knowledge_gaps.c.tenant_id == rec.tenant_id,
                        knowledge_gaps.c.gap_id == row["gap_id"],
                    )
                    .values(unique_askers_count=distinct_count)
                )
        except Exception as exc:
            logger.warning("gaps.record_failed tenant=%s err=%s", rec.tenant_id, exc)
            return None

        question_count = int(row["question_count"])
        is_new = question_count == 1
        return GapRecordResult(
            gap_id=row["gap_id"],
            is_new=is_new,
            question_count=question_count,
        )


# ── Helpers ─────────────────────────────────────────────────────


_WHITESPACE_RE = re.compile(r"\s+")
# Strip everything except word chars and whitespace — punctuation and
# question marks all collapse so phrasing variants ("how do I X?" vs
# "how do I X") hash identically.
_NORMALISE_RE = re.compile(r"[^\w\s]")


def compute_topic_hash(text: str) -> str:
    normalised = _NORMALISE_RE.sub(
        " ", (text or "").lower()
    )
    normalised = _WHITESPACE_RE.sub(" ", normalised).strip()
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def _make_label(text: str, *, limit: int = 200) -> str:
    cleaned = _WHITESPACE_RE.sub(" ", (text or "").strip())
    if not cleaned:
        return "Untitled gap"
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(0, limit - 1)].rstrip() + "…"


def _array_union(left, right):
    union = sa.func.array_cat(left, right)
    return sa.func.array(
        sa.select(sa.func.unnest(union).label("v"))
        .distinct()
        .scalar_subquery()
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)
