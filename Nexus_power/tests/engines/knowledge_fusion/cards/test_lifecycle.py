"""LifecycleManager — state transitions + consensus computation."""

from __future__ import annotations

import pytest

from app.cards.lifecycle import (
    LifecycleDecision,
    LifecycleManager,
    LifecycleState,
)


def _mgr() -> LifecycleManager:
    return LifecycleManager(min_consensus_sources=3, min_consensus_score=0.80)


def test_consensus_is_active_over_total() -> None:
    assert LifecycleManager.compute_consensus(active_count=3, dissent_count=1) == 0.75
    assert LifecycleManager.compute_consensus(active_count=0, dissent_count=0) == 0.0


def test_tribal_stays_tribal_below_threshold() -> None:
    d = _mgr().evaluate(
        current_state=LifecycleState.TRIBAL,
        active_count=2,
        dissent_count=0,
        superseded_by=None,
    )
    assert d.state == LifecycleState.TRIBAL
    assert d.change_type is None


def test_promotes_to_consensus_when_thresholds_met() -> None:
    d = _mgr().evaluate(
        current_state=LifecycleState.TRIBAL,
        active_count=5,
        dissent_count=0,
        superseded_by=None,
    )
    assert d.state == LifecycleState.CONSENSUS
    assert d.change_type == "promoted"


def test_demotes_from_consensus_when_sources_drop() -> None:
    d = _mgr().evaluate(
        current_state=LifecycleState.CONSENSUS,
        active_count=2,
        dissent_count=0,
        superseded_by=None,
    )
    assert d.state == LifecycleState.TRIBAL
    assert d.change_type == "demoted"


def test_any_dissent_forces_contested_from_tribal() -> None:
    d = _mgr().evaluate(
        current_state=LifecycleState.TRIBAL,
        active_count=5,
        dissent_count=1,
        superseded_by=None,
    )
    assert d.state == LifecycleState.CONTESTED
    assert d.change_type == "marked_contested"


def test_canonical_is_sticky_against_dissent() -> None:
    """Canonical never auto-demotes; admin must demote or contest."""
    d = _mgr().evaluate(
        current_state=LifecycleState.CANONICAL,
        active_count=5,
        dissent_count=2,
        superseded_by=None,
    )
    assert d.state == LifecycleState.CANONICAL
    assert d.change_type is None


def test_canonical_is_sticky_against_source_drop() -> None:
    d = _mgr().evaluate(
        current_state=LifecycleState.CANONICAL,
        active_count=1,
        dissent_count=0,
        superseded_by=None,
    )
    assert d.state == LifecycleState.CANONICAL
    assert d.change_type is None


def test_superseded_overrides_everything() -> None:
    d = _mgr().evaluate(
        current_state=LifecycleState.CANONICAL,
        active_count=5,
        dissent_count=0,
        superseded_by="card-99",
    )
    assert d.state == LifecycleState.DEPRECATED
    assert d.change_type == "superseded"


def test_promote_to_canonical_from_tribal() -> None:
    d = _mgr().promote_to_canonical(LifecycleState.TRIBAL)
    assert d.state == LifecycleState.CANONICAL
    assert d.change_type == "promoted"


def test_promote_idempotent_when_already_canonical() -> None:
    d = _mgr().promote_to_canonical(LifecycleState.CANONICAL)
    assert d.state == LifecycleState.CANONICAL
    assert d.change_type is None


def test_promote_rejects_deprecated() -> None:
    with pytest.raises(ValueError):
        _mgr().promote_to_canonical(LifecycleState.DEPRECATED)


def test_supersede_requires_target() -> None:
    with pytest.raises(ValueError):
        _mgr().supersede(LifecycleState.TRIBAL, superseded_by="")


def test_min_consensus_validation() -> None:
    with pytest.raises(ValueError):
        LifecycleManager(min_consensus_sources=1)
    with pytest.raises(ValueError):
        LifecycleManager(min_consensus_score=0.0)
    with pytest.raises(ValueError):
        LifecycleManager(min_consensus_score=1.1)
