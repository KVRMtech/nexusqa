"""Standalone unit test for T5.5 — the 3-tier heal policy.

No DB, no network, no VM. Run from the package dir:
    python -m pytest Nexus_power/platform/api/app/services/diff_and_heal/test_heal_policy.py -q
or as a script (falls back to a tiny runner if pytest is absent):
    python Nexus_power/platform/api/app/services/diff_and_heal/test_heal_policy.py

Doctrine asserted here:
  * DEFAULT-OFF => every input the upstream gates already cleared returns AUTO
    (byte-identical to today; the working control-kind demo still auto-persists).
  * The policy can ONLY make persistence STRICTER (auto -> approve -> fail); it
    never promotes and never relaxes a gate.
  * A refuse-cause or an unconfirmed green can NEVER be AUTO, even with the flag
    off (defense in depth).
  * may_auto_persist is True iff tier == AUTO.
"""
from __future__ import annotations

import importlib
import os
import sys

# Import the module under test regardless of how the test dir is invoked.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import heal_policy  # noqa: E402


def _reset_env(monkeypatch_like: dict | None = None):
    """Clear every env var the policy reads so each case starts from defaults."""
    for k in (
        "NEXUS_HEAL_POLICY",
        "NEXUS_HEAL_POLICY_AUTO_THRESHOLD",
        "NEXUS_HEAL_POLICY_APPROVE_THRESHOLD",
    ):
        os.environ.pop(k, None)
    if monkeypatch_like:
        os.environ.update(monkeypatch_like)
    importlib.reload(heal_policy)  # re-read module-level constants if any


# ── default-off: strict no-op (byte-identical to today) ──────────────────────

def test_disabled_returns_auto_for_grounded_high_conf():
    _reset_env()
    r = heal_policy.evaluate_heal_tier(confidence=0.85, outcome_grounded=True,
                                       cause="", confirmed_green=True)
    assert r["tier"] == heal_policy.TIER_AUTO
    assert r["may_auto_persist"] is True


def test_disabled_returns_auto_even_for_marginal():
    """With the flag OFF the policy must be a no-op: a marginal heal that the
    upstream gates passed still auto-persists exactly like today."""
    _reset_env()
    r = heal_policy.evaluate_heal_tier(confidence=0.60, outcome_grounded=False,
                                       cause="", confirmed_green=True)
    assert r["tier"] == heal_policy.TIER_AUTO
    assert r["may_auto_persist"] is True


def test_disabled_still_fails_refuse_cause():
    """Defense in depth: a leaked refuse-cause is FAIL even with the flag off."""
    _reset_env()
    r = heal_policy.evaluate_heal_tier(confidence=0.99, outcome_grounded=True,
                                       cause="REAL_REGRESSION", confirmed_green=True)
    assert r["tier"] == heal_policy.TIER_FAIL
    assert r["may_auto_persist"] is False


def test_disabled_still_fails_unconfirmed_green():
    _reset_env()
    r = heal_policy.evaluate_heal_tier(confidence=0.99, outcome_grounded=True,
                                       cause="", confirmed_green=False)
    assert r["tier"] == heal_policy.TIER_FAIL
    assert r["may_auto_persist"] is False


# ── enabled: the demo heal still auto-persists (no regression) ───────────────

def test_enabled_control_kind_demo_still_auto():
    """The working demo: a grounded <select> control-kind heal (diagnose
    confidence 0.85, outcome-grounded). Enabling the policy must NOT regress it."""
    _reset_env({"NEXUS_HEAL_POLICY": "1"})
    r = heal_policy.evaluate_heal_tier(confidence=0.85, outcome_grounded=True,
                                       cause="", confirmed_green=True)
    assert r["tier"] == heal_policy.TIER_AUTO
    assert r["may_auto_persist"] is True


def test_enabled_at_auto_boundary_is_auto():
    _reset_env({"NEXUS_HEAL_POLICY": "1"})
    r = heal_policy.evaluate_heal_tier(confidence=0.80, outcome_grounded=True,
                                       cause="", confirmed_green=True)
    assert r["tier"] == heal_policy.TIER_AUTO


# ── enabled: STRICTER for the marginal heals (the only behavior change) ──────

def test_enabled_marginal_confidence_needs_approve():
    _reset_env({"NEXUS_HEAL_POLICY": "1"})
    r = heal_policy.evaluate_heal_tier(confidence=0.60, outcome_grounded=True,
                                       cause="", confirmed_green=True)
    assert r["tier"] == heal_policy.TIER_APPROVE
    assert r["may_auto_persist"] is False


