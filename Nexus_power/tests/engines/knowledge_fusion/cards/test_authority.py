"""AuthorityCalculator — role × recency × confirmation weighting."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app.cards.authority import (
    AuthorityCalculator,
    DEFAULT_ROLE_WEIGHTS,
)


# ── Role weighting ──────────────────────────────────────────────


def test_compliance_outweighs_sales() -> None:
    calc = AuthorityCalculator()
    assert calc.role_weight("compliance") > calc.role_weight("sales")


def test_unknown_role_falls_back_to_default() -> None:
    calc = AuthorityCalculator()
    assert calc.role_weight("astrologer") == DEFAULT_ROLE_WEIGHTS[""]


def test_role_overrides_apply() -> None:
    calc = AuthorityCalculator(role_overrides={"sales": 5.0})
    assert calc.role_weight("sales") == 5.0
    # Other roles still pull from defaults.
    assert calc.role_weight("compliance") == DEFAULT_ROLE_WEIGHTS["compliance"]


def test_invalid_override_weights_are_skipped() -> None:
    calc = AuthorityCalculator(
        role_overrides={"sales": -1.0, "trainer": "bad", "engineer": 3.0}  # type: ignore[dict-item]
    )
    assert calc.role_weight("sales") == DEFAULT_ROLE_WEIGHTS["sales"]
    assert calc.role_weight("engineer") == 3.0


def test_role_suffix_match() -> None:
    """``senior_compliance`` should pick up compliance weight via token match."""
    calc = AuthorityCalculator()
    assert calc.role_weight("senior_compliance") == DEFAULT_ROLE_WEIGHTS["compliance"]


# ── Recency ─────────────────────────────────────────────────────


def test_recency_factor_decays_with_age() -> None:
    calc = AuthorityCalculator(recency_floor=0.01)
    now = datetime(2026, 5, 12, tzinfo=timezone.utc)
    fresh = calc.recency_factor(
        date(2026, 5, 10), halflife_days=270, now=now
    )
    stale = calc.recency_factor(
        date(2020, 5, 10), halflife_days=270, now=now
    )
    assert fresh > stale
    assert fresh > 0.99
    assert stale < 0.05 + 1e-9


def test_recency_factor_floor() -> None:
    calc = AuthorityCalculator(recency_floor=0.1)
    now = datetime(2026, 5, 12, tzinfo=timezone.utc)
    very_old = calc.recency_factor(
        date(1990, 1, 1), halflife_days=30, now=now
    )
    assert very_old == pytest.approx(0.1)


def test_recency_none_collapses_to_one() -> None:
    calc = AuthorityCalculator()
    assert calc.recency_factor(None, halflife_days=270) == 1.0


# ── Confirmation boost ─────────────────────────────────────────


def test_confirmation_boost_monotonic_with_diminishing_returns() -> None:
    """Boost is monotonic; each *additional* confirmation contributes
    less than the previous one (log-shaped curve)."""
    calc = AuthorityCalculator(confirmation_ceiling=10.0)
    one = calc.confirmation_boost(1)
    two = calc.confirmation_boost(2)
    ten = calc.confirmation_boost(10)
    eleven = calc.confirmation_boost(11)
    # Strict monotonicity in n.
    assert one < two < ten < eleven
    # Diminishing returns: marginal gain from 10→11 is smaller than 1→2.
    assert (two - one) > (eleven - ten)


def test_confirmation_boost_capped() -> None:
    calc = AuthorityCalculator(confirmation_ceiling=1.5)
    assert calc.confirmation_boost(1_000_000) == pytest.approx(1.5)


# ── Composite contribution ─────────────────────────────────────


def test_contribution_combines_all_three_factors() -> None:
    calc = AuthorityCalculator()
    now = datetime(2026, 5, 12, tzinfo=timezone.utc)
    contrib = calc.contribution(
        sme_id="alice",
        sme_role="compliance",
        stated_at=date(2026, 5, 10),
        halflife_days=270,
        prior_contributing_count=3,
        now=now,
    )
    expected = (
        contrib.role_weight * contrib.recency_factor * contrib.confirmation_boost
    )
    assert contrib.weight == pytest.approx(expected)
    assert contrib.weight > 0.0


# ── Saturating confidence ─────────────────────────────────────


def test_canonical_confidence_saturates_in_range() -> None:
    # Zero weight → 0 confidence.
    assert AuthorityCalculator.canonical_confidence([]) == 0.0
    # Small positive weight gives small but nonzero confidence.
    low = AuthorityCalculator.canonical_confidence(
        [0.5], saturation_weight=6.0
    )
    assert 0.0 < low < 0.2
    # Saturation weight produces ~63%.
    mid = AuthorityCalculator.canonical_confidence(
        [6.0], saturation_weight=6.0
    )
    assert 0.55 < mid < 0.70
    # Very high weight approaches 1.
    high = AuthorityCalculator.canonical_confidence(
        [60.0], saturation_weight=6.0
    )
    assert high > 0.99
    assert high < 1.0
