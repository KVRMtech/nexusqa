"""
Nexus Orchestrator — Generic workflow engine.

This module provides the canonical import path for the workflow engine.
The actual implementation lives in products/nexus-qa-orchestrator/app/workflows/
and will be migrated here incrementally.

Usage:
    from platform.orchestrator import (
        ChainDefinition,
        ChainEngine,
        ChainRegistry,
        WorkflowContext,
    )

Architecture:
    The orchestrator is a generic DAG-based workflow engine that:
    1. Accepts chain definitions (DAGs of stages)
    2. Resolves engine URLs for each stage
    3. Executes stages in dependency order with retry logic
    4. Provides $-path variable resolution between stages
    5. Persists workflow state for resumability

    Domain-specific chains (QA testing, compliance audit, etc.)
    are provided by product plugins, NOT hardcoded in the orchestrator.
"""

from __future__ import annotations

import importlib
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)

# ─── Dynamic re-export from existing location ─────────────────
# The generic workflow engine currently lives at:
#   products/nexus-qa-orchestrator/app/workflows/
#
# We re-export from there to establish the canonical import path.
# As the migration progresses, code will move here directly.

# Resolve to the repository root (two levels up from platform/orchestrator/)
# then into products/nexus-qa-orchestrator/app where the workflow engine lives.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_ORCHESTRATOR_ROOT = (
    _REPO_ROOT
    / "products"
    / "nexus-qa-orchestrator"
    / "app"
)

if str(_ORCHESTRATOR_ROOT) not in sys.path:
    sys.path.insert(0, str(_ORCHESTRATOR_ROOT))

try:
    from workflows import (  # type: ignore[import-untyped]
        ChainDefinition,
        ChainEngine,
        ChainListItem,
        ChainRegistry,
        EngineURLResolver,
        FileStore,
        PollingConfig,
        RetryPolicy,
        StageDefinition,
        StageExecution,
        StageStatus,
        StartWorkflowRequest,
        StartWorkflowResponse,
        WorkflowContext,
        WorkflowInstance,
        WorkflowStatus,
        WorkflowStore,
        WorkflowSummary,
    )

    _AVAILABLE = True
except ImportError as _exc:
    logger.debug(
        "Orchestrator workflow engine not available at %s: %s",
        _ORCHESTRATOR_ROOT,
        _exc,
    )
    _AVAILABLE = False


def is_available() -> bool:
    """Check if the orchestrator engine is importable."""
    return _AVAILABLE


if TYPE_CHECKING:
    # Provide type stubs for static analysis even when not importable
    from workflows import (  # noqa: F811  # type: ignore[import-not-found]
        ChainDefinition,
        ChainEngine,
        ChainListItem,
        ChainRegistry,
        EngineURLResolver,
        FileStore,
        PollingConfig,
        RetryPolicy,
        StageDefinition,
        StageExecution,
        StageStatus,
        StartWorkflowRequest,
        StartWorkflowResponse,
        WorkflowContext,
        WorkflowInstance,
        WorkflowStatus,
        WorkflowStore,
        WorkflowSummary,
    )


__all__ = [
    "ChainDefinition",
    "ChainEngine",
    "ChainListItem",
    "ChainRegistry",
    "EngineURLResolver",
    "FileStore",
    "PollingConfig",
    "RetryPolicy",
    "StageDefinition",
    "StageExecution",
    "StageStatus",
    "StartWorkflowRequest",
    "StartWorkflowResponse",
    "WorkflowContext",
    "WorkflowInstance",
    "WorkflowStatus",
    "WorkflowStore",
    "WorkflowSummary",
    "is_available",
]
