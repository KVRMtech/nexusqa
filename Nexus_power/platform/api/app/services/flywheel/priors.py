"""Federated PRIORS consumption interface (Phase 2/3 scaffold) — the *return path*
of the flywheel, with the RAIL enforced in code.

What the flywheel learns = per-vertical PRIORS: de-identified, k-anonymized,
DP-noised aggregates fit ACROSS consented tenants (no raw row ever leaves a
building — see ``flywheel.export``). They flow back here as OPTIONAL, default-OFF
confidence signals into the deterministic core. The RAIL (non-negotiable, enforced
below) — a prior may only:

  * push a DISMISSABLE verdict toward ``needs_review`` (surface a maybe-missed bug),
  * RAISE a grounded confidence,
  * or OFFER a missed fix candidate (still proven by a headed re-run);

and may NEVER:

  * downgrade or dismiss a ``real_regression`` / ``needs_review``,
  * relax the validator, or change a bind.

ZERO-PRIOR == byte-identical to today (a regression invariant): with priors OFF or
absent, every function here is an identity / no-op. This is the single rail-guarded
DOOR through which learned signal may touch the frozen verdict/heal core — so that
learned signal can never become an ungoverned backdoor into it.

DEFERRED (needs the federated-training stack + N consented tenants, per the Phase 2
design): producing the priors themselves, real cryptographic signature
verification, and per-vertical ε/k. This module is the CONSUMPTION contract,
present from day one; the 4 documented integration points (classify_failure,
diagnose, _refine_kind, resolve_reanchor) each call through it under the same rail.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os

# The verdicts a prior may ESCALATE FROM (dismissable only). ``real_regression`` and
# ``needs_review`` are authoritative and are NEVER touched — a prior can neither
# dismiss nor downgrade them.
_DISMISSABLE = frozenset({"selector_drift", "visual_change", "flake"})
_ESCALATION_TARGET = "needs_review"

# A prior must clear BOTH bars before it is allowed to act at all — a thin or
# uncertain prior is ignored. (A prior can only ever escalate, never dismiss, so the
# failure mode of acting on a weak prior is "asked a human unnecessarily", never
# "hid a real bug".)
_MIN_SUPPORT = 50
_MIN_P_ESCALATE = 0.6


def priors_enabled() -> bool:
    """Master gate — DEFAULT OFF. With this off, every consumer here is an identity,
    so behaviour is byte-identical to a build with no priors at all."""
    return os.getenv("NEXUS_FLYWHEEL_PRIORS", "").strip().lower() in {"1", "true", "yes", "on"}


def _load_raw() -> dict:
    """Load the signed priors blob from ``NEXUS_FLYWHEEL_PRIORS_PATH``. Fail-CLOSED
    to zero-prior ({}) when disabled / absent / unreadable / unsigned. Real
    cryptographic signature verification is DEFERRED; for now a blob MUST carry a
    non-empty ``signature`` field or it is rejected, so an unsigned/corrupt file can
    never silently become a live prior. Returns the inner ``priors`` mapping."""
    if not priors_enabled():
        return {}
    path = os.getenv("NEXUS_FLYWHEEL_PRIORS_PATH", "").strip()
    if not path:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            blob = json.load(fh)
    except Exception:
        return {}
    if not isinstance(blob, dict) or not _signature_ok(blob):
        return {}  # unsigned / tampered / wrong-key → rejected (fail-closed)
    priors = blob.get("priors")
    return priors if isinstance(priors, dict) else {}


def _signature_ok(blob: dict) -> bool:
    """Verify the priors signature. When ``NEXUS_FLYWHEEL_PRIORS_KEY`` is set,
    recompute the HMAC-SHA256 over the canonical priors and constant-time compare —
    a tampered, unsigned, or wrong-key blob is REJECTED (fail-closed). With no key
    configured, fall back to requiring a non-empty ``signature`` field (full integrity
    deferred to the keyed path). Mirrors ``aggregator.sign_priors`` exactly."""
    if not blob.get("signature"):
        return False
    key = os.getenv("NEXUS_FLYWHEEL_PRIORS_KEY", "").strip()
    if not key:
        return True  # presence-only check (integrity deferred to the keyed path)
    body = json.dumps(blob.get("priors", {}), sort_keys=True, separators=(",", ":")).encode("utf-8")
    expected = hmac.new(key.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, str(blob.get("signature", "")))


def prior_for(decision_point: str, feature_key: str) -> dict | None:
    """The learned prior for ``(decision_point, feature_key)``, or None when priors
    are off/absent. A prior dict is advisory data, e.g.
    ``{"escalate": true, "p_real_regression": 0.71, "support": 312}``."""
    if not priors_enabled():
        return None
    dp = _load_raw().get(decision_point)
    if not isinstance(dp, dict):
        return None
    p = dp.get(feature_key)
    return p if isinstance(p, dict) else None


def escalate_verdict(
    label: str, confidence: float, *, prior: dict | None,
) -> tuple[str, float, str | None]:
    """Apply a prior to a verdict under the RAIL. Returns
    ``(label, confidence, note)``.

    INERT when ``prior`` is None (identity — the zero-prior invariant). A sufficiently
    supported prior that a DISMISSABLE pattern is often a real regression escalates it
    to ``needs_review`` (NEVER to a dismissed verdict, NEVER touching an authoritative
    ``real_regression`` / ``needs_review``). The move is always toward MORE human
    attention, never less — so a wrong prior costs a needless review, never a hidden
    regression."""
    if not prior:
        return label, confidence, None
    # Rail: only DISMISSABLE verdicts may be escalated; authoritative verdicts
    # (real_regression, needs_review, passed, …) are returned untouched.
    if label not in _DISMISSABLE:
        return label, confidence, None
    try:
        support = int(prior.get("support", 0) or 0)
        p_real = float(prior.get("p_real_regression", 0.0) or 0.0)
    except (TypeError, ValueError):
        return label, confidence, None  # malformed prior → fail-closed (no escalation)
    if bool(prior.get("escalate")) and support >= _MIN_SUPPORT and p_real >= _MIN_P_ESCALATE:
        note = (
            f"prior: this pattern was a real regression in {round(p_real * 100)}% of "
            f"{support} federated cases — escalated to needs_review"
        )
        # max(): never LOWER an existing confidence; only raise toward the prior.
        return _ESCALATION_TARGET, max(float(confidence), p_real), note
    return label, confidence, None


__all__ = ["priors_enabled", "prior_for", "escalate_verdict"]