def test_enabled_proven_but_not_outcome_grounded_needs_approve():
    """A green that re-ran green but asserts NO recorded outcome can never be AUTO
    when the policy is on — it must STOP at needs_human."""
    _reset_env({"NEXUS_HEAL_POLICY": "1"})
    r = heal_policy.evaluate_heal_tier(confidence=0.90, outcome_grounded=False,
                                       cause="", confirmed_green=True)
    assert r["tier"] == heal_policy.TIER_APPROVE
    assert r["may_auto_persist"] is False


def test_enabled_low_confidence_fails():
    _reset_env({"NEXUS_HEAL_POLICY": "1"})
    r = heal_policy.evaluate_heal_tier(confidence=0.30, outcome_grounded=True,
                                       cause="", confirmed_green=True)
    assert r["tier"] == heal_policy.TIER_FAIL
    assert r["may_auto_persist"] is False


def test_enabled_low_conf_and_not_grounded_fails():
    _reset_env({"NEXUS_HEAL_POLICY": "1"})
    r = heal_policy.evaluate_heal_tier(confidence=0.10, outcome_grounded=False,
                                       cause="", confirmed_green=True)
    assert r["tier"] == heal_policy.TIER_FAIL


# ── invariants ───────────────────────────────────────────────────────────────

def test_only_auto_may_persist_invariant():
    """may_auto_persist is True iff tier == AUTO, across the grid."""
    for enabled in (False, True):
        _reset_env({"NEXUS_HEAL_POLICY": "1"} if enabled else None)
        for conf in (0.0, 0.3, 0.55, 0.6, 0.79, 0.8, 0.85, 1.0):
            for grounded in (False, True):
                for cause in ("", "REAL_REGRESSION"):
                    for confirmed in (True, False):
                        r = heal_policy.evaluate_heal_tier(
                            confidence=conf, outcome_grounded=grounded,
                            cause=cause, confirmed_green=confirmed,
                        )
                        assert (r["tier"] == heal_policy.TIER_AUTO) == r["may_auto_persist"]


def test_enabling_is_monotonically_stricter():
    """For EVERY input, enabling the policy can only keep or lower the persist
    decision (auto -> approve -> fail); it can never make a non-AUTO input AUTO."""
    rank = {heal_policy.TIER_AUTO: 2, heal_policy.TIER_APPROVE: 1, heal_policy.TIER_FAIL: 0}
    grid = []
    _reset_env()
    for conf in (0.2, 0.55, 0.6, 0.79, 0.8, 0.85, 0.95):
        for grounded in (False, True):
            for cause in ("", "VARIANT_SUSPECTED"):
                for confirmed in (True, False):
                    off = heal_policy.evaluate_heal_tier(
                        confidence=conf, outcome_grounded=grounded,
                        cause=cause, confirmed_green=confirmed)["tier"]
                    grid.append((conf, grounded, cause, confirmed, off))
    _reset_env({"NEXUS_HEAL_POLICY": "1"})
    for conf, grounded, cause, confirmed, off in grid:
        on = heal_policy.evaluate_heal_tier(
            confidence=conf, outcome_grounded=grounded,
            cause=cause, confirmed_green=confirmed)["tier"]
        assert rank[on] <= rank[off], (
            f"enabling promoted {off}->{on} for conf={conf} grounded={grounded} "
            f"cause={cause!r} confirmed={confirmed}")


def test_misconfigured_thresholds_dont_invert():
    """approve > auto must be clamped so the rungs can't invert."""
    _reset_env({
        "NEXUS_HEAL_POLICY": "1",
        "NEXUS_HEAL_POLICY_AUTO_THRESHOLD": "0.50",
        "NEXUS_HEAL_POLICY_APPROVE_THRESHOLD": "0.90",
    })
    # appr clamped to auto (0.50); a 0.50 grounded confirmed heal is AUTO, not below-approve.
    r = heal_policy.evaluate_heal_tier(confidence=0.50, outcome_grounded=True,
                                       cause="", confirmed_green=True)
    assert r["tier"] == heal_policy.TIER_AUTO


def test_garbage_confidence_is_safe():
    _reset_env({"NEXUS_HEAL_POLICY": "1"})
    r = heal_policy.evaluate_heal_tier(confidence=None, outcome_grounded=True,  # type: ignore[arg-type]
                                       cause="", confirmed_green=True)
    assert r["tier"] == heal_policy.TIER_FAIL  # coerced to 0.0


if __name__ == "__main__":
    # Minimal runner so this works without pytest installed.
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        try:
            fn()
        except AssertionError as e:
            print(f"FAIL {fn.__name__}: {e}")
            raise
        else:
            passed += 1
            print(f"ok   {fn.__name__}")
    print(f"\n{passed}/{len(fns)} passed")
