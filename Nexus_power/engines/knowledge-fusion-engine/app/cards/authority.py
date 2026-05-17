"""Authority weighting — role × recency × confirmation count.

The score for a single source contribution is::

    weight = role_weight(sme_role) * recency(stated_at, halflife_days)
                                   * confirmation_boost(card.contributing_count)

The canonical confidence of a card is the *normalised* sum of weights
of its ACTIVE sources, mapped into [0, 1] via a saturating function.

Authority chains are persisted as JSON on the card so the UI can
display "verified by X, then Y, then Z" without a join.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Iterable, Optional

# Default role weights. Tenants may override via tenant_authority_matrix.
DEFAULT_ROLE_WEIGHTS: dict[str, float] = {
    # compliance / legal / regulatory speak with the highest authority
    # on rules. These take precedence over individual SME assertions.
    "compliance": 3.0,
    "legal": 3.0,
    "chief_compliance_officer": 3.5,
    "regulatory": 3.0,
    # Trainers / architects / SMEs
    "trainer": 2.0,
    "architect": 2.0,
    "principal": 2.0,
    "sme": 2.0,
    "senior_engineer": 1.8,
    "tech_lead": 1.8,
    # Operations / engineering
    "engineer": 1.5,
    "qa": 1.5,
    "product": 1.5,
    "support": 1.2,
    # Sales / front-line
    "sales": 1.0,
    "rep": 1.0,
    "associate": 0.9,
    # Default
    "": 1.0,
}


@dataclass(frozen=True)
class AuthorityContribution:
    sme_id: Optional[str]
    sme_role: Optional[str]
    role_weight: float
    recency_factor: float
    confirmation_boost: float
    weight: float

    def to_chain_entry(self, *, source_id: str, stated_at: Optional[date]) -> dict:
        return {
            "source_id": source_id,
            "sme_id": self.sme_id or "",
            "sme_role": self.sme_role or "",
            "role_weight": self.role_weight,
            "recency_factor": self.recency_factor,
            "confirmation_boost": self.confirmation_boost,
            "weight": self.weight,
            "stated_at": stated_at.isoformat() if stated_at else None,
        }


class AuthorityCalculator:
    """Pure functions for authority weighting.

    Construct with per-tenant role overrides (empty for defaults) and
    reuse across many cards.
    """

    def __init__(
        self,
        *,
        role_overrides: Optional[dict[str, float]] = None,
        recency_floor: float = 0.05,
        confirmation_floor: float = 0.7,
        confirmation_ceiling: float = 1.5,
    ) -> None:
        merged = dict(DEFAULT_ROLE_WEIGHTS)
        if role_overrides:
            for k, v in role_overrides.items():
                if not isinstance(k, str) or not k:
                    continue
                try:
                    weight = float(v)
                except (TypeError, ValueError):
                    continue
                if weight <= 0:
                    continue
                merged[k.lower()] = weight
        self._roles = merged
        self._recency_floor = max(0.0, min(1.0, recency_floor))
        self._conf_floor = confirmation_floor
        self._conf_ceiling = confirmation_ceiling

    # ── Public API ──────────────────────────────────────────────

    def role_weight(self, role: Optional[str]) -> float:
        if not role:
            return self._roles.get("", 1.0)
        key = role.strip().lower()
        if not key:
            return self._roles.get("", 1.0)
        if key in self._roles:
            return self._roles[key]
        # Allow simple suffix matches: "senior_compliance" → "compliance"
        # only on whole-word boundaries inside a snake_case role.
        tokens = key.replace("-", "_").split("_")
        best = 0.0
        for t in tokens:
            if t in self._roles:
                best = max(best, self._roles[t])
        return best if best > 0 else self._roles.get("", 1.0)

    def recency_factor(
        self,
        stated_at: Optional[date],
        *,
        halflife_days: int,
        now: Optional[datetime] = None,
    ) -> float:
        """Exponential decay with floor; ``stated_at=None`` collapses to 1.0.

        ``halflife_days=270`` (default) means a 9-month-old assertion
        weighs ~50% of a fresh one.
        """
        if stated_at is None or halflife_days <= 0:
            return 1.0
        now_dt = now or datetime.now(timezone.utc)
        # Treat stated_at as UTC midnight.
        stated_dt = datetime(
            stated_at.year, stated_at.month, stated_at.day, tzinfo=timezone.utc
        )
        elapsed_days = max(0.0, (now_dt - stated_dt).total_seconds() / 86400.0)
        decay = 0.5 ** (elapsed_days / halflife_days)
        return max(self._recency_floor, min(1.0, decay))

    def confirmation_boost(self, contributing_count: int) -> float:
        """Log-shaped boost so 3 sources >> 1, 10 sources ≈ 9.

        Capped at ``confirmation_ceiling`` to avoid runaway scoring on
        viral topics.
        """
        if contributing_count <= 0:
            return self._conf_floor
        # log10(1+n) maps {1:0.30, 3:0.60, 10:1.04, 30:1.49}
        boost = self._conf_floor + math.log10(1 + contributing_count) * 0.4
        return min(self._conf_ceiling, boost)

    def contribution(
        self,
        *,
        sme_id: Optional[str],
        sme_role: Optional[str],
        stated_at: Optional[date],
        halflife_days: int,
        prior_contributing_count: int,
        now: Optional[datetime] = None,
    ) -> AuthorityContribution:
        rw = self.role_weight(sme_role)
        rf = self.recency_factor(
            stated_at, halflife_days=halflife_days, now=now
        )
        cb = self.confirmation_boost(prior_contributing_count)
        return AuthorityContribution(
            sme_id=sme_id,
            sme_role=sme_role,
            role_weight=rw,
            recency_factor=rf,
            confirmation_boost=cb,
            weight=rw * rf * cb,
        )

    @staticmethod
    def canonical_confidence(
        active_weights: Iterable[float],
        *,
        saturation_weight: float = 6.0,
    ) -> float:
        """Saturating map from sum-of-weights to [0,1].

        ``saturation_weight`` is the total weight at which the card hits
        roughly 0.85 confidence. Tunable per tenant later.
        """
        total = float(sum(w for w in active_weights if w > 0))
        if total <= 0:
            return 0.0
        if saturation_weight <= 0:
            saturation_weight = 1.0
        # 1 - e^(-total / s); approaches 1 as total grows.
        return float(1.0 - math.exp(-total / saturation_weight))
