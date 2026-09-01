"""Phase 8 — Compliance evidence packager.

Public surface:
    * ``EvidencePackager``            — walks audit tables for a tenant
                                         + period and produces a signed
                                         JSON manifest plus per-scope
                                         NDJSON shards
    * ``EvidencePackagerConfig``      — packager tuning (chunk size,
                                         signing key, storage dir)
    * ``EvidenceSlice``               — single-scope packaged result
    * ``EvidenceBundle``              — manifest+slices+signature
    * ``SCOPE_TABLES``                — declarative scope -> table map
"""

from __future__ import annotations

from .packager import (
    EvidenceBundle,
    EvidencePackager,
    EvidencePackagerConfig,
    EvidencePackagerError,
    EvidenceSlice,
    SCOPE_TABLES,
    ScopeDefinition,
    UnknownScopeError,
)

__all__ = [
    "EvidenceBundle",
    "EvidencePackager",
    "EvidencePackagerConfig",
    "EvidencePackagerError",
    "EvidenceSlice",
    "SCOPE_TABLES",
    "ScopeDefinition",
    "UnknownScopeError",
]
