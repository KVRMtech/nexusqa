"""nexus_sdk.evidence — canonical evidence processing algorithms.

Pure-Python modules shared across the Spine engine and the canonical
orchestrator.  No FastAPI, no SQLAlchemy, no network I/O.
"""

from .click_synthesizer import (
    ClickSynthesizerConfig,
    synthesize_clicks,
)

__all__ = [
    "ClickSynthesizerConfig",
    "synthesize_clicks",
]
