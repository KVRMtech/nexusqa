"""Smoke test for the federated PRIORS consumption interface (flywheel.priors).

The two invariants that make the priors return-path safe to ship BEFORE the priors
themselves exist:

  * ZERO-PRIOR == IDENTITY: with NEXUS_FLYWHEEL_PRIORS unset (default), prior_for()
    is None and escalate_verdict() returns its input unchanged for EVERY verdict —
    byte-identical to a build with no priors.
  * THE RAIL: a loaded prior can only ESCALATE a dismissable verdict toward
    needs_review (never dismiss/downgrade real_regression or needs_review), only
    when it clears the support + probability bars, and never LOWERS confidence.

Also checks fail-closed loading: an UNSIGNED priors file is rejected.

Run:
    python Nexus_power/platform/api/tests/test_flywheel_priors_smoke.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

# ── Path setup ────────────────────────────────────────────────────────────
_API_ROOT = Path(__file__).resolve().parents[1]
_SDK_ROOT = _API_ROOT.parents[1] / "sdk" / "nexus-sdk"
sys.path.insert(0, str(_API_ROOT))
sys.path.insert(0, str(_SDK_ROOT))

from app.services.flywheel import priors  # noqa: E402

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


def _clear_env() -> None:
    for k in ("NEXUS_FLYWHEEL_PRIORS", "NEXUS_FLYWHEEL_PRIORS_PATH"):
        os.environ.pop(k, None)


# Every verdict the reducer can emit — the identity invariant must hold for ALL.
_ALL_VERDICTS = [
    "passed", "real_regression", "selector_drift", "visual_change",
    "flake", "needs_review",
]


def test_zero_prior_is_identity() -> None:
    print("zero-prior == identity (priors OFF):")
    _clear_env()
    check("priors_enabled() is False by default", priors.priors_enabled() is False)
    check("prior_for() is None when off",
          priors.prior_for("triage_feedback", "anykey") is None)
    for label in _ALL_VERDICTS:
        out_label, out_conf, note = priors.escalate_verdict(label, 0.42, prior=None)
        check(f"escalate_verdict identity for '{label}'",
              out_label == label and out_conf == 0.42 and note is None,
              detail=f"got ({out_label}, {out_conf}, {note})")


def test_rail_never_downgrades() -> None:
    print("the rail — a STRONG prior must NEVER touch authoritative verdicts:")
    strong = {"escalate": True, "p_real_regression": 0.95, "support": 1000}
    for label in ("real_regression", "needs_review", "passed"):
        out_label, out_conf, note = priors.escalate_verdict(label, 0.9, prior=strong)
        check(f"authoritative '{label}' untouched by a strong prior",
              out_label == label and note is None,
              detail=f"got ({out_label}, {out_conf}, {note})")


def test_rail_escalates_dismissable_only_when_supported() -> None:
    print("the rail — dismissable escalates ONLY when the prior clears both bars:")
    strong = {"escalate": True, "p_real_regression": 0.8, "support": 500}
    weak_support = {"escalate": True, "p_real_regression": 0.8, "support": 3}
    weak_prob = {"escalate": True, "p_real_regression": 0.51, "support": 500}
    no_flag = {"escalate": False, "p_real_regression": 0.99, "support": 999}

    out_label, out_conf, note = priors.escalate_verdict("flake", 0.3, prior=strong)
    check("strong prior escalates 'flake' -> needs_review",
          out_label == "needs_review" and note is not None,
          detail=f"got ({out_label}, {out_conf}, {note})")
    check("escalation never LOWERS confidence (max with p_real)",
          out_conf >= 0.3 and out_conf == 0.8,
          detail=f"conf={out_conf}")

    for name, p in (("thin support", weak_support), ("low prob", weak_prob),
                    ("no escalate flag", no_flag)):
        lbl, _c, n = priors.escalate_verdict("selector_drift", 0.3, prior=p)
        check(f"{name} prior leaves 'selector_drift' untouched",
              lbl == "selector_drift" and n is None,
              detail=f"got ({lbl}, {n})")


def test_unsigned_file_rejected() -> None:
    print("fail-closed loading — an UNSIGNED priors file is rejected:")
    _clear_env()
    with tempfile.TemporaryDirectory() as d:
        # Unsigned blob (no 'signature') with a would-be strong prior.
        unsigned = Path(d) / "unsigned.json"
        unsigned.write_text(json.dumps({
            "priors": {"triage_feedback": {"k": {"escalate": True,
                                                 "p_real_regression": 0.99,
                                                 "support": 999}}}
        }), encoding="utf-8")
        os.environ["NEXUS_FLYWHEEL_PRIORS"] = "1"
        os.environ["NEXUS_FLYWHEEL_PRIORS_PATH"] = str(unsigned)
        check("enabled but unsigned -> prior_for None (fail-closed)",
              priors.prior_for("triage_feedback", "k") is None)

        # A signed blob with the same prior IS loaded (proves the path works).
        signed = Path(d) / "signed.json"
        signed.write_text(json.dumps({
            "signature": "stub-sig",
            "priors": {"triage_feedback": {"k": {"escalate": True,
                                                 "p_real_regression": 0.99,
                                                 "support": 999}}}
        }), encoding="utf-8")
        os.environ["NEXUS_FLYWHEEL_PRIORS_PATH"] = str(signed)
        got = priors.prior_for("triage_feedback", "k")
        check("enabled + signed -> prior loaded",
              isinstance(got, dict) and got.get("support") == 999,
              detail=f"got {got}")
    _clear_env()


if __name__ == "__main__":
    test_zero_prior_is_identity()
    test_rail_never_downgrades()
    test_rail_escalates_dismissable_only_when_supported()
    test_unsigned_file_rejected()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
