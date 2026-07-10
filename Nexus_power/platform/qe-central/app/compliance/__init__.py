"""QE-Central Phase-8 — SOC2 / regulated compliance (design SEAM E).

Compliance-framework ADAPTERS as READ-ONLY PROJECTIONS over the evidence
VKPower-Verdict already captures (hash-chained ``verdict_events``, reproducible
``decision_dossiers``, governed ``verification_waivers``, immutable
``audit_log``).  The adapters capture nothing new and mutate nothing: they re-
frame trust the hash chains already carry into the control-mapping shape a
specific framework expects (SOC 2, the NAIC Model Audit Rule, the EU AI-Act
technical-documentation profile).

Public surface:
  * :func:`get_adapter` / :func:`available_frameworks` / :func:`build_report` —
    look up a framework and project a bundle (unknown → :class:`UnknownFrameworkError`).
  * :class:`EvidenceBundle` / :class:`EvidenceWindow` — the read-only snapshot
    the adapters consume.
  * :func:`load_evidence` — the live, best-effort, tenant-scoped reader that
    materialises a bundle from the nexus substrate DB.
  * :func:`verify_verdict_chains` / :func:`report_digest` — the tamper-evidence
    and deterministic-digest primitives.
"""
from __future__ import annotations

from .adapter import (
    ADAPTER_VERSION,
    CHAIN_REGISTRY_VERSION,
    ComplianceAdapter,
    ControlSpec,
    EUAIActAnnex22Adapter,
    EvidenceBundle,
    EvidenceWindow,
    NAICModelAuditAdapter,
    SOC2Adapter,
    UnknownFrameworkError,
    available_frameworks,
    build_report,
    canonical_verdict_payload,
    compute_chain_hash,
    get_adapter,
    report_digest,
    verify_verdict_chains,
)
from .loader import load_evidence

__all__ = [
    "ADAPTER_VERSION",
    "CHAIN_REGISTRY_VERSION",
    "ComplianceAdapter",
    "ControlSpec",
    "EUAIActAnnex22Adapter",
    "EvidenceBundle",
    "EvidenceWindow",
    "NAICModelAuditAdapter",
    "SOC2Adapter",
    "UnknownFrameworkError",
    "available_frameworks",
    "build_report",
    "canonical_verdict_payload",
    "compute_chain_hash",
    "get_adapter",
    "load_evidence",
    "report_digest",
    "verify_verdict_chains",
]
