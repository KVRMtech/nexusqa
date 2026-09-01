"""End-to-end smoke test for the FULL flywheel loop (the moat mechanism):

    de-id tenant EXPORTS  →  aggregator.fit  →  sign  →  priors.load (HMAC-verified)
        →  escalate_verdict (under the rail)

This proves the MECHANISM closes — capture's de-identified aggregates become a signed
prior that, fed back, escalates a future dismissed verdict toward needs_review. The
multi-tenant exports here are SYNTHETIC TEST FIXTURES (labelled below), NOT product
data — the real moat still needs N consented tenants, which no code supplies. The
fixtures only exercise the plumbing.

Run:
    python Nexus_power/platform/api/tests/test_flywheel_loop_smoke.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

_API_ROOT = Path(__file__).resolve().parents[1]
_SDK_ROOT = _API_ROOT.parents[1] / "sdk" / "nexus-sdk"
sys.path.insert(0, str(_API_ROOT))
sys.path.insert(0, str(_SDK_ROOT))

from app.services.flywheel import aggregator, priors  # noqa: E402

PASS = 0
FAIL = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS  {label}")
    else:
        FAIL += 1
        print(f"  FAIL  {label}  {detail}")


# ── SYNTHETIC tenant exports (TEST FIXTURES — not product data) ───────────────
# Each mimics export.build_tenant_export() output: counts-only de-id groups. Two
# tenants both saw 'locator_missing' failures the engine DISMISSED as selector_drift
# but a human often OVERRODE to real_regression — exactly the lesson the prior encodes.
_TENANT_A = {
    "groups": [
        {"error_pattern_class": "locator_missing", "engine_verdict_enum": "selector_drift",
         "human_decision_enum": "overridden_to:real_regression", "count": 40,
         "later_contradicted_count": 0},
        {"error_pattern_class": "locator_missing", "engine_verdict_enum": "selector_drift",
         "human_decision_enum": "agreed", "count": 20, "later_contradicted_count": 0},
        {"error_pattern_class": "timeout", "engine_verdict_enum": "flake",
         "human_decision_enum": "agreed", "count": 50, "later_contradicted_count": 0},
    ],
}
_TENANT_B = {
    "groups": [
        {"error_pattern_class": "locator_missing", "engine_verdict_enum": "selector_drift",
         "human_decision_enum": "overridden_to:real_regression", "count": 30,
         "later_contradicted_count": 0},
        {"error_pattern_class": "locator_missing", "engine_verdict_enum": "selector_drift",
         "human_decision_enum": "agreed", "count": 10, "later_contradicted_count": 0},
        # a thin pattern, below min_support → must be dropped
        {"error_pattern_class": "outcome_contradicted", "engine_verdict_enum": "flake",
         "human_decision_enum": "overridden_to:real_regression", "count": 3,
         "later_contradicted_count": 0},
    ],
}


def test_fit_aggregates_across_tenants() -> None:
    print("aggregator.fit — federated-average across tenants:")
    fitted = aggregator.fit_verdict_escalation_priors([_TENANT_A, _TENANT_B], min_support=50)
    ve = fitted.get("verdict_escalation", {})
    lm = ve.get("locator_missing")
    # locator_missing: num = 40 + 30 = 70 ; den = 40+20 + 30+10 = 100 ; p = 0.70
    check("locator_missing prior exists", lm is not None, detail=str(ve))
    if lm:
        check("support summed across tenants (100)", lm["support"] == 100, detail=str(lm))
        check("p_real_regression == 0.70", lm["p_real_regression"] == 0.7, detail=str(lm))
        check("escalate True (>= 0.6)", lm["escalate"] is True, detail=str(lm))
        check("tenants counted (2)", lm["tenants"] == 2, detail=str(lm))
    # timeout: num 0 / den 50 → p 0 → escalate False (present but not escalating)
    tо = ve.get("timeout")
    check("timeout prior not escalating", (tо is None) or (tо["escalate"] is False), detail=str(tо))
    # outcome_contradicted: support 3 < 50 → dropped
    check("thin pattern dropped (support < min)", "outcome_contradicted" not in ve, detail=str(ve))


def test_loop_closes_sign_load_escalate() -> None:
    print("full loop -- sign -> load (HMAC-verified) -> escalate:")
    key = b"test-fixture-key-not-a-secret"
    fitted = aggregator.fit_verdict_escalation_priors([_TENANT_A, _TENANT_B], min_support=50)
    envelope = aggregator.signed_priors_json(fitted, key=key)

    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "priors.json"
        path.write_text(envelope, encoding="utf-8")
        os.environ["NEXUS_FLYWHEEL_PRIORS"] = "1"
        os.environ["NEXUS_FLYWHEEL_PRIORS_PATH"] = str(path)
        os.environ["NEXUS_FLYWHEEL_PRIORS_KEY"] = key.decode()

        prior = priors.prior_for("verdict_escalation", "locator_missing")
        check("signed priors load + HMAC-verify", isinstance(prior, dict) and prior.get("escalate") is True,
              detail=str(prior))

        # The payoff: a future 'flake'/'selector_drift' on locator_missing escalates.
        label, conf, note = priors.escalate_verdict("selector_drift", 0.3, prior=prior)
        check("dismissed verdict ESCALATES to needs_review", label == "needs_review" and note is not None,
              detail=f"({label}, {conf}, {note})")
        check("a real_regression is NEVER touched by the same prior",
              priors.escalate_verdict("real_regression", 0.9, prior=prior)[0] == "real_regression")

        # Tamper: flip a byte in the signed priors → HMAC verify must reject it.
        blob = json.loads(envelope)
        blob["priors"]["verdict_escalation"]["locator_missing"]["p_real_regression"] = 0.99
        path.write_text(json.dumps(blob), encoding="utf-8")
        check("TAMPERED priors rejected (HMAC mismatch -> None)",
              priors.prior_for("verdict_escalation", "locator_missing") is None)

    for k in ("NEXUS_FLYWHEEL_PRIORS", "NEXUS_FLYWHEEL_PRIORS_PATH", "NEXUS_FLYWHEEL_PRIORS_KEY"):
        os.environ.pop(k, None)


if __name__ == "__main__":
    test_fit_aggregates_across_tenants()
    test_loop_closes_sign_load_escalate()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
