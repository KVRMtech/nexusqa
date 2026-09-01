"""Product entity resolution for atlas eligibility.

Inputs:
    * the tenant's product catalog (from migration 019 ``products``)
    * a fragment of text (typically a transcript segment, rule text,
      visual scene description, etc.)

Output: a ranked list of candidate ``product_id``s with confidence.

Phase 1's ``ProductTagger`` returns a set of matches; this resolver
adds:

    * ranking by match count + alias specificity,
    * a stable ``primary`` choice for builders that need exactly one
      product to attach an atlas node to,
    * configurable minimum-confidence gate.

It does not depend on an LLM — pure word-boundary regex on the
tenant's declared catalog. An LLM-augmented variant can wrap this
later without breaking the contract.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Iterable, Optional

logger = logging.getLogger(__name__)


# ── DTOs ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ProductCatalogEntry:
    product_id: str
    name: str
    slug: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProductMatch:
    product_id: str
    hits: int
    distinct_terms: int
    confidence: float


@dataclass(frozen=True)
class ProductVerdict:
    primary: Optional[str]
    matches: tuple[ProductMatch, ...]
    confidence: float

    @property
    def all_product_ids(self) -> tuple[str, ...]:
        return tuple(m.product_id for m in self.matches)


# ── Resolver ───────────────────────────────────────────────────


class ProductResolver:
    """Tenant-scoped product entity resolver.

    Compile-once-reuse-many: pass the tenant's catalog at construction;
    the regex set is built once. Call ``resolve(text)`` per fragment.
    """

    def __init__(
        self,
        catalog: Iterable[ProductCatalogEntry],
        *,
        min_confidence: float = 0.5,
        max_results: int = 3,
    ) -> None:
        self._entries: list[ProductCatalogEntry] = []
        self._patterns: dict[str, list[tuple[str, re.Pattern[str]]]] = {}
        seen_ids: set[str] = set()
        for entry in catalog:
            if entry.product_id in seen_ids:
                continue
            seen_ids.add(entry.product_id)
            patterns: list[tuple[str, re.Pattern[str]]] = []
            seen_terms: set[str] = set()
            for raw in (entry.name, entry.slug, *entry.aliases):
                norm = (raw or "").strip()
                if not norm:
                    continue
                key = norm.lower()
                if key in seen_terms:
                    continue
                seen_terms.add(key)
                escaped = re.escape(norm)
                patterns.append(
                    (
                        norm,
                        re.compile(rf"(?<!\w){escaped}(?!\w)", re.IGNORECASE),
                    )
                )
            if patterns:
                self._entries.append(entry)
                self._patterns[entry.product_id] = patterns
        self._min_conf = max(0.0, min(1.0, float(min_confidence)))
        self._max_results = max(1, int(max_results))

    @property
    def is_empty(self) -> bool:
        return not self._entries

    def resolve(self, text: str) -> ProductVerdict:
        if not text or not self._entries:
            return ProductVerdict(primary=None, matches=(), confidence=0.0)

        per_product: list[ProductMatch] = []
        for entry in self._entries:
            patterns = self._patterns.get(entry.product_id, [])
            hits = 0
            distinct_terms = 0
            for _, p in patterns:
                found = p.findall(text)
                if found:
                    hits += len(found)
                    distinct_terms += 1
            if hits == 0:
                continue
            confidence = self._score(
                hits=hits,
                distinct_terms=distinct_terms,
                total_terms=len(patterns),
            )
            if confidence < self._min_conf:
                continue
            per_product.append(
                ProductMatch(
                    product_id=entry.product_id,
                    hits=hits,
                    distinct_terms=distinct_terms,
                    confidence=confidence,
                )
            )

        if not per_product:
            return ProductVerdict(primary=None, matches=(), confidence=0.0)

        # Rank: more distinct_terms wins; tie-break on hits; then by
        # catalog position to stay deterministic.
        order = {e.product_id: i for i, e in enumerate(self._entries)}
        per_product.sort(
            key=lambda m: (
                -m.distinct_terms,
                -m.hits,
                order.get(m.product_id, 10_000),
            )
        )
        trimmed = tuple(per_product[: self._max_results])
        primary = trimmed[0].product_id
        return ProductVerdict(
            primary=primary,
            matches=trimmed,
            confidence=trimmed[0].confidence,
        )

    # ── Scoring ─────────────────────────────────────────────────

    @staticmethod
    def _score(*, hits: int, distinct_terms: int, total_terms: int) -> float:
        if total_terms <= 0:
            return 0.0
        # Distinct-terms coverage drives base confidence; total hits
        # buys a smaller boost. Capped at 1.0.
        coverage = distinct_terms / total_terms
        hit_boost = min(0.30, (hits - distinct_terms) * 0.05)
        return min(1.0, 0.55 + coverage * 0.40 + hit_boost)
