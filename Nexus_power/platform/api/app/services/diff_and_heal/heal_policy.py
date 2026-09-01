"""T5.5 — the 3-tier heal POLICY the auto-heal/TrueFix loop consults BEFORE it
persists a proven-green heal.

Doctrine (non-negotiable):
  * This function can ONLY make persistence STRICTER. It never relaxes a gate, never
    auto-persists anything today's gates would have escalated, and is INERT
    (returns AUTO with no extra friction) when the feature is off — so today's
    confirmed-green control-kind demo still auto-persists, byte-identical.
  * It runs AFTER the loop has already: confirmed 2 independent greens, rolled up
    suite_outcome_grounding (and refused a fully-hollow green), and refused every
    REAL_REGRESSION / AUTH_PRECONDITION / DATA_PRECONDITION_UNMET / VARIANT_SUSPECTED
    cause in diagnose(). So a refuse cause NEVER reaches here as auto-fixable — this
    is a SECOND, additive ceiling, not the primary safety mechanism.

Three tiers (the policy maps a heal to exactly one):
  * AUTO     — confidence >= auto_threshold AND outcome grounded AND not a
               low-confidence/suspected fix: auto-persist as today (still PROPOSED;
               the human approve gate at /approve is unchanged + RBAC-gated, T5.4).
  * APPROVE  — marginal (confidence in [approve_threshold, auto_threshold) OR a
               proven-but-NOT-outcome-grounded green): proven green, but the loop
               must STOP at needs_human and require an explicit human approve. (This
               is the only behavior change vs today, and it is strictly STRICTER —
               a marginal heal that today auto-persists now needs a human.)
  * FAIL     — confidence < approve_threshold, or an explicit refuse-cause leaked
               through (defense in depth): do NOT persist; needs_human.

DEFAULT-OFF: with ``NEXUS_HEAL_POLICY`` unset/false the policy returns AUTO for any
input the upstream gates already let through — i.e. a no-op. Thresholds are chosen
(below) so that even WHEN enabled, today's control-kind heal (diagnose confidence
0.85 for a grounded <select>, outcome-grounded) is still AUTO. So enabling the
policy never regresses the working demo; it only adds an APPROVE rung for the
marginal heals that previously slipped straight to PROPOSED.
"""

from __future__ import annotations

import os

TIER_AUTO = "auto"
TIER_APPROVE = "approve"
TIER_FAIL = "fail"

# Causes that must NEVER auto-persist. These are already refused in diagnose()
# before any heal branch; listing them here is belt-and-suspenders so that if a
# future caller wires the policy somewhere diagnose did not gate, a refuse cause
# still cannot become an AUTO heal.
_REFUSE_CAUSES = frozenset({
    "REAL_REGRESSION", "AUTH_PRECONDITION", "DATA_PRECONDITION_UNMET",
    "VARIANT_SUSPECTED",
})

# Thresholds. Defaults chosen against diagnose()'s confidence scale:
#   grounded control-kind (<select> seen) = 0.85  -> AUTO
#   suspected control-kind                = 0.60   -> APPROVE
#   re-anchor (carries its own confidence) varies  -> AUTO iff >= 0.8
#   needs_review / flake / locator        = 0.4-0.6 -> never auto-fixable anyway
_DEFAULT_AUTO = 0.80
_DEFAULT_APPROVE = 0.55


def policy_enabled() -> bool:
    """Master gate — DEFAULT OFF. Off => evaluate() returns AUTO for anything the
    upstream gates already passed (a strict no-op vs today)."""
    return os.getenv("NEXUS_HEAL_POLICY", "").strip().lower() in {"1", "true", "yes", "on"}


def _thresholds() -> tuple[float, float]:
    def _f(name: str, default: float) -> float:
        try:
            v = float(os.getenv(name, "").strip() or default)
        except (TypeError, ValueError):
            return default
        return v
    auto = _f("NEXUS_HEAL_POLICY_AUTO_THRESHOLD", _DEFAULT_AUTO)
    appr = _f("NEXUS_HEAL_POLICY_APPROVE_THRESHOLD", _DEFAULT_APPROVE)
    # Never let mis-config invert the rungs (approve must be <= auto).
    if appr > auto:
        appr = auto
    return auto, appr


def evaluate_heal_tier(
    *,
    confidence: float,
    outcome_grounded: bool,
    cause: str = "",
    confirmed_green: bool = True,
) -> dict:
    """Decide the tier for a heal the loop has ALREADY proven (2 confirmed greens,
    non-hollow suite). Returns ``{"tier", "may_auto_persist", "reason"}``.

    Invariants:
      * ``may_auto_persist`` is True ONLY for TIER_AUTO.
      * With the policy OFF, every proven green returns TIER_AUTO (no-op vs today).
      * A refuse-cause or an unconfirmed green can never return TIER_AUTO, even with
        the policy off — these are stricter-only guards (defense in depth).
      * This function can only ever DEMOTE (auto->approve->fail); it never promotes.
    """
    # Defense in depth: a leaked refuse-cause or an unconfirmed green is FAIL,
    # regardless of the feature flag — we can only ever be stricter than today.
    if cause in _REFUSE_CAUSES:
        return {"tier": TIER_FAIL, "may_auto_persist": False,
                "reason": f"refuse cause '{cause}' must never auto-persist"}
    if not confirmed_green:
        return {"tier": TIER_FAIL, "may_auto_persist": False,
                "reason": "heal not confirmed on an independent re-run — needs a human"}

    if not policy_enabled():
        # No-op: behave exactly as today (the upstream gates already cleared this).
        return {"tier": TIER_AUTO, "may_auto_persist": True,
                "reason": "heal policy disabled — auto-persist as today (still PROPOSED, human approve unchanged)"}

    auto_t, appr_t = _thresholds()
    try:
        conf = float(confidence)
    except (TypeError, ValueError):
        conf = 0.0

    # A proven-but-not-outcome-grounded green can never be AUTO: it re-ran green but
    # asserts no recorded business outcome, so a human must look. (Strictly stricter
    # than today, where it would persist as PROPOSED.)
    if not outcome_grounded:
        if conf >= appr_t:
            return {"tier": TIER_APPROVE, "may_auto_persist": False,
                    "reason": ("proven green but the step asserts no recorded OUTCOME "
                               "(outcome_not_grounded) — requires human approval before persist")}
        return {"tier": TIER_FAIL, "may_auto_persist": False,
                "reason": "low-confidence and not outcome-grounded — needs a human"}

    if conf >= auto_t:
        return {"tier": TIER_AUTO, "may_auto_persist": True,
                "reason": f"confidence {conf:.2f} >= {auto_t:.2f} and outcome grounded — auto-persist (PROPOSED)"}
    if conf >= appr_t:
        return {"tier": TIER_APPROVE, "may_auto_persist": False,
                "reason": f"marginal confidence {conf:.2f} in [{appr_t:.2f},{auto_t:.2f}) — requires human approval"}
    return {"tier": TIER_FAIL, "may_auto_persist": False,
            "reason": f"confidence {conf:.2f} < {appr_t:.2f} — too weak to persist; needs a human"}


__all__ = [
    "TIER_AUTO", "TIER_APPROVE", "TIER_FAIL",
    "policy_enabled", "evaluate_heal_tier",
]
