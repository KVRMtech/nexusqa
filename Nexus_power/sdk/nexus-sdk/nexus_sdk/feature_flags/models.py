"""Pydantic DTOs and enums for the feature flag service."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Mode(str, Enum):
    """Progression ladder for a feature.

    SHADOW   — full pipeline executes; no external side effect.
    DM_ONLY  — outbound restricted to direct messages to the asker.
    LIVE     — full outbound including channel posts.
    """

    SHADOW = "shadow"
    DM_ONLY = "dm_only"
    LIVE = "live"

    @property
    def rank(self) -> int:
        return _MODE_RANK[self]


_MODE_RANK = {Mode.SHADOW: 0, Mode.DM_ONLY: 1, Mode.LIVE: 2}


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class Outcome(str, Enum):
    """Outcomes recorded against the circuit breaker."""

    SUCCESS = "success"
    FAILURE = "failure"          # internal error during processing
    THUMBS_UP = "thumbs_up"      # positive user feedback
    THUMBS_DOWN = "thumbs_down"  # negative user feedback
    TIMEOUT = "timeout"          # processing exceeded SLO


_FAILURE_OUTCOMES: frozenset[Outcome] = frozenset(
    {Outcome.FAILURE, Outcome.THUMBS_DOWN, Outcome.TIMEOUT}
)


def is_failure_outcome(outcome: Outcome) -> bool:
    return outcome in _FAILURE_OUTCOMES


class CircuitConfig(BaseModel):
    """Tunables for the breaker.

    The breaker observes outcomes within a sliding window. When the
    failure rate exceeds ``failure_threshold`` and the window contains
    at least ``min_samples`` outcomes, the circuit trips to OPEN and
    stays there until ``cooldown_seconds`` elapses; the next outcome
    after that observed in HALF_OPEN closes the circuit on success or
    re-opens it on failure.
    """

    model_config = ConfigDict(frozen=True)

    window_seconds: int = Field(default=300, ge=10, le=3600)
    min_samples: int = Field(default=20, ge=5, le=10_000)
    failure_threshold: float = Field(default=0.30, ge=0.0, le=1.0)
    cooldown_seconds: int = Field(default=900, ge=10, le=86_400)

    @field_validator("failure_threshold")
    @classmethod
    def _threshold_strict(cls, v: float) -> float:
        if v <= 0.0:
            raise ValueError("failure_threshold must be > 0")
        return v


class FlagState(BaseModel):
    """Current state of a feature for a single tenant.

    Includes derived ``effective_mode``: the actual mode the consumer
    should honor after applying the circuit-breaker override.
    """

    model_config = ConfigDict(frozen=True)

    tenant_id: str
    feature_key: str
    enabled: bool
    mode: Mode
    config: dict
    version: int
    circuit_state: CircuitState
    cooldown_until: Optional[datetime] = None
    enabled_by: Optional[str] = None
    enabled_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    @property
    def is_inert(self) -> bool:
        """True when no behavior should fire (off or circuit open)."""
        return (not self.enabled) or self.circuit_state == CircuitState.OPEN

    @property
    def effective_mode(self) -> Mode:
        """Mode the consumer should observe after applying the breaker.

        When the circuit is OPEN we degrade to SHADOW regardless of
        the configured mode; HALF_OPEN preserves the configured mode
        to allow a canary request through.
        """
        if not self.enabled or self.circuit_state == CircuitState.OPEN:
            return Mode.SHADOW
        return self.mode

    def allows(self, required: Mode) -> bool:
        """True if the effective mode is at least ``required``."""
        return self.effective_mode.rank >= required.rank


class FlagUpdate(BaseModel):
    """Mutation payload for FeatureFlagService.set().

    All fields optional; only provided fields are written. ``actor``
    is mandatory for audit. ``expected_version`` enables optimistic
    locking — set to the version observed in a prior ``get()``.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: Optional[bool] = None
    mode: Optional[Mode] = None
    config: Optional[dict] = None
    actor: str = Field(min_length=1, max_length=128)
    expected_version: Optional[int] = Field(default=None, ge=1)


class OptimisticLockError(Exception):
    """Raised when ``expected_version`` does not match current row."""

    def __init__(self, tenant_id: str, feature_key: str, expected: int, actual: int):
        self.tenant_id = tenant_id
        self.feature_key = feature_key
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"feature_flag version mismatch for {tenant_id}/{feature_key}: "
            f"expected={expected} actual={actual}"
        )


class FeatureUnavailable(Exception):
    """Raised by ``require_feature`` when the gate denies access."""

    def __init__(self, feature_key: str, reason: str, status_code: int = 503):
        self.feature_key = feature_key
        self.reason = reason
        self.status_code = status_code
        super().__init__(f"feature unavailable: {feature_key} ({reason})")
