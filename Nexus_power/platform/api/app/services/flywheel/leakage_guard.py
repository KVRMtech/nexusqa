"""T5.2 — the LEAKAGE GUARD + the priors TIE-BREAK BOOST hook.

Two additive, default-safe pieces that complete the flywheel safety floor:

1. LEAKAGE GUARD (runnable, $0, deterministic). The promise to a regulated buyer's
   security team is "your raw text/values/selectors/names NEVER leave the building".
   The ledger schema already has no raw column, and featurize() returns only
   enums/buckets/shapes/hashes — but a promise is worth nothing without a test that
   PROVES it. ``assert_no_leakage`` takes the RAW inputs a caller is about to
   featurize plus the de-identified row it produced, and fails LOUD if any raw
   token, value, selector, or accessible name appears verbatim (or as an obvious
   substring) in the de-identified payload. Call it in tests AND optionally inline
   (gated) before record_label, so a future featurize() bug that echoes raw input
   is caught at the boundary, not in an audit.

2. PRIORS TIE-BREAK BOOST. ``priors.escalate_verdict`` is the ESCALATION hook (push
   a dismissable verdict toward needs_review). This is the distinct, complementary
   hook the doctrine calls out: a prior may only BOOST a grounded confidence as a
   TIE-BREAK, and may NEVER downgrade a real_regression or flip a refuse. With priors
   OFF/absent it is the identity (zero-prior invariant). It cannot change a verdict,
   a bind, or a refuse — it only nudges a confidence number UP within an already-
   chosen verdict, used purely to order otherwise-equal heal candidates.

DEFERRED (infra-gated, per the Phase 2 design): the federated TRAINING that produces
the priors (needs N consented tenants + GPU) is NOT runnable here — see
``aggregator.fit_verdict_escalation_priors`` for the mechanism and the gate. This
module is the consumption-side boost hook + the leakage test, present from day one.
"""

from __future__ import annotations

from .priors import priors_enabled, prior_for

# A refuse/authoritative verdict a prior may NEVER touch (mirror of priors._DISMISSABLE's
# complement intent): the boost hook is INERT on these — it can only ever nudge the
# confidence of an already-grounded, non-authoritative decision.
_NEVER_BOOST = frozenset({"real_regression", "needs_review"})

# A prior must clear this support bar before it may even nudge a tie-break (a thin
# prior is ignored; the cost of ignoring it is "ordered two equal candidates by the
# default order", never a wrong heal).
_MIN_BOOST_SUPPORT = 50
# A boost can never raise confidence by more than this, and never above 0.95 — a
# learned prior may TIP a tie, it may not manufacture certainty.
_MAX_BOOST_DELTA = 0.05
_BOOST_CEILING = 0.95


def boost_confidence(
    verdict: str, confidence: float, *,
    decision_point: str, feature_key: str,
) -> tuple[float, str | None]:
    """Tie-break BOOST under the rail. Returns ``(confidence, note)``.

    INERT (identity) when: priors are off/absent, the verdict is authoritative
    (real_regression / needs_review), no prior exists for the key, or the prior is
    too thin. When a sufficiently-supported positive prior exists, raise confidence by
    at most ``_MAX_BOOST_DELTA`` (capped at ``_BOOST_CEILING``) — never lowering it,
    never changing the verdict, never flipping a refuse. Used only to ORDER otherwise-
    equal heal candidates; it cannot create or suppress a heal."""
    if not priors_enabled():
        return confidence, None
    v = (verdict or "").strip().lower()
    if v in _NEVER_BOOST:
        return confidence, None  # rail: never touch an authoritative/refuse verdict
    prior = prior_for(decision_point, feature_key)
    if not prior:
        return confidence, None
    try:
        support = int(prior.get("support", 0) or 0)
        p_correct = float(prior.get("p_correct", 0.0) or 0.0)
    except (TypeError, ValueError):
        return confidence, None  # malformed prior → fail-closed (no boost)
    if support < _MIN_BOOST_SUPPORT or p_correct <= 0.0:
        return confidence, None
    try:
        base = float(confidence)
    except (TypeError, ValueError):
        return confidence, None
    delta = min(_MAX_BOOST_DELTA, _MAX_BOOST_DELTA * p_correct)
    boosted = min(_BOOST_CEILING, max(base, base + delta))
    if boosted <= base:
        return confidence, None
    note = (f"prior tie-break: this fix shape succeeded in {round(p_correct * 100)}% of "
            f"{support} federated cases — confidence nudged {base:.2f}->{boosted:.2f} "
            "(tie-break only; verdict unchanged)")
    return boosted, note


# ─── LEAKAGE GUARD ────────────────────────────────────────────────────────────

class LeakageError(AssertionError):
    """Raised when a de-identified payload contains raw input verbatim — a privacy
    contract violation. Loud by design: this must fail a test/CI, never be swallowed."""


def _norm(s) -> str:
    return str(s or "").strip().lower()


def find_leaks(raw_inputs, deidentified: dict, *, min_len: int = 4) -> list[dict]:
    """Return a list of detected leaks (empty == clean). A leak = a raw input string
    (length >= min_len) that appears verbatim, case-insensitively, as a value (or a
    substring of a value) anywhere in the de-identified payload.

    ``raw_inputs`` = the sensitive strings about to be featurized (label / value /
    selector / accessible-name / error text). ``deidentified`` = the dict of coarse
    features produced for the ledger. Short tokens (< min_len) are skipped to avoid
    false positives on enum fragments (e.g. 'id', 'a')."""
    raws = [_norm(r) for r in (raw_inputs or []) if _norm(r) and len(_norm(r)) >= min_len]
    if not raws:
        return []
    leaks: list[dict] = []
    for col, val in (deidentified or {}).items():
        sval = _norm(val)
        if not sval:
            continue
        for r in raws:
            if r in sval:
                leaks.append({"column": col, "raw": r, "value": str(val)})
    return leaks


def assert_no_leakage(raw_inputs, deidentified: dict, *, min_len: int = 4) -> None:
    """Fail LOUD (LeakageError) if any raw input leaked into the de-identified
    payload. The single call a test (or a gated inline check) makes to PROVE the
    de-id boundary holds for a given correction."""
    leaks = find_leaks(raw_inputs, deidentified, min_len=min_len)
    if leaks:
        cols = ", ".join(sorted({l["column"] for l in leaks}))
        raise LeakageError(
            f"de-identification leak: raw input appears verbatim in de-identified "
            f"column(s) [{cols}] — refusing to record. Details: {leaks}"
        )


__all__ = [
    "boost_confidence", "find_leaks", "assert_no_leakage", "LeakageError",
]
