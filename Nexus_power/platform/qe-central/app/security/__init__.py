"""QE-Central — security spine (fail-closed onboarding + attestation gates).

Phase-6 SAFETY SPINE: the fail-closed foundation that must exist BEFORE any
real client's app or credentials touch the system.  Pure, import-light modules
the routers + the cycle driver call to REFUSE a crawl/cycle/schedule against an
app that is not cleared for it.

Public surface (imported by other modules):
  * :mod:`app.security.prod_guard` — the production onboarding guard.
  * :mod:`app.security.boot_validator` — the fail-closed boot-safety gate
    (:func:`validate_boot_safety`), called EARLY in ``main.lifespan``.
"""
from __future__ import annotations

# ``boot_validator`` is delivered by a sibling Phase-6 task.  Guard its import so
# the security package (and the independent :mod:`app.security.prod_guard`) stays
# importable whether or not that module has landed yet; any error OTHER than the
# module simply being absent still propagates loudly.
try:
    from .boot_validator import (
        BootSafetyError,
        collect_boot_violations,
        kek_health_status,
        validate_boot_safety,
    )

    _BOOT_VALIDATOR_EXPORTS = [
        "BootSafetyError",
        "collect_boot_violations",
        "kek_health_status",
        "validate_boot_safety",
    ]
except ModuleNotFoundError as _exc:  # pragma: no cover - concurrent-build guard
    if (getattr(_exc, "name", "") or "").split(".")[-1] != "boot_validator":
        raise
    _BOOT_VALIDATOR_EXPORTS = []

__all__ = [*_BOOT_VALIDATOR_EXPORTS]
