"""Per-tenant, per-feature flag service with persisted circuit breaker.

Five-level toggle:
    1. Global env switch (consumer decides; not enforced here).
    2. Per-tenant enable/disable + mode (shadow | dm_only | live).
    3. Per-tenant configuration (channels, thresholds, allow-lists).
    4. Circuit breaker (auto-trip on failure / thumbs_down spike).
    5. Per-surface / per-channel mute (lives in ``config``).

Public surface:
    - ``FeatureFlagService``         — main service
    - ``FlagState`` / ``FlagUpdate`` — DTOs
    - ``Mode``, ``CircuitState``,
      ``Outcome``                    — enums
    - ``require_feature``           — FastAPI dependency factory
    - ``CircuitConfig``             — breaker parameters
    - ``OptimisticLockError``,
      ``FeatureUnavailable``        — exceptions
"""

from __future__ import annotations

from .models import (
    CircuitConfig,
    CircuitState,
    FeatureUnavailable,
    FlagState,
    FlagUpdate,
    Mode,
    OptimisticLockError,
    Outcome,
)
from .service import FeatureFlagService
from .decorators import require_feature

__all__ = [
    "CircuitConfig",
    "CircuitState",
    "FeatureFlagService",
    "FeatureUnavailable",
    "FlagState",
    "FlagUpdate",
    "Mode",
    "OptimisticLockError",
    "Outcome",
    "require_feature",
]
