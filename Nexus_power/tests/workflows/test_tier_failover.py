"""
Tests for the shared TierCircuitBreaker primitive.

This is consumed by both the text-LLM tier router and the vision tier
router. Each consumer has its own behavior tests that exercise the
primitive in context; these tests cover the primitive in isolation so
a regression here surfaces with a clear cause.
"""

from __future__ import annotations

import pytest

from nexus_sdk.workflows._tier_failover import TierCircuitBreaker


def test_breaker_initially_does_not_skip_anything():
    cb = TierCircuitBreaker()
    cb.configure([0, 1, 2])
    assert not cb.should_skip(0, now=0.0)
    assert not cb.should_skip(1, now=0.0)
    assert not cb.should_skip(2, now=0.0)


def test_breaker_opens_after_threshold_within_cooldown():
    cb = TierCircuitBreaker(cooldown_seconds=60.0, failure_threshold=2)
    cb.configure([0, 1, 2])
    cb.record_failure(0, now=100.0)
    assert not cb.should_skip(0, now=101.0)  # only 1 failure so far
    cb.record_failure(0, now=110.0)
    # 2 consecutive failures within cooldown → breaker open
    assert cb.should_skip(0, now=120.0)
    # After cooldown elapses, breaker closes again.
    assert not cb.should_skip(0, now=200.0)


def test_breaker_success_resets_consecutive_failures():
    cb = TierCircuitBreaker(cooldown_seconds=60.0, failure_threshold=2)
    cb.configure([0, 1])
    cb.record_failure(0, now=100.0)
    cb.record_failure(0, now=101.0)
    assert cb.should_skip(0, now=110.0)
    cb.record_success(0, now=112.0)
    # Successes reset the consecutive-failure counter, so the breaker
    # closes immediately on the next check.
    assert not cb.should_skip(0, now=113.0)


def test_breaker_protects_last_tier_by_default():
    """Legacy behavior: the last tier is the fallback-of-last-resort and
    never gets skipped. Used by both routers pre-refactor."""
    cb = TierCircuitBreaker(
        cooldown_seconds=60.0, failure_threshold=2, never_skip_last=True,
    )
    cb.configure([0, 1, 2])
    for _ in range(10):
        cb.record_failure(2, now=100.0)
    # Even after 10 failures, last tier is never skipped.
    assert not cb.should_skip(2, now=110.0)
    # But non-last tiers still get skipped.
    cb.record_failure(0, now=100.0)
    cb.record_failure(0, now=101.0)
    assert cb.should_skip(0, now=110.0)


def test_breaker_can_skip_last_tier_when_never_skip_last_disabled():
    """LLM_TIER_FAIL_FAST_LAST=true equivalent — the last tier IS
    subject to circuit-breaking."""
    cb = TierCircuitBreaker(
        cooldown_seconds=60.0, failure_threshold=2, never_skip_last=False,
    )
    cb.configure([0, 1, 2])
    cb.record_failure(2, now=100.0)
    cb.record_failure(2, now=101.0)
    assert cb.should_skip(2, now=110.0), (
        "with never_skip_last=False, even the last tier opens"
    )


def test_breaker_stats_track_totals():
    cb = TierCircuitBreaker()
    cb.configure([0, 1])
    cb.record_success(0)
    cb.record_success(0)
    cb.record_failure(0)
    cb.record_failure(1)
    stats = cb.stats()
    assert stats[0]["total_successes"] == 2
    assert stats[0]["total_failures"] == 1
    assert stats[1]["total_failures"] == 1


def test_breaker_unconfigured_index_never_skipped():
    """Defensive: a tier index the breaker doesn't know about should
    not be skipped — it has no failure history to base a decision on."""
    cb = TierCircuitBreaker()
    cb.configure([0, 1])
    assert not cb.should_skip(99, now=0.0)


def test_breaker_single_tier_configuration():
    """If only one tier is configured, the protect-last guard applies
    to it AND it's the only tier. Result: never skipped (matches the
    Tier-3-only deployment shape)."""
    cb = TierCircuitBreaker(failure_threshold=1)
    cb.configure([0])
    cb.record_failure(0)
    cb.record_failure(0)
    assert not cb.should_skip(0, now=10.0)
