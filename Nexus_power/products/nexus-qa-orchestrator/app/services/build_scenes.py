"""
Scene Builder � Phase 3 algorithm (orchestrator re-export).

The canonical algorithm lives in nexus_sdk.evidence.build_scenes.
Orchestrator code imports from this path for clarity.
"""
from nexus_sdk.evidence.build_scenes import (  # noqa: F401
    SCENE_BOUNDARY_THRESHOLD,
    build_scenes,
)
