"""Per-artifact indexing pipeline.

Inputs:  one ``indexing_jobs`` row (tenant_id + artifact_id).
Outputs: rows in ``transcript_segments``, Backbone nodes via the
         BackboneClient, and a structured result dict.

Steps:
    1. Fetch canonical artifact (must exist + status='completed' + quality_gate pass).
    2. If artifact isn't ready, return Outcome.SKIP (or RETRY when transient).
    3. Chunk the transcript.
    4. Detect product tags from text vs tenant product catalog.
    5. UPSERT segments into transcript_segments. Idempotent on text_hash.
    6. Create Backbone TranscriptSegment nodes for each new segment.
    7. Mark segment row embedding_status='indexed' with backbone_node_id.

The pipeline is idempotent: re-running for the same artifact_id is
safe — text_hash dedup and unique constraint on (artifact_id, ordinal)
prevent duplicates.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from .atlas import (
    AtlasBuilder,
    NodeCandidate,
    ProductCatalogEntry,
    ProductResolver,
)
from .backbone_client import BackboneClient, BackboneClientError
from .canonical_reader import CanonicalReader
from .cards import CardSynthesizer, SourceCandidate, SourceType
from .chunker import Chunk, ChunkerConfig, TranscriptChunker
from .db import Database, transcript_segments

logger = logging.getLogger(__name__)


class IndexerOutcome(str, Enum):
    INDEXED = "indexed"
    NOOP = "noop"
    SKIP_NOT_READY = "skip_not_ready"
    SKIP_NO_TRANSCRIPT = "skip_no_transcript"
    SKIP_QUALITY_GATE = "skip_quality_gate"
    RETRY = "retry"


@dataclass(frozen=True)
class IndexResult:
    outcome: IndexerOutcome
    segments_created: int
    segments_skipped: int
    segments_failed: int
    artifact_status: Optional[str]
    detail: dict[str, Any]


class Indexer:
    def __init__(
        self,
        db: Database,
        reader: CanonicalReader,
        backbone: BackboneClient,
        chunker: TranscriptChunker,
        synthesizer: Optional["CardSynthesizer"] = None,
        atlas_builder: Optional[AtlasBuilder] = None,
    ):
        self._db = db
        self._reader = reader
        self._backbone = backbone
        self._chunker = chunker
        self._synthesizer = synthesizer
        self._atlas_builder = atlas_builder

    async def index_artifact(
        self,
        *,
        tenant_id: str,
        session_id: str,
        artifact_id: str,
        trace_id: str,
    ) -> IndexResult:
        artifact = await self._reader.fetch_artifact(
            tenant_id=tenant_id, artifact_id=artifact_id
        )
        if artifact is None:
            return IndexResult(
                outcome=IndexerOutcome.RETRY,
                segments_created=0,
                segments_skipped=0,
                segments_failed=0,
                artifact_status=None,
                detail={"reason": "artifact_not_found"},
            )

        status = (artifact.get("status") or "").lower()
        qg = (artifact.get("quality_gate_outcome") or "").lower()

        # The artifact must be terminal-completed before we accept it
        # into the substrate. Anything else returns RETRY (which lets
        # the queue back off and re-check later).
        if status != "completed":
            return IndexResult(
                outcome=IndexerOutcome.RETRY,
                segments_created=0,
                segments_skipped=0,
                segments_failed=0,
                artifact_status=status,
                detail={"reason": f"artifact_status={status}"},
            )
        if qg and qg not in ("pass", ""):
            # Quality gate explicitly failed or needs review — skip
            # (not retry; this is a terminal state for substrate).
            return IndexResult(
                outcome=IndexerOutcome.SKIP_QUALITY_GATE,
                segments_created=0,
                segments_skipped=0,
                segments_failed=0,
                artifact_status=status,
                detail={"quality_gate_outcome": qg},
            )

        chunks = self._chunker.chunk_artifact(artifact)
        if not chunks:
            return IndexResult(
                outcome=IndexerOutcome.SKIP_NO_TRANSCRIPT,
                segments_created=0,
                segments_skipped=0,
                segments_failed=0,
                artifact_status=status,
                detail={"reason": "no_transcript_text"},
            )

        products = await self._reader.fetch_tenant_products(tenant_id=tenant_id)
        tagger = ProductTagger(products)

        # Persist segment rows first. text_hash dedup makes this
        # safe to call repeatedly.
        created, skipped = await self._upsert_segments(
            tenant_id=tenant_id,
            session_id=session_id,
            artifact_id=artifact_id,
            chunks=chunks,
            tagger=tagger,
        )

        # Now embed via Backbone. Newly-created segments are picked
        # up via embedding_status='pending'; previously-indexed ones
        # are skipped naturally.
        backbone_created, embed_failed, just_embedded = (
            await self._embed_pending(
                tenant_id=tenant_id,
                session_id=session_id,
                artifact_id=artifact_id,
                trace_id=trace_id,
            )
        )

        cards_created = 0
        cards_updated = 0
        cards_errors: list[str] = []
        if self._synthesizer is not None and just_embedded:
            stated_at: Optional[Any] = None
            for ts_key in ("completed_at", "created_at"):
                v = artifact.get(ts_key)
                if v is not None:
                    stated_at = v
                    break
            stated_date = (
                stated_at.date() if hasattr(stated_at, "date") else None
            )
            candidates: list[SourceCandidate] = []
            for row in just_embedded:
                candidates.append(
                    SourceCandidate(
                        source_type=SourceType.SEGMENT,
                        source_id=row["segment_id"],
                        backbone_node_id=row["backbone_node_id"],
                        text=row["text_redacted"],
                        session_id=session_id,
                        artifact_id=artifact_id,
                        sme_id=row.get("speaker_id") or None,
                        sme_role=row.get("speaker_role") or None,
                        stated_at=stated_date,
                        product_id=(
                            row.get("product_ids", [None])[0]
                            if row.get("product_ids")
                            else None
                        ),
                    )
                )
            try:
                synth_result = await self._synthesizer.synthesize_batch(
                    tenant_id=tenant_id,
                    trace_id=trace_id,
                    candidates=candidates,
                )
                cards_created = sum(
                    1
                    for o in synth_result.outcomes
                    if o.action == "created_new"
                )
                cards_updated = sum(
                    1
                    for o in synth_result.outcomes
                    if o.action == "added_to_existing"
                )
                cards_errors = synth_result.errors
            except Exception as exc:
                logger.warning(
                    "indexer.synthesizer_failed tenant=%s artifact=%s err=%s",
                    tenant_id,
                    artifact_id,
                    exc,
                )
                cards_errors.append(f"unhandled:{exc}")

        atlas_nodes_upserted = 0
        atlas_edges_auto = 0
        atlas_alignments_pending = 0
        atlas_products_touched: tuple[str, ...] = ()
        atlas_errors: list[str] = []
        if self._atlas_builder is not None and just_embedded:
            atlas_candidates = []
            for row in just_embedded:
                product_id = (
                    row.get("product_ids", [None])[0]
                    if row.get("product_ids")
                    else None
                )
                atlas_candidates.append(
                    NodeCandidate(
                        tenant_id=tenant_id,
                        backbone_node_id=row["backbone_node_id"],
                        node_type="TranscriptSegment",
                        label=row["text_redacted"][:160] or row["segment_id"],
                        text=row["text_redacted"],
                        product_id=product_id,
                        source_session_id=session_id,
                        source_artifact_id=artifact_id,
                        source_segment_ids=[row["segment_id"]],
                    )
                )
            resolver = ProductResolver(
                [
                    ProductCatalogEntry(
                        product_id=p["product_id"],
                        name=p["name"],
                        slug=p["slug"],
                        aliases=tuple(p.get("aliases") or []),
                    )
                    for p in products
                ]
            )
            try:
                atlas_result = await self._atlas_builder.build(
                    tenant_id=tenant_id,
                    trace_id=trace_id,
                    candidates=atlas_candidates,
                    product_resolver=resolver,
                )
                atlas_nodes_upserted = atlas_result.nodes_upserted
                atlas_edges_auto = atlas_result.edges_auto
                atlas_alignments_pending = atlas_result.alignments_pending
                atlas_products_touched = atlas_result.products_touched
                atlas_errors = list(atlas_result.errors)
            except Exception as exc:
                logger.warning(
                    "indexer.atlas_build_failed tenant=%s artifact=%s err=%s",
                    tenant_id,
                    artifact_id,
                    exc,
                )
                atlas_errors.append(f"unhandled:{exc}")

        outcome = (
            IndexerOutcome.INDEXED
            if created > 0 or backbone_created > 0
            else IndexerOutcome.NOOP
        )
        return IndexResult(
            outcome=outcome,
            segments_created=created,
            segments_skipped=skipped,
            segments_failed=embed_failed,
            artifact_status=status,
            detail={
                "backbone_nodes_created": backbone_created,
                "cards_created": cards_created,
                "cards_updated": cards_updated,
                "card_synth_errors": cards_errors,
                "atlas_nodes_upserted": atlas_nodes_upserted,
                "atlas_edges_auto": atlas_edges_auto,
                "atlas_alignments_pending": atlas_alignments_pending,
                "atlas_products_touched": list(atlas_products_touched),
                "atlas_errors": atlas_errors,
            },
        )

    # ── Persistence ─────────────────────────────────────────────

    async def _upsert_segments(
        self,
        *,
        tenant_id: str,
        session_id: str,
        artifact_id: str,
        chunks: list[Chunk],
        tagger: "ProductTagger",
    ) -> tuple[int, int]:
        """Insert segments, skipping rows where (artifact_id, text_hash)
        already exists. Returns (created, skipped)."""
        created = 0
        skipped = 0
        async with self._db.tenant_session(tenant_id) as session:
            # Snapshot existing text hashes for idempotency.
            existing_hashes = {
                row["text_hash"]
                for row in (
                    await session.execute(
                        sa.select(transcript_segments.c.text_hash).where(
                            transcript_segments.c.tenant_id == tenant_id,
                            transcript_segments.c.artifact_id == artifact_id,
                        )
                    )
                ).mappings().all()
            }

            # Snapshot the largest existing ordinal so we keep ordinals
            # strictly increasing across re-runs of partial work.
            base_ordinal = (
                await session.execute(
                    sa.select(
                        sa.func.coalesce(
                            sa.func.max(transcript_segments.c.ordinal), -1
                        )
                    ).where(
                        transcript_segments.c.tenant_id == tenant_id,
                        transcript_segments.c.artifact_id == artifact_id,
                    )
                )
            ).scalar_one()

            next_ordinal = int(base_ordinal) + 1
            rows_to_insert: list[dict[str, Any]] = []
            now = _now()
            for chunk in chunks:
                if chunk.text_hash in existing_hashes:
                    skipped += 1
                    continue
                product_ids = list(tagger.tag(chunk.text))
                rows_to_insert.append(
                    {
                        "segment_id": chunk.segment_id,
                        "tenant_id": tenant_id,
                        "session_id": session_id,
                        "artifact_id": artifact_id,
                        "ordinal": next_ordinal,
                        "speaker_id": chunk.speaker_id,
                        "speaker_role": chunk.speaker_role,
                        "text_redacted": chunk.text,
                        "text_hash": chunk.text_hash,
                        "start_ms": chunk.start_ms,
                        "end_ms": chunk.end_ms,
                        "token_count": chunk.token_count,
                        "confidence": chunk.confidence,
                        "product_ids": product_ids,
                        "topic_label": chunk.topic_label,
                        "backbone_node_id": None,
                        "embedding_status": "pending",
                        "created_at": now,
                    }
                )
                existing_hashes.add(chunk.text_hash)
                next_ordinal += 1

            if rows_to_insert:
                stmt = pg_insert(transcript_segments).values(rows_to_insert)
                stmt = stmt.on_conflict_do_nothing(
                    constraint="uq_ts_artifact_ordinal"
                )
                result = await session.execute(stmt)
                created = int(result.rowcount or 0)

        return created, skipped

    async def _embed_pending(
        self,
        *,
        tenant_id: str,
        session_id: str,
        artifact_id: str,
        trace_id: str,
    ) -> tuple[int, int, list[dict[str, Any]]]:
        """Send pending segments to Backbone and update embedding_status.

        Returns ``(succeeded, failed, just_embedded)`` where
        ``just_embedded`` is the list of rows successfully embedded in
        this pass — used by the card synthesizer to fold them into the
        card graph without re-querying the DB.
        """
        async with self._db.tenant_session(tenant_id) as session:
            pending = (
                await session.execute(
                    sa.select(
                        transcript_segments.c.segment_id,
                        transcript_segments.c.text_redacted,
                        transcript_segments.c.ordinal,
                        transcript_segments.c.start_ms,
                        transcript_segments.c.end_ms,
                        transcript_segments.c.speaker_id,
                        transcript_segments.c.speaker_role,
                        transcript_segments.c.product_ids,
                    )
                    .where(
                        transcript_segments.c.tenant_id == tenant_id,
                        transcript_segments.c.artifact_id == artifact_id,
                        transcript_segments.c.embedding_status == "pending",
                    )
                    .order_by(transcript_segments.c.ordinal.asc())
                )
            ).mappings().all()

        if not pending:
            return 0, 0, []

        succeeded = 0
        failed = 0
        just_embedded: list[dict[str, Any]] = []
        for row in pending:
            try:
                node_id = await self._backbone.store_transcript_segment(
                    tenant_id=tenant_id,
                    trace_id=trace_id,
                    session_id=session_id,
                    text=row["text_redacted"],
                    properties={
                        "segment_id": row["segment_id"],
                        "artifact_id": artifact_id,
                        "session_id": session_id,
                        "ordinal": row["ordinal"],
                        "start_ms": row["start_ms"],
                        "end_ms": row["end_ms"],
                        "speaker_id": row["speaker_id"] or "",
                        "speaker_role": row["speaker_role"] or "",
                        "product_ids": list(row["product_ids"] or []),
                    },
                )
            except BackboneClientError as exc:
                logger.warning(
                    "indexer.embed_failed segment_id=%s err=%s",
                    row["segment_id"],
                    exc,
                )
                await self._mark_segment(
                    tenant_id=tenant_id,
                    segment_id=row["segment_id"],
                    status="failed",
                    backbone_node_id=None,
                )
                failed += 1
                continue
            await self._mark_segment(
                tenant_id=tenant_id,
                segment_id=row["segment_id"],
                status="indexed",
                backbone_node_id=node_id,
            )
            succeeded += 1
            just_embedded.append(
                {
                    "segment_id": row["segment_id"],
                    "text_redacted": row["text_redacted"],
                    "ordinal": row["ordinal"],
                    "start_ms": row["start_ms"],
                    "end_ms": row["end_ms"],
                    "speaker_id": row["speaker_id"],
                    "speaker_role": row["speaker_role"],
                    "product_ids": list(row["product_ids"] or []),
                    "backbone_node_id": node_id,
                }
            )
        return succeeded, failed, just_embedded

    async def _mark_segment(
        self,
        *,
        tenant_id: str,
        segment_id: str,
        status: str,
        backbone_node_id: Optional[str],
    ) -> None:
        async with self._db.tenant_session(tenant_id) as session:
            await session.execute(
                sa.update(transcript_segments)
                .where(
                    transcript_segments.c.tenant_id == tenant_id,
                    transcript_segments.c.segment_id == segment_id,
                )
                .values(
                    embedding_status=status,
                    backbone_node_id=backbone_node_id,
                )
            )


# ── Product tagger ──────────────────────────────────────────────


class ProductTagger:
    """Tag a chunk with product_ids by case-insensitive token match.

    Uses word-boundary regex per alias to avoid false positives
    (``LT5`` should not match ``ALT54``). Cheap and predictable —
    a real semantic tagger lands in Phase 5 when the Atlas needs
    cross-modal alignment.
    """

    def __init__(self, products: list[dict[str, Any]]):
        self._matchers: list[tuple[str, list[re.Pattern[str]]]] = []
        for p in products:
            terms: list[str] = [p["name"], p["slug"]]
            terms.extend(p.get("aliases") or [])
            patterns: list[re.Pattern[str]] = []
            seen: set[str] = set()
            for raw in terms:
                norm = (raw or "").strip()
                if not norm or norm.lower() in seen:
                    continue
                seen.add(norm.lower())
                # Word boundary on each side; allow internal punctuation
                # like ``LT-5`` or ``LT_5`` by treating non-alphanumerics
                # as literals.
                escaped = re.escape(norm)
                patterns.append(
                    re.compile(rf"(?<!\w){escaped}(?!\w)", re.IGNORECASE)
                )
            if patterns:
                self._matchers.append((p["product_id"], patterns))

    def tag(self, text: str) -> set[str]:
        if not text or not self._matchers:
            return set()
        out: set[str] = set()
        for product_id, patterns in self._matchers:
            if any(p.search(text) for p in patterns):
                out.add(product_id)
        return out


def _now() -> datetime:
    return datetime.now(timezone.utc)
