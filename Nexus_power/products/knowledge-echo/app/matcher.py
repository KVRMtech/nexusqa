"""Matcher — turns a question into ranked, hydrated echo candidates.

Wraps the Backbone HTTP search behind a small type layer so the
orchestrator can reason about confidence bands without parsing JSON.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal

from .backbone_client import BackboneSearchClient

logger = logging.getLogger(__name__)


ConfidenceBand = Literal["high", "medium", "low", "none"]


@dataclass(frozen=True)
class MatchCandidate:
    node_id: str
    node_type: str
    similarity: float
    text: str
    speaker_id: str
    speaker_role: str
    session_id: str
    artifact_id: str
    start_ms: int
    end_ms: int
    ordinal: int
    product_ids: tuple[str, ...]
    raw: dict[str, Any]

    def to_audit_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "node_type": self.node_type,
            "similarity": self.similarity,
            "session_id": self.session_id,
            "artifact_id": self.artifact_id,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "ordinal": self.ordinal,
            "speaker_id": self.speaker_id,
            "product_ids": list(self.product_ids),
        }


@dataclass(frozen=True)
class MatchResult:
    candidates: list[MatchCandidate]
    top_similarity: float
    confidence_band: ConfidenceBand

    @property
    def is_empty(self) -> bool:
        return not self.candidates


class Matcher:
    def __init__(
        self,
        client: BackboneSearchClient,
        *,
        high_threshold: float = 0.85,
        medium_threshold: float = 0.65,
        node_types: tuple[str, ...] = (
            "TranscriptSegment",
            "BusinessRule",
            "KnowledgeCard",
        ),
    ) -> None:
        if not (0.0 <= medium_threshold <= high_threshold <= 1.0):
            raise ValueError(
                "thresholds must satisfy 0 <= medium <= high <= 1"
            )
        self._client = client
        self._high = high_threshold
        self._medium = medium_threshold
        self._node_types = list(node_types)

    async def match(
        self,
        *,
        tenant_id: str,
        trace_id: str,
        query: str,
        limit: int,
    ) -> MatchResult:
        if not query or not query.strip():
            return MatchResult(
                candidates=[], top_similarity=0.0, confidence_band="none"
            )

        raw_results = await self._client.search(
            tenant_id=tenant_id,
            trace_id=trace_id,
            query=query.strip(),
            node_types=self._node_types,
            limit=limit,
            min_similarity=self._medium,
        )
        candidates = [self._materialise(r) for r in raw_results]
        candidates = [c for c in candidates if c is not None]
        candidates.sort(key=lambda c: c.similarity, reverse=True)
        top_sim = candidates[0].similarity if candidates else 0.0
        return MatchResult(
            candidates=candidates,
            top_similarity=top_sim,
            confidence_band=self._band(top_sim),
        )

    def _band(self, similarity: float) -> ConfidenceBand:
        if similarity >= self._high:
            return "high"
        if similarity >= self._medium:
            return "medium"
        if similarity > 0:
            return "low"
        return "none"

    @staticmethod
    def _materialise(row: dict[str, Any]) -> MatchCandidate | None:
        if not isinstance(row, dict):
            return None
        properties = row.get("properties") or {}
        if not isinstance(properties, dict):
            properties = {}
        source = row.get("source") or {}
        text = properties.get("text") or properties.get("rule_text") or ""
        if not isinstance(text, str):
            text = str(text)
        try:
            similarity = float(row.get("similarity") or 0.0)
        except (TypeError, ValueError):
            similarity = 0.0
        product_ids_raw = properties.get("product_ids") or []
        if not isinstance(product_ids_raw, list):
            product_ids_raw = []
        return MatchCandidate(
            node_id=str(row.get("node_id") or ""),
            node_type=str(row.get("node_type") or ""),
            similarity=similarity,
            text=text,
            speaker_id=str(properties.get("speaker_id") or ""),
            speaker_role=str(properties.get("speaker_role") or ""),
            session_id=str(
                properties.get("session_id") or source.get("session_id") or ""
            ),
            artifact_id=str(properties.get("artifact_id") or ""),
            start_ms=int(properties.get("start_ms") or 0),
            end_ms=int(properties.get("end_ms") or 0),
            ordinal=int(properties.get("ordinal") or 0),
            product_ids=tuple(
                str(p) for p in product_ids_raw if isinstance(p, (str, int))
            ),
            raw=row,
        )
