"""Nexus audio intelligence — SME-narration analysis layered on transcripts.

This package operates on the transcript segments produced by the Ears
engine.  It does not perform speech recognition itself; it consumes the
already-transcribed segment list and surfaces structured intelligence:

  * :func:`extract_intent_events` — turns "now I'll click submit" into a
    timestamped :class:`IntentEvent` that downstream stages can use to
    anchor scene boundaries, force frame-burst windows, and seed the
    triangulated action classifier (Phase B.3).

The functions here are pure and deterministic — no LLM calls, no
network — so they are safe to invoke inline in the canonical
processing chain without latency or cost concerns.
"""

from .intent_extractor import (
    INTENT_KINDS,
    IntentEvent,
    extract_intent_events,
)

__all__ = [
    "INTENT_KINDS",
    "IntentEvent",
    "extract_intent_events",
]
