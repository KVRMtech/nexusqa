"""QE-Central S5 — admission control + politeness (design §3.5).

A VENDORED HealScheduler-shaped admission controller (global + per-tenant
concurrency caps + round-robin fairness) fused with a per-canonical-host token
bucket (rate = ``fences.max_rps``, burst 2×) and a per-host mutex, so a fleet
of cycles can never (a) starve a tenant, nor (b) read as an attack on a
CUSTOMER's app.  Everything is IN-MEMORY and buckets start EMPTY, so a restart
can never produce a politeness burst.
"""
from .admission import (  # noqa: F401
    ADMISSION,
    AdmissionController,
    AdmissionDecision,
    AdmissionLease,
    TokenBucket,
    build_default_controller,
)

__all__ = [
    "ADMISSION",
    "AdmissionController",
    "AdmissionDecision",
    "AdmissionLease",
    "TokenBucket",
    "build_default_controller",
]
