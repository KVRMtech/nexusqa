"""
Brain Engine — Coordinator module.

Manages cross-engine intelligence, decision-making,
quality gates, and confidence-based routing.
"""

from app.coordinator.decision_engine import DecisionEngine, DecisionContext, Decision
from app.coordinator.quality_gate import QualityGate, QualityScore
from app.coordinator.session_reasoner import SessionReasoner

__all__ = [
    "DecisionEngine",
    "DecisionContext",
    "Decision",
    "QualityGate",
    "QualityScore",
    "SessionReasoner",
]
