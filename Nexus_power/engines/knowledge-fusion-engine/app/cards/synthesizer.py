"""Card synthesizer — folds new sources into the card graph.

For each ``SourceCandidate``:

    1. Search Backbone for similar KnowledgeCard nodes (top-K).
    2. Decide:
         best similarity ≥ DEDUP_THRESHOLD   → ADD as source to that card
         best similarity ≥ RELATED_THRESHOLD → ADD as source AND mark
                                                ``dissent`` if a contradiction
                                                is detected; otherwise active
         best similarity < RELATED_THRESHOLD → CREATE new tribal card,
                                                seeded with this source.
    3. Recompute authority chain, canonical_confidence, consensus_score,
       lifecycle_state, dissent_count for the affected card.
    4. (Optional) Create/refresh the KnowledgeCard Backbone node so the
       card participates in echo search.

The synthesizer is idempotent on ``(card_id, source_type, source_id)``
thanks to the UNIQUE constraint on ``knowledge_card_sources``. Calling
it twice for the same input is a no-op for the source itself, but it
still re-evaluates lifecycle (cheap).
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Literal, Optional

from ..backbone_client import BackboneClient, BackboneClientError
from .authority import AuthorityCalculator
from .contradiction import (
    ContradictionDetector,
    HeuristicContradictionDetector,
)
from .lifecycle import LifecycleManager, LifecycleState
from .models import SourceCandidate, SourceType
from .repository import CardRepository

logger = logging.getLogger(__name__)


# ── Tunables ───────────────────────────────────────────────────


@dataclass(frozen=True)
class SynthesisConfig:
    dedup_threshold: float = 0.88     # ≥ this → fold into existing card
    related_threshold: float = 0.72   # ≥ this and < dedup → fold + assess agreement
    search_limit: int = 5
    max_topic_label_chars: int = 96
    saturation_weight: float = 6.0    # passed to AuthorityCalculator
    register_backbone_node: bool = True

    def __post_init__(self) -> None:
        if not (0.0 < self.related_threshold <= self.dedup_threshold <= 1.0):
            raise ValueError(
                "thresholds must satisfy 0 < related <= dedup <= 1"
            )


# ── Outputs ────────────────────────────────────────────────────


@dataclass(frozen=True)
class SourceOutcome:
    source_id: str
    action: Literal["added_to_existing", "created_new", "noop_duplicate", "skipped"]
    card_id: Optional[str]
    similarity: Optional[float]
    status: Optional[str]
    dissent_signal: Optional[str] = None


@dataclass(frozen=True)
class SynthesisResult:
    outcomes: list[SourceOutcome]
    cards_touched: set[str] = field(default_factory=set)
    errors: list[str] = field(default_factory=list)


# ── Synthesizer ────────────────────────────────────────────────


class CardSynthesizer:
    def __init__(
        self,
        *,
        repo: CardRepository,
        backbone: BackboneClient,
        config: Optional[SynthesisConfig] = None,
        contradiction_detector: Optional[ContradictionDetector] = None,
    ) -> None:
        self._repo = repo
        self._backbone = backbone
        self._cfg = config or SynthesisConfig()
        self._lifecycle = LifecycleManager()
        self._detector = (
            contradiction_detector or HeuristicContradictionDetector()
        )

    # ── Public API ──────────────────────────────────────────────

    async def synthesize_batch(
        self,
        *,
        tenant_id: str,
        trace_id: str,
        candidates: Iterable[SourceCandidate],
        actor: Optional[str] = None,
    ) -> SynthesisResult:
        result = SynthesisResult(outcomes=[])
        calc = await self._make_calc(tenant_id)

        for cand in candidates:
            try:
                outcome = await self._synthesize_one(
                    tenant_id=tenant_id,
                    trace_id=trace_id,
                    candidate=cand,
                    calc=calc,
                    actor=actor,
                )
            except BackboneClientError as exc:
                logger.warning(
                    "synth.backbone_failed source=%s err=%s",
                    cand.source_id,
                    exc,
                )
                result.errors.append(
                    f"backbone:{cand.source_id}:{exc}"
                )
                continue
            except Exception as exc:
                logger.exception(
                    "synth.unhandled_error source=%s err=%s",
                    cand.source_id,
                    exc,
                )
                result.errors.append(
                    f"unhandled:{cand.source_id}:{exc}"
                )
                continue

            result.outcomes.append(outcome)
            if outcome.card_id:
                result.cards_touched.add(outcome.card_id)
        return result

    # ── Per-candidate pipeline ─────────────────────────────────

    async def _synthesize_one(
        self,
        *,
        tenant_id: str,
        trace_id: str,
        candidate: SourceCandidate,
        calc: AuthorityCalculator,
        actor: Optional[str],
    ) -> SourceOutcome:
        text = (candidate.text or "").strip()
        if not text:
            return SourceOutcome(
                source_id=candidate.source_id,
                action="skipped",
                card_id=None,
                similarity=None,
                status=None,
            )

        existing = await self._backbone.search(
            tenant_id=tenant_id,
            trace_id=trace_id,
            query=text,
            node_types=["KnowledgeCard"],
            limit=self._cfg.search_limit,
            min_similarity=self._cfg.related_threshold,
        )

        best = _best_match(existing, related_floor=self._cfg.related_threshold)

        if best is None or best.similarity < self._cfg.related_threshold:
            # Brand-new topic — seed a tribal card.
            return await self._create_new_card(
                tenant_id=tenant_id,
                trace_id=trace_id,
                candidate=candidate,
                calc=calc,
                actor=actor,
            )

        card_id = best.card_id
        if not card_id:
            # The matched node didn't carry our card_id — fall back to
            # creating a new card to avoid attaching to a foreign node.
            return await self._create_new_card(
                tenant_id=tenant_id,
                trace_id=trace_id,
                candidate=candidate,
                calc=calc,
                actor=actor,
            )

        return await self._attach_to_card(
            tenant_id=tenant_id,
            card_id=card_id,
            candidate=candidate,
            similarity=best.similarity,
            best_text=best.canonical_text,
            calc=calc,
            actor=actor,
        )

    # ── Create path ─────────────────────────────────────────────

    async def _create_new_card(
        self,
        *,
        tenant_id: str,
        trace_id: str,
        candidate: SourceCandidate,
        calc: AuthorityCalculator,
        actor: Optional[str],
    ) -> SourceOutcome:
        # Topic label/slug derived from candidate text. Operators can
        # later rename via the platform-api admin endpoint.
        topic_label = _make_label(
            candidate.text, limit=self._cfg.max_topic_label_chars
        )
        topic_slug = _make_slug(topic_label, source_id=candidate.source_id)

        card_row = await self._repo.create_card(
            tenant_id=tenant_id,
            topic_slug=topic_slug,
            topic_label=topic_label,
            canonical_statement=candidate.text.strip(),
            product_id=candidate.product_id,
            jurisdiction=candidate.jurisdiction,
            tags=[t for t in (candidate.extra.get("tags") or []) if isinstance(t, str)],
            halflife_days=int(candidate.extra.get("halflife_days") or 270),
            changed_by=actor,
        )
        card_id = card_row["card_id"]

        # Add the seeding source.
        stored = await self._repo.add_source(
            tenant_id=tenant_id,
            card_id=card_id,
            source_type=candidate.source_type.value,
            source_id=candidate.source_id,
            backbone_node_id=candidate.backbone_node_id,
            session_id=candidate.session_id,
            artifact_id=candidate.artifact_id,
            sme_id=candidate.sme_id,
            sme_role=candidate.sme_role,
            stated_at=candidate.stated_at,
            similarity_to_canonical=1.0,
            weight=calc.contribution(
                sme_id=candidate.sme_id,
                sme_role=candidate.sme_role,
                stated_at=candidate.stated_at,
                halflife_days=card_row["halflife_days"],
                prior_contributing_count=0,
            ).weight,
            status="active",
            metadata={"seed": True},
            changed_by=actor,
        )

        # Recompute scores + register backbone node.
        await self._recompute_card(
            tenant_id=tenant_id,
            card_id=card_id,
            actor=actor,
            calc=calc,
        )
        if self._cfg.register_backbone_node:
            try:
                node_id = await self._backbone.store_knowledge_card_node(
                    tenant_id=tenant_id,
                    trace_id=trace_id,
                    card_id=card_id,
                    topic_label=topic_label,
                    canonical_statement=candidate.text.strip(),
                    product_id=candidate.product_id,
                    tags=card_row.get("tags") or [],
                    properties={"topic_slug": topic_slug},
                )
                latest = await self._repo.get(
                    tenant_id=tenant_id, card_id=card_id
                )
                if latest is not None:
                    await self._repo.update_card(
                        tenant_id=tenant_id,
                        card_id=card_id,
                        expected_version=latest["version"],
                        changes={"backbone_node_id": node_id},
                        change_type="metadata_updated",
                        changed_by=actor,
                        note="registered Backbone node",
                    )
            except BackboneClientError as exc:
                logger.warning(
                    "synth.card_node_register_failed card=%s err=%s",
                    card_id,
                    exc,
                )

        return SourceOutcome(
            source_id=candidate.source_id,
            action="created_new",
            card_id=card_id,
            similarity=1.0,
            status="active" if stored else "active",
        )

    # ── Attach path ─────────────────────────────────────────────

    async def _attach_to_card(
        self,
        *,
        tenant_id: str,
        card_id: str,
        candidate: SourceCandidate,
        similarity: float,
        best_text: Optional[str],
        calc: AuthorityCalculator,
        actor: Optional[str],
    ) -> SourceOutcome:
        card = await self._repo.get(tenant_id=tenant_id, card_id=card_id)
        if card is None:
            # Backbone returned a card_id we don't own — degrade to creating a new card.
            return await self._create_new_card(
                tenant_id=tenant_id,
                trace_id=uuid.uuid4().hex,
                candidate=candidate,
                calc=calc,
                actor=actor,
            )

        # Heuristic contradiction detection against the canonical text.
        canonical_text = best_text or card["canonical_statement"]
        signal = self._detector.detect(
            canonical=canonical_text, candidate=candidate.text
        )

        status = "active"
        dissent_kind: Optional[str] = None
        if signal is not None and similarity >= self._cfg.related_threshold:
            status = "dissenting"
            dissent_kind = signal.kind

        prior_count = int(card["contributing_count"])
        contribution = calc.contribution(
            sme_id=candidate.sme_id,
            sme_role=candidate.sme_role,
            stated_at=candidate.stated_at,
            halflife_days=int(card["halflife_days"]),
            prior_contributing_count=prior_count,
        )

        stored = await self._repo.add_source(
            tenant_id=tenant_id,
            card_id=card_id,
            source_type=candidate.source_type.value,
            source_id=candidate.source_id,
            backbone_node_id=candidate.backbone_node_id,
            session_id=candidate.session_id,
            artifact_id=candidate.artifact_id,
            sme_id=candidate.sme_id,
            sme_role=candidate.sme_role,
            stated_at=candidate.stated_at,
            similarity_to_canonical=float(similarity),
            weight=contribution.weight,
            status=status,
            metadata={
                "contradiction_kind": dissent_kind,
                "contradiction_confidence": (
                    signal.confidence if signal else None
                ),
            } if signal else {},
            changed_by=actor,
        )

        if stored is None:
            # Duplicate — still recompute lifecycle so consensus reflects
            # any operator-driven status changes that may have happened.
            await self._recompute_card(
                tenant_id=tenant_id,
                card_id=card_id,
                actor=actor,
                calc=calc,
            )
            return SourceOutcome(
                source_id=candidate.source_id,
                action="noop_duplicate",
                card_id=card_id,
                similarity=float(similarity),
                status=None,
            )

        await self._recompute_card(
            tenant_id=tenant_id,
            card_id=card_id,
            actor=actor,
            calc=calc,
        )

        return SourceOutcome(
            source_id=candidate.source_id,
            action="added_to_existing",
            card_id=card_id,
            similarity=float(similarity),
            status=status,
            dissent_signal=dissent_kind,
        )

    # ── Recompute ──────────────────────────────────────────────

    async def _recompute_card(
        self,
        *,
        tenant_id: str,
        card_id: str,
        actor: Optional[str],
        calc: AuthorityCalculator,
    ) -> None:
        counts = await self._repo.count_sources_by_status(
            tenant_id=tenant_id, card_id=card_id
        )
        active = int(counts.get("active", 0))
        dissent = int(counts.get("dissenting", 0))
        total = active + dissent + int(counts.get("superseded", 0))

        active_weight = await self._repo.sum_active_weight(
            tenant_id=tenant_id, card_id=card_id
        )
        confidence = AuthorityCalculator.canonical_confidence(
            [active_weight],
            saturation_weight=self._cfg.saturation_weight,
        )

        card = await self._repo.get(tenant_id=tenant_id, card_id=card_id)
        if card is None:
            return

        decision = self._lifecycle.evaluate(
            current_state=LifecycleState(card["lifecycle_state"]),
            active_count=active,
            dissent_count=dissent,
            superseded_by=card.get("superseded_by"),
        )

        # Rebuild authority_chain summary for the UI.
        sources = await self._repo.list_sources(
            tenant_id=tenant_id, card_id=card_id
        )
        chain = [
            {
                "source_id": s["source_id"],
                "sme_id": s.get("sme_id"),
                "sme_role": s.get("sme_role"),
                "weight": float(s.get("weight") or 0.0),
                "status": s["status"],
                "stated_at": s["stated_at"].isoformat()
                if s.get("stated_at")
                else None,
            }
            for s in sources
        ]
        chain.sort(key=lambda e: e["weight"], reverse=True)

        last_verified = datetime.now(timezone.utc)
        verify_due = (
            last_verified + timedelta(days=int(card["halflife_days"]))
            if int(card["halflife_days"]) > 0
            else None
        )

        changes: dict[str, Any] = {
            "consensus_score": decision.consensus_score,
            "canonical_confidence": float(confidence),
            "contributing_count": total,
            "dissent_count": dissent,
            "authority_chain": chain,
            "last_verified_at": last_verified,
            "verify_due_at": verify_due,
        }
        if decision.is_transition:
            changes["lifecycle_state"] = decision.state.value

        await self._repo.update_card(
            tenant_id=tenant_id,
            card_id=card_id,
            expected_version=card["version"],
            changes=changes,
            change_type=decision.change_type or "canonical_updated",
            changed_by=actor,
            note=None,
            snapshot_extra={
                "active": active,
                "dissent": dissent,
                "active_weight": active_weight,
            },
        )

    async def _make_calc(self, tenant_id: str) -> AuthorityCalculator:
        overrides = await self._repo.get_authority_overrides(tenant_id)
        return AuthorityCalculator(role_overrides=overrides)


# ── Helpers ────────────────────────────────────────────────────


@dataclass(frozen=True)
class _MatchPick:
    card_id: Optional[str]
    similarity: float
    canonical_text: Optional[str]


def _best_match(
    rows: list[dict[str, Any]], *, related_floor: float
) -> Optional[_MatchPick]:
    best: Optional[_MatchPick] = None
    for row in rows:
        sim = float(row.get("similarity") or 0.0)
        if sim < related_floor:
            continue
        props = row.get("properties") or {}
        if not isinstance(props, dict):
            continue
        card_id = props.get("card_id")
        if not isinstance(card_id, str) or not card_id:
            continue
        canonical_text = (
            props.get("canonical_statement") or props.get("text")
        )
        if (best is None) or (sim > best.similarity):
            best = _MatchPick(
                card_id=card_id,
                similarity=sim,
                canonical_text=canonical_text
                if isinstance(canonical_text, str)
                else None,
            )
    return best


_LABEL_TRIM_RE = re.compile(r"\s+")


def _make_label(text: str, *, limit: int) -> str:
    cleaned = _LABEL_TRIM_RE.sub(" ", (text or "").strip())
    if not cleaned:
        return "Untitled topic"
    if len(cleaned) <= limit:
        return cleaned
    snippet = cleaned[: max(0, limit - 1)].rstrip()
    return snippet + "…"


_SLUG_KEEP_RE = re.compile(r"[^a-z0-9]+")


def _make_slug(label: str, *, source_id: str) -> str:
    base = _SLUG_KEEP_RE.sub("-", label.lower()).strip("-")
    base = base[:240]
    if not base:
        base = "topic"
    # Append a short, deterministic suffix derived from the seeding
    # source_id so concurrent inserts don't collide on uq_kc_topic_slug.
    suffix = source_id[:8] if source_id else uuid.uuid4().hex[:8]
    return f"{base}-{suffix}"
