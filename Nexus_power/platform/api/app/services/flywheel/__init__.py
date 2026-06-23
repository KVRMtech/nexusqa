"""Nexus flywheel (Phase 2 foundation) — the consented failure→fix data substrate.

This package CAPTURES human/outcome corrections as de-identified labeled examples
(``ledger`` + ``featurize``). It does NOT train and does NOT export by itself —
cross-customer learning + consented export are separate, default-OFF, and
deferred to real data + infra. Everything here is additive and must never alter
the deterministic verdict/heal core.

Surface (all default-OFF / inert until explicitly enabled):
  * ledger.record_label / FlywheelLabelRow  — capture (NEXUS_FLYWHEEL_CAPTURE)
  * export.build_tenant_export / seal_export — consented k-anon DP export (NEXUS_FLYWHEEL_EXPORT)
  * leakage_guard.assert_no_leakage          — the de-id boundary PROOF ($0, runnable)
  * leakage_guard.boost_confidence           — priors tie-break BOOST hook (NEXUS_FLYWHEEL_PRIORS)
  * priors.escalate_verdict                  — priors escalation hook (under the rail)
  * aggregator.fit_verdict_escalation_priors — federated fit (INFRA-GATED: N tenants + GPU)
"""

# Re-export the safe consumption surface so callers import from the package root.
# Importing this module pulls ledger -> nexus_sdk.db.Base (the SDK ORM); that's
# expected in the API runtime. Each function is itself default-OFF / inert.
from .export import (  # noqa: F401
    build_tenant_export, seal_export, aggregate_labels, add_dp_noise,
    export_enabled, DEFAULT_K,
)
from .leakage_guard import (  # noqa: F401
    assert_no_leakage, find_leaks, LeakageError, boost_confidence,
)
from .priors import priors_enabled, prior_for, escalate_verdict  # noqa: F401

__all__ = [
    "build_tenant_export", "seal_export", "aggregate_labels", "add_dp_noise",
    "export_enabled", "DEFAULT_K",
    "assert_no_leakage", "find_leaks", "LeakageError", "boost_confidence",
    "priors_enabled", "prior_for", "escalate_verdict",
]
