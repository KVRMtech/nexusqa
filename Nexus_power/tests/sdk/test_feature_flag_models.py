"""Unit tests for feature flag DTOs and rank/effective-mode logic.

Pure-Python — no DB or Redis required. Covers:

* Mode rank ordering (shadow < dm_only < live)
* Circuit override on FlagState.effective_mode (OPEN -> SHADOW)
* allows() semantics for routing decisions
* FlagUpdate strict validation (extra fields forbidden)
* CircuitConfig threshold validation
"""

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from nexus_sdk.feature_flags.models import (
    CircuitConfig,
    CircuitState,
    FlagState,
    FlagUpdate,
    Mode,
    Outcome,
    is_failure_outcome,
)


def _base_state(**overrides) -> FlagState:
    defaults = dict(
        tenant_id="t1",
        feature_key="knowledge_echo",
        enabled=True,
        mode=Mode.LIVE,
        config={},
        version=1,
        circuit_state=CircuitState.CLOSED,
    )
    defaults.update(overrides)
    return FlagState(**defaults)


# ── Mode ordering ────────────────────────────────────────────────


def test_mode_rank_orders_shadow_dm_live() -> None:
    assert Mode.SHADOW.rank < Mode.DM_ONLY.rank < Mode.LIVE.rank


def test_state_allows_compares_against_effective_mode() -> None:
    live = _base_state(mode=Mode.LIVE)
    assert live.allows(Mode.SHADOW)
    assert live.allows(Mode.DM_ONLY)
    assert live.allows(Mode.LIVE)

    dm = _base_state(mode=Mode.DM_ONLY)
    assert dm.allows(Mode.SHADOW)
    assert dm.allows(Mode.DM_ONLY)
    assert not dm.allows(Mode.LIVE)


# ── Circuit override ────────────────────────────────────────────


def test_open_circuit_forces_effective_mode_to_shadow() -> None:
    state = _base_state(
        mode=Mode.LIVE,
        circuit_state=CircuitState.OPEN,
        cooldown_until=datetime.now(timezone.utc) + timedelta(minutes=15),
    )
    assert state.effective_mode == Mode.SHADOW
    assert state.is_inert is True
    assert not state.allows(Mode.DM_ONLY)


def test_disabled_flag_forces_effective_mode_to_shadow() -> None:
    state = _base_state(mode=Mode.LIVE, enabled=False)
    assert state.effective_mode == Mode.SHADOW
    assert state.is_inert is True


def test_half_open_preserves_configured_mode_for_canary() -> None:
    state = _base_state(mode=Mode.LIVE, circuit_state=CircuitState.HALF_OPEN)
    assert state.effective_mode == Mode.LIVE
    assert state.is_inert is False


# ── FlagUpdate strict validation ───────────────────────────────


def test_flag_update_requires_actor() -> None:
    with pytest.raises(ValidationError):
        FlagUpdate(mode=Mode.LIVE)  # type: ignore[call-arg]


def test_flag_update_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        FlagUpdate(actor="alice", widget="unknown")  # type: ignore[call-arg]


def test_flag_update_accepts_partial_patch() -> None:
    upd = FlagUpdate(actor="alice", enabled=True)
    assert upd.enabled is True
    assert upd.mode is None
    assert upd.config is None


# ── CircuitConfig validation ───────────────────────────────────


def test_circuit_config_rejects_zero_threshold() -> None:
    with pytest.raises(ValidationError):
        CircuitConfig(failure_threshold=0.0)


def test_circuit_config_bounds() -> None:
    with pytest.raises(ValidationError):
        CircuitConfig(window_seconds=5)  # below floor
    with pytest.raises(ValidationError):
        CircuitConfig(window_seconds=4000)  # above ceiling


# ── Outcome classification ─────────────────────────────────────


def test_failure_outcomes_classified_correctly() -> None:
    for o in (Outcome.FAILURE, Outcome.THUMBS_DOWN, Outcome.TIMEOUT):
        assert is_failure_outcome(o) is True
    for o in (Outcome.SUCCESS, Outcome.THUMBS_UP):
        assert is_failure_outcome(o) is False
