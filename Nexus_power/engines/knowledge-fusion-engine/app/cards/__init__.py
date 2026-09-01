"""Knowledge Cards — multi-SME synthesis layer.

Public surface:
    * ``CardRepository``         — CRUD on knowledge_cards / sources / history
    * ``CardSynthesizer``        — pipeline that turns segments into cards
    * ``AuthorityCalculator``    — role × recency × confirmation weighting
    * ``LifecycleManager``       — state machine (tribal/consensus/canonical/contested/deprecated)
    * ``ContradictionDetector``  — heuristic disagreement detector
    * DTOs: ``Card``, ``CardSource``, ``LifecycleState``, ``SourceStatus``,
            ``AuthorityContribution``
"""

from __future__ import annotations

from .authority import (
    AuthorityCalculator,
    AuthorityContribution,
    DEFAULT_ROLE_WEIGHTS,
)
from .contradiction import (
    ContradictionDetector,
    ContradictionSignal,
    HeuristicContradictionDetector,
)
from .lifecycle import (
    LifecycleManager,
    LifecycleState,
    LifecycleDecision,
)
from .models import (
    Card,
    CardSource,
    NewCardCandidate,
    SourceCandidate,
    SourceStatus,
    SourceType,
)
from .repository import CardRepository
from .synthesizer import CardSynthesizer, SynthesisResult

__all__ = [
    "AuthorityCalculator",
    "AuthorityContribution",
    "Card",
    "CardRepository",
    "CardSource",
    "CardSynthesizer",
    "ContradictionDetector",
    "ContradictionSignal",
    "DEFAULT_ROLE_WEIGHTS",
    "HeuristicContradictionDetector",
    "LifecycleDecision",
    "LifecycleManager",
    "LifecycleState",
    "NewCardCandidate",
    "SourceCandidate",
    "SourceStatus",
    "SourceType",
    "SynthesisResult",
]
