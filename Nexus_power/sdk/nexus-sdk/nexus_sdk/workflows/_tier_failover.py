"""
Shared tier-failover primitive.

The text LLM router (``nexus_sdk.llm.tiered.TieredLLMRouter``) and the
vision router (``nexus_sdk.vision.tiered.VisionTierRouter``) share the
same conceptual machinery:

  1. Walk providers in tier order
  2. Skip a tier when its circuit breaker is open
  3. Try the tier; on failure, either retry (transient) or skip
  4. Raise when every tier is exhausted

The two routers can't share an entire base class because their request
and response types diverge (single text prompt vs image-and-OCR
request; per-tenant budget gating only applies to text). But the
circuit-breaker state-machine itself is identical.

This module exposes that state machine as `TierCircuitBreaker` — a
plain data class with three operations:

  - ``should_skip(tier_index, now) -> bool``
  - ``record_success(tier_index, now)``
  - ``record_failure(tier_index, now)``

Both routers use this; behavior is preserved bit-for-bit relative to
the pre-refactor implementations. Tests in the consumer modules cover
the behavior; this module is intentionally minimal so it stays trivial
to audit.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class _TierState:
    """Per-tier counters. Pure data; no behavior."""
    consecutive_failures: int = 0
    last_failure_epoch: float = 0.0
    total_failures: int = 0
    total_successes: int = 0


@dataclass
class TierCircuitBreaker:
    """Coordinates skip/try decisions for a set of tier indices.

    ``cooldown_seconds``: how long after the failure threshold a tier
    stays "open" (skipped). Default 60s matches the legacy router.

    ``failure_threshold``: consecutive failures before the breaker opens.

    ``never_skip_last``: when True, the LAST tier in the configured
    sequence is always tried even if its breaker is open. This is the
    legacy default — it means a dead final fallback hangs every
    request. The text router exposes ``LLM_TIER_FAIL_FAST_LAST=true``
    to flip this to False; vision can do the same.
    """

    cooldown_seconds: float = 60.0
    failure_threshold: int = 2
    never_skip_last: bool = True

    _state: dict[int, _TierState] = field(default_factory=dict)
    _last_tier_index: int = -1

    def configure(self, tier_indices: list[int]) -> None:
        """Initialize state for the configured tier order. Idempotent.

        Pass the *index* of each tier (not the tier value). The order
        matters: the last index is the one ``never_skip_last`` protects.
        """
        for idx in tier_indices:
            self._state.setdefault(idx, _TierState())
        if tier_indices:
            self._last_tier_index = tier_indices[-1]

    def should_skip(self, tier_index: int, now: float | None = None) -> bool:
        """True when this tier is in cool-down AND not exempted."""
        st = self._state.get(tier_index)
        if st is None:
            return False
        if self.never_skip_last and tier_index == self._last_tier_index:
            return False
        if st.consecutive_failures < self.failure_threshold:
            return False
        now = now if now is not None else time.time()
        return (now - st.last_failure_epoch) < self.cooldown_seconds

    def record_success(self, tier_index: int, now: float | None = None) -> None:
        st = self._state.setdefault(tier_index, _TierState())
        st.consecutive_failures = 0
        st.total_successes += 1

    def record_failure(self, tier_index: int, now: float | None = None) -> None:
        st = self._state.setdefault(tier_index, _TierState())
        st.consecutive_failures += 1
        st.last_failure_epoch = now if now is not None else time.time()
        st.total_failures += 1

    def stats(self) -> dict[int, dict[str, int | float]]:
        """For diagnostics / health endpoints."""
        return {
            idx: {
                "consecutive_failures": st.consecutive_failures,
                "total_failures": st.total_failures,
                "total_successes": st.total_successes,
                "last_failure_epoch": st.last_failure_epoch,
            }
            for idx, st in self._state.items()
        }


__all__ = ["TierCircuitBreaker"]
