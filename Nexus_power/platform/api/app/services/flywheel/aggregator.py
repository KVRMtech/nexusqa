"""Federated AGGREGATOR (Phase 3 scaffold) — fits per-vertical PRIORS from the
de-identified, k-anonymized tenant EXPORTS, and signs them for the return path.

This is the MIDDLE of the flywheel, the piece that was missing:

    capture (ledger) → de-id export (export.py) → AGGREGATE + FIT (here)
        → signed priors → consume under the rail (priors.py)

It runs ONLY on the counts-only aggregates that already crossed the boundary — no
raw row, value, name, or selector can reach here (export.py guarantees
``raw_rows_exported == 0``), so federation is "average the lessons, never the
files". Counts SUM across tenants; that sum IS the federated aggregate.

What it fits today = the VERDICT-ESCALATION prior: *for failures whose
de-identified error-pattern is X, how often did a DISMISSED verdict turn out to be
a real regression* — i.e. a human override to real_regression (``triage_feedback``)
or a later contradiction (``heal_outcome``). Fed back, that prior lets the consumer
ESCALATE a future dismissable-X verdict toward ``needs_review`` — NEVER the reverse
(the rail lives in ``priors.py``; a prior can only ask for MORE human attention).

DEFERRED to the privacy/ML build (per the Phase 2/3 design): real differential
privacy (``export.add_dp_noise`` is still a flagged stub), full secure aggregation,
PKI signing (here we HMAC-sign — real symmetric integrity, single-key), and
per-vertical ε/k calibration. And the one thing NO code supplies: REAL consented
multi-tenant data — until N tenants opt in, this fits over whatever exists (often a
single tenant or test fixtures), and export.py's k-anon floor correctly drops groups
too thin to be safe. So the MECHANISM is complete and provably closes; the MOAT
still waits on signed federated contracts.
"""

from __future__ import annotations

import hashlib
import hmac
import json

# The verdicts a prior may escalate FROM — must match priors._DISMISSABLE.
_DISMISSABLE = frozenset({"selector_drift", "visual_change", "flake"})
# The human-correction enum the capture path writes for "engine was wrong, it's real".
_OVERRIDE_TO_REGRESSION = "overridden_to:real_regression"


def fit_verdict_escalation_priors(
    tenant_exports, *, min_support: int = 50, p_threshold: float = 0.6,
) -> dict:
    """Federated-average the tenant export aggregates into verdict-escalation priors
    keyed by ``error_pattern_class``. Each tenant's counts SUM in (that is the
    federation — only counts ever crossed a boundary). Returns
    ``{"verdict_escalation": {pattern: {escalate, p_real_regression, support,
    tenants}}}``. A pattern below ``min_support`` is omitted (too thin to act on).

    Numerator = times that pattern proved a real regression (human override to
    real_regression + later contradiction). Denominator = times we had ANY outcome
    signal for that pattern while the engine had DISMISSED it. So the prior answers:
    'when the engine dismisses pattern X, how often is it actually a real bug?'"""
    num: dict[str, float] = {}          # times pattern X proved a real regression
    den: dict[str, float] = {}          # times we had an outcome for a dismissed X
    tenants_seen: dict[str, set] = {}
    for ti, exp in enumerate(tenant_exports or []):
        for g in (exp.get("groups") or []):
            key = (g.get("error_pattern_class") or "").strip()
            if not key or key == "none":
                continue
            count = float(g.get("count", 0) or 0)
            overridden = count if g.get("human_decision_enum") == _OVERRIDE_TO_REGRESSION else 0.0
            contradicted = float(g.get("later_contradicted_count", 0) or 0)
            really_regression = overridden + contradicted
            engine_dismissed = g.get("engine_verdict_enum") in _DISMISSABLE
            # Only learn from rows where the engine dismissed (the population the
            # prior would act on) OR where we positively saw it was really a regression.
            if not (engine_dismissed or really_regression):
                continue
            den[key] = den.get(key, 0.0) + count
            num[key] = num.get(key, 0.0) + really_regression
            tenants_seen.setdefault(key, set()).add(ti)

    priors: dict[str, dict] = {}
    for key, d in den.items():
        if d < min_support:
            continue
        p = (num[key] / d) if d else 0.0
        priors[key] = {
            "escalate": bool(p >= p_threshold),
            "p_real_regression": round(p, 4),
            "support": int(d),
            "tenants": len(tenants_seen.get(key, ())),
        }
    return {"verdict_escalation": priors}


def _canonical(priors: dict) -> bytes:
    return json.dumps(priors, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_priors(priors: dict, *, key: bytes) -> dict:
    """Wrap priors in a SIGNED envelope: real HMAC-SHA256 integrity over the canonical
    JSON (symmetric / single-key — PKI is deferred, but this is a genuine signature,
    not a stub). The consumer (``priors.verify``) checks it when
    ``NEXUS_FLYWHEEL_PRIORS_KEY`` is set, so a tampered or unsigned blob is rejected."""
    sig = hmac.new(key, _canonical(priors), hashlib.sha256).hexdigest()
    return {"signature": sig, "alg": "HMAC-SHA256", "priors": priors}


def signed_priors_json(priors: dict, *, key: bytes) -> str:
    """The signed-priors envelope as a JSON string, ready to write to the path the
    consumer loads from (``NEXUS_FLYWHEEL_PRIORS_PATH``)."""
    return json.dumps(sign_priors(priors, key=key), sort_keys=True)


__all__ = [
    "fit_verdict_escalation_priors",
    "sign_priors",
    "signed_priors_json",
]
