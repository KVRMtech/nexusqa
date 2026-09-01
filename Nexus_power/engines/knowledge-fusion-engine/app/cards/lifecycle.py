"""Card lifecycle — state machine and consensus computation.

States::

    tribal       — fewer than ``min_consensus_sources`` active sources,
                    or consensus_score below ``min_consensus_score``.
    consensus    — enough active sources, agreeing strongly.
    canonical    — explicitly promoted by an admin/compliance user.
    contested    — at least one active dissenting source.
    deprecated   — superseded by another card.

Transitions are driven by:
    1. Source additions / status changes (computed automatically).
    2. Explicit operator actions (``promote``, ``demote``,
       ``mark_contested``, ``supersede``).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class LifecycleState(str, Enum):
    TRIBAL = "tribal"
    CONSENSUS = "consensus"
    CANONICAL = "canonical"
    CONTESTED = "contested"
    DEPRECATED = "deprecated"


@dataclass(frozen=True)
class LifecycleDecision:
    state: LifecycleState
    consensus_score: float
    change_type: Optional[str]  # None when state didn't change

    @property
    def is_transition(self) -> bool:
        return self.change_type is not None


class LifecycleManager:
    """Deterministic transitions; no IO."""

    def __init__(
        self,
        *,
        min_consensus_sources: int = 3,
        min_consensus_score: float = 0.80,
    ) -> None:
        if min_consensus_sources < 2:
            raise ValueError("min_consensus_sources must be >= 2")
        if not (0.0 < min_consensus_score <= 1.0):
            raise ValueError("min_consensus_score must be in (0, 1]")
        self._min_sources = min_consensus_sources
        self._min_score = min_consensus_score

    # ── Computations driven by source state ────────────────────

    @staticmethod
    def compute_consensus(
        *,
        active_count: int,
        dissent_count: int,
    ) -> float:
        """Fraction of non-retracted sources that are 'active' (agreeing).

        ``active`` sources support the canonical statement; ``dissenting``
        sources oppose it. Returns 0.0 when there are no sources at all.
        """
        total = active_count + dissent_count
        if total <= 0:
            return 0.0
        return active_count / total

    def evaluate(
        self,
        *,
        current_state: LifecycleState,
        active_count: int,
        dissent_count: int,
        superseded_by: Optional[str],
    ) -> LifecycleDecision:
        """Compute the new state from source counts.

        Operator-set states (``canonical``, ``deprecated``) are sticky:
        only explicit ``demote`` / unset operations can leave them.
        """
        consensus_score = self.compute_consensus(
            active_count=active_count, dissent_count=dissent_count
        )

        # Deprecated is terminal until superseded_by is cleared.
        if superseded_by:
            if current_state != LifecycleState.DEPRECATED:
                return LifecycleDecision(
                    state=LifecycleState.DEPRECATED,
                    consensus_score=consensus_score,
                    change_type="superseded",
                )
            return LifecycleDecision(
                state=LifecycleState.DEPRECATED,
                consensus_score=consensus_score,
                change_type=None,
            )

        # Dissent forces contested (overrides everything except canonical/deprecated)
        if dissent_count > 0:
            if current_state == LifecycleState.CANONICAL:
                # Canonical stays — admin must explicitly demote or
                # mark contested. We still report the consensus_score
                # so observability sees the regression.
                return LifecycleDecision(
                    state=LifecycleState.CANONICAL,
                    consensus_score=consensus_score,
                    change_type=None,
                )
            if current_state != LifecycleState.CONTESTED:
                return LifecycleDecision(
                    state=LifecycleState.CONTESTED,
                    consensus_score=consensus_score,
                    change_type="marked_contested",
                )
            return LifecycleDecision(
                state=LifecycleState.CONTESTED,
                consensus_score=consensus_score,
                change_type=None,
            )

        # Canonical is operator-set; never auto-leave it.
        if current_state == LifecycleState.CANONICAL:
            return LifecycleDecision(
                state=LifecycleState.CANONICAL,
                consensus_score=consensus_score,
                change_type=None,
            )

        # Promotion candidate: enough sources & high agreement.
        if (
            active_count >= self._min_sources
            and consensus_score >= self._min_score
        ):
            target = LifecycleState.CONSENSUS
        else:
            target = LifecycleState.TRIBAL

        if target == current_state:
            return LifecycleDecision(
                state=target,
                consensus_score=consensus_score,
                change_type=None,
            )
        change = (
            "promoted"
            if target == LifecycleState.CONSENSUS
            else "demoted"
        )
        return LifecycleDecision(
            state=target,
            consensus_score=consensus_score,
            change_type=change,
        )

    # ── Operator transitions ────────────────────────────────────

    def promote_to_canonical(
        self, current_state: LifecycleState
    ) -> LifecycleDecision:
        if current_state == LifecycleState.DEPRECATED:
            raise ValueError("cannot promote a deprecated card")
        if current_state == LifecycleState.CANONICAL:
            return LifecycleDecision(
                state=LifecycleState.CANONICAL,
                consensus_score=1.0,
                change_type=None,
            )
        return LifecycleDecision(
            state=LifecycleState.CANONICAL,
            consensus_score=1.0,
            change_type="promoted",
        )

    def demote(
        self,
        current_state: LifecycleState,
        *,
        active_count: int,
        dissent_count: int,
    ) -> LifecycleDecision:
        """Re-evaluate non-operator state after an admin demotes."""
        return self.evaluate(
            current_state=LifecycleState.TRIBAL,
            active_count=active_count,
            dissent_count=dissent_count,
            superseded_by=None,
        )

    def supersede(
        self, current_state: LifecycleState, *, superseded_by: str
    ) -> LifecycleDecision:
        if not superseded_by:
            raise ValueError("superseded_by must be set")
        return LifecycleDecision(
            state=LifecycleState.DEPRECATED,
            consensus_score=0.0,
            change_type="superseded",
        )
