"""Regression classifier (Phase 5, pure core) — partitions each case honestly.

A DETERMINISTIC, min-gated classifier assigns exactly one disposition per case. Advisory
signals (a VLM/perceptual "match") are INPUTS only and can NEVER, alone, downgrade a
genuine regression to benign. Guard order enforces the never-green-wash doctrine:

  1. failed + not healed to a proven clean run  → GENUINE_REGRESSION
  2. failed + healed to a proven clean run       → SELF_HEALED (zero-touch)
  3. any ORACLE value change                     → GENUINE_REGRESSION  (the $250→$180 rule)
  4. semantic 'deviation'                        → GENUINE_REGRESSION
  5. a missing/unproven observation, or semantic 'uncertain'/'unavailable'
                                                 → HONEST_UNPROVEN (routes to a human)
  6. only NON-oracle display changes + 'match'   → BENIGN_DRIFT
  7. no changes                                  → PASS_UNCHANGED

No baseline yet ⇒ FIRST_BASELINE (establish; never a false regression on cycle 1).
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping

from .regression_diff import MISSING, VALUE_CHANGED, changed, oracle_value_changes

PASS_UNCHANGED = "PASS_UNCHANGED"
SELF_HEALED = "SELF_HEALED"
BENIGN_DRIFT = "BENIGN_DRIFT"
GENUINE_REGRESSION = "GENUINE_REGRESSION"
HONEST_UNPROVEN = "HONEST_UNPROVEN"
FIRST_BASELINE = "FIRST_BASELINE"

#: Dispositions that create a human review; the rest are zero-touch.
REVIEW_DISPOSITIONS = frozenset({GENUINE_REGRESSION, HONEST_UNPROVEN})

_SEM_DEVIATION = "deviation"
_SEM_MATCH = "match"
_SEM_UNPROVEN = frozenset({"uncertain", "unavailable", ""})


def _healed_clean(heal_result: Mapping | None) -> bool:
    h = heal_result if isinstance(heal_result, Mapping) else {}
    # A heal only counts if it reached a PROVEN clean run version (never a silent green).
    return bool(str(h.get("clean_run_version") or "").strip())


def classify(
    *,
    run_status: str,
    diffs: Iterable[Mapping],
    has_baseline: bool,
    heal_result: Mapping | None = None,
    semantic_signal: str = "",
) -> dict:
    """Classify one case into exactly one disposition with a named, evidence-linked
    reason. Pure and deterministic."""
    diffs = list(diffs)
    sem = str(semantic_signal or "").strip().lower()

    if not has_baseline:
        return _verdict(FIRST_BASELINE, "no prior baseline — establishing it from this run", diffs)

    # 1/2 — a failed run: healed (proven) or a genuine break.
    if str(run_status or "").strip().lower() in ("failed", "error", "broken"):
        if _healed_clean(heal_result):
            return _verdict(SELF_HEALED, "failed then healed to a proven clean run", diffs)
        return _verdict(GENUINE_REGRESSION, "the flow failed and could not be healed", diffs)

    # 3 — an oracle value changed (incl. any value-shaped change, fail-safe).
    oracle_changes = oracle_value_changes(diffs)
    if oracle_changes:
        fields = ", ".join(d["field"] for d in oracle_changes)
        return _verdict(GENUINE_REGRESSION, f"an oracle value changed ({fields})", diffs)

    # 4 — the semantic checker says the outcome deviates.
    if sem == _SEM_DEVIATION:
        return _verdict(GENUINE_REGRESSION, "semantic check reports the outcome deviated", diffs)

    ch = changed(diffs)

    # 5 — a missing observation, or a change we could not confirm → refuse, don't guess.
    if any(d.get("kind") == MISSING for d in ch):
        return _verdict(HONEST_UNPROVEN, "an expected outcome was not observed this run", diffs)
    if ch and sem in _SEM_UNPROVEN:
        return _verdict(HONEST_UNPROVEN, "a change occurred but could not be confirmed benign", diffs)

    # 6 — only non-oracle display changes AND the semantic checker confirms a match.
    if ch and sem == _SEM_MATCH:
        return _verdict(BENIGN_DRIFT, "only display wording changed; semantics still match", diffs)

    # A change with no confirming signal is NOT benign — fail to unproven.
    if ch:
        return _verdict(HONEST_UNPROVEN, "a display change with no confirming semantic signal", diffs)

    # 7 — nothing changed.
    return _verdict(PASS_UNCHANGED, "no observed change", diffs)


def _verdict(disposition: str, reason: str, diffs: list) -> dict:
    return {
        "disposition": disposition,
        "reason": reason,
        "needs_review": disposition in REVIEW_DISPOSITIONS,
        "changed_fields": [d["field"] for d in diffs if d.get("kind") != "unchanged"],
    }


def narrative(case_name: str, verdict: Mapping, diffs: Iterable[Mapping]) -> str:
    """A plain-language, TEMPLATE (never free LLM prose) summary for the review card."""
    disp = verdict.get("disposition")
    ch = [d for d in diffs if d.get("kind") == VALUE_CHANGED]
    if disp == GENUINE_REGRESSION and ch:
        d = ch[0]
        return f"{case_name}: {d['field']} changed from {d['old']!r} to {d['new']!r} — review."
    if disp == BENIGN_DRIFT and ch:
        d = ch[0]
        return (f"{case_name} still works; {d['field']} wording changed "
                f"{d['old']!r} → {d['new']!r} — review?")
    if disp == SELF_HEALED:
        return f"{case_name}: recovered automatically (a locator drifted and was re-anchored)."
    if disp == PASS_UNCHANGED:
        return f"{case_name}: unchanged."
    if disp == FIRST_BASELINE:
        return f"{case_name}: baseline established."
    return f"{case_name}: needs review ({verdict.get('reason')})."
