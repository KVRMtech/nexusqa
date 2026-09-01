"""Phase 7 — Action layer.

Public surface:
    * ``ActionRepository``           — audit log + tours + impact CRUD
    * ``ImpactAnalyzer``             — atlas graph BFS for blast radius
    * ``TourComposer``               — atlas-walking persona-personalised
                                       playlist generator
    * ``SandboxRunner``              — calls Legs to execute a scenario
    * DTOs: ``ActionInvocation``, ``SynthesizedTour``, ``ImpactAnalysis``,
             ``TourSegment``, ``ImpactNode``, ``LayerSummary``,
             ``SandboxRequest``, ``SandboxResult``
"""

from __future__ import annotations

from .impact import (
    ImpactAnalyzer,
    ImpactAnalyzerConfig,
    ImpactNode,
    ImpactReport,
    LayerSummary,
)
from .models import (
    ActionInvocation,
    ActionKind,
    ActionStatus,
    ImpactAnalysis,
    SynthesizedTour,
    TourSegment,
    TourStatus,
)
from .repository import ActionRepository
from .sandbox import (
    SandboxClient,
    SandboxClientError,
    SandboxQuotaExceeded,
    SandboxRequest,
    SandboxResult,
    SandboxRunner,
    SandboxRunnerConfig,
)
from .tours import TourComposer, TourComposerConfig

__all__ = [
    "ActionInvocation",
    "ActionKind",
    "ActionRepository",
    "ActionStatus",
    "ImpactAnalysis",
    "ImpactAnalyzer",
    "ImpactAnalyzerConfig",
    "ImpactNode",
    "ImpactReport",
    "LayerSummary",
    "SandboxClient",
    "SandboxClientError",
    "SandboxQuotaExceeded",
    "SandboxRequest",
    "SandboxResult",
    "SandboxRunner",
    "SandboxRunnerConfig",
    "SynthesizedTour",
    "TourComposer",
    "TourComposerConfig",
    "TourSegment",
    "TourStatus",
]
