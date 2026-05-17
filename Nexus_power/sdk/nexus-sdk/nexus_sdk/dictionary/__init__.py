"""UI dictionary — per-tenant knowledge accumulation across recordings.

The dictionary is the canonical pipeline's long-term memory.  Every time
the orchestration extracts a control from a scene, it consults the
dictionary first; if a match exists the prior selector and action_kind
are inherited along with their accumulated confidence, and only the
delta (e.g. a new bounding-box centre) is updated.  When a control is
new the row is created fresh with recognition_count=1.

Public surface:

  * :func:`compute_element_signature` — stable identity for a control
  * :class:`UIDictionary` — load/lookup/register against a tenant scope

The implementation is transport-agnostic: callers pass an active
SQLAlchemy ``AsyncSession`` so the dictionary participates in the
caller's transaction (the spine engine bundles dictionary updates with
evidence_controls inserts so a partial failure rolls everything back).
"""

from .registry import UIDictionary, compute_element_signature

__all__ = [
    "UIDictionary",
    "compute_element_signature",
]
