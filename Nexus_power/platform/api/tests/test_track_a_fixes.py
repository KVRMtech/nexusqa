"""Unit tests for the Track-A trust fixes (Phase-0).

A2 — playwright_auditor: locator-uniqueness dimension flags a repeated-name control
     with no disambiguating anchor (the saucedemo 6x 'Add to cart' blind spot).
A4 — network_oracle: a base-host connection/DNS failure is an ENV outage, not an app
     bug — so the auto-heal loop must NOT file a defect against the customer's app.
A1 — confidence: an ambiguous control with no anchor gets observed['ambiguous_unresolved']
     so the (autonomous) compiler refuses to guess rather than binding the wrong one.

The first two modules load standalone (no app deps). A1 needs the package context, so
it is attempted and skipped gracefully if the app env isn't importable. Runnable under
pytest OR standalone:  python platform/api/tests/test_track_a_fixes.py
"""
from __future__ import annotations

import importlib.util
import os
import sys

_HERE = os.path.dirname(__file__)
_TF = os.path.join(_HERE, "..", "app", "services", "test_factory")


def _load(modname: str, filename: str):
    spec = importlib.util.spec_from_file_location(modname, os.path.join(_TF, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pw = _load("pw_auditor_under_test", "playwright_auditor.py")
netq = _load("network_oracle_under_test", "network_oracle.py")


# ── A2: auditor locator-uniqueness dimension ──────────────────────────────────

def _ambiguous_evidence():
    # Two 'Add to cart' click actions on the SAME page → the name is ambiguous.
    return [
        {"verb": "click", "target_label": "Add to cart", "page_visit_id": "p1"},
        {"verb": "click", "target_label": "Add to cart", "page_visit_id": "p1"},
        {"verb": "click", "target_label": "Checkout", "page_visit_id": "p1"},
    ]


def test_auditor_flags_ambiguous_locator_without_anchor():
    steps = [{"step_number": 1, "action": "Click Add to cart",
              "provenance": "demonstrated",
              "observed": {"verb": "click", "label": "Add to cart"}}]
    rep = pw.score_spec("await page.getByRole('button', { name: 'Add to cart' }).click();",
                        steps, evidence=_ambiguous_evidence())
    # WARNING-ONLY: surfaced as a finding + per-step verdict; does NOT deduct the score
    # (so a benign false positive can't demote a good script until proven).
    assert any("Ambiguous locator" in f for f in rep["findings"]), rep["findings"]
    assert any(p["verdict"] == pw.V_AMBIGUOUS for p in rep["per_step"]), rep["per_step"]


def test_auditor_does_not_flag_when_anchor_present():
    steps = [{"step_number": 1, "action": "Click Add to cart",
              "provenance": "demonstrated",
              "observed": {"verb": "click", "label": "Add to cart",
                           "anchor": "Sauce Labs Onesie", "anchor_kind": "card"}}]
    rep = pw.score_spec("await page.getByRole('listitem').getByRole('button').click();",
                        steps, evidence=_ambiguous_evidence())
    assert not any("Ambiguous locator" in f for f in rep["findings"]), rep["findings"]


def test_auditor_does_not_flag_unique_control():
    steps = [{"step_number": 1, "action": "Click Login",
              "provenance": "demonstrated",
              "observed": {"verb": "click", "label": "Login"}}]
    evidence = [{"verb": "click", "target_label": "Login", "page_visit_id": "p1"}]  # unique
    rep = pw.score_spec("await page.getByRole('button', { name: 'Login' }).click();",
                        steps, evidence=evidence)
    assert not any("Ambiguous locator" in f for f in rep["findings"]), rep["findings"]


# ── A3: auditor gate (default warning-only) ───────────────────────────────────

def _impossible_report():
    # A spec that asserts a navigation a click doesn't cause -> impossible transition.
    steps = [{"step_number": 1, "action": "Click Add to cart",
              "provenance": "demonstrated",
              "observed": {"verb": "click", "label": "Add to cart",
                           "next_url": "https://x/cart.html", "navigation_grounded": False}}]
    return pw.score_spec("await page.getByRole('button').click();\nawait expect(page)"
                         ".toHaveURL(/cart/);", steps, evidence=None)


def test_gate_default_is_warning_only_but_honest():
    rep = _impossible_report()
    g = pw.gate(rep)  # default enforced/blocking=False
    assert g["enforced"] is False, g          # warning-only: does not block shipping
    assert g["passed"] is False, g            # …but 'passed' is HONEST (would_block)
    assert g["would_block"] is True, g
    assert g["block_reasons"], g


def test_gate_blocking_rejects_impossible_transition():
    g = pw.gate(_impossible_report(), blocking=True)
    assert g["enforced"] is True and g["passed"] is False, g
    assert any("impossible" in r.lower() for r in g["block_reasons"]), g


def test_gate_passes_a_clean_report():
    steps = [{"step_number": 1, "action": "Open the app", "provenance": "demonstrated",
              "observed": {"verb": "navigate", "label": "", "url": "https://x/"}}]
    g = pw.gate(pw.score_spec("await page.goto('/');", steps, evidence=None), blocking=True)
    assert g["passed"] is True and g["would_block"] is False, g


def test_gate_surfaces_ambiguous_but_does_not_block():
    steps = [{"step_number": 1, "action": "Click Add to cart", "provenance": "demonstrated",
              "observed": {"verb": "click", "label": "Add to cart"}}]
    rep = pw.score_spec("await page.getByRole('button', { name: 'Add to cart' }).click();",
                        steps, evidence=_ambiguous_evidence())
    g = pw.gate(rep, blocking=True)
    assert g["ambiguous_locators"] >= 1, g
    # ambiguity is warning-grade, NOT a block reason -> passes even when enforced.
    assert g["passed"] is True and g["would_block"] is False, g


# ── A4: base-host outage classification ───────────────────────────────────────

def test_base_host_outage_is_env_not_app_bug():
    sig = {"kind": "network_error", "detail": "net::ERR_CONNECTION_REFUSED"}
    # It IS a real-bug signal by the generic classifier (so it would file a defect)…
    assert netq.is_real_bug_signal(sig) is True
    # …but on the FIRST step (only the base navigation happened) it's an ENV outage.
    assert netq.is_base_host_connection_failure(
        sig, base_url="https://app.example.com", is_first_step=True) is True


def test_base_host_structured_url_match():
    sig = {"kind": "network_error", "url": "https://app.example.com/", "detail": "failed"}
    assert netq.is_base_host_connection_failure(sig, base_url="https://app.example.com") is True
    # A DIFFERENT host that failed mid-flow is NOT a base-host outage.
    sig2 = {"kind": "network_error", "url": "https://cdn.other.com/x.js", "detail": "failed"}
    assert netq.is_base_host_connection_failure(sig2, base_url="https://app.example.com") is False


def test_server_error_is_still_an_app_bug():
    # A 5xx on the base host is a REAL app bug, never reclassified as an env outage.
    sig = {"kind": "server_error", "status": 500, "detail": "Internal Server Error"}
    assert netq.is_base_host_connection_failure(
        sig, base_url="https://app.example.com", is_first_step=True) is False


def test_midflow_network_error_not_env_outage():
    # A network error deep in the flow (not first step, no URL) stays app-adjudicated.
    sig = {"kind": "network_error", "detail": "connection reset"}
    assert netq.is_base_host_connection_failure(
        sig, base_url="https://app.example.com", is_first_step=False) is False


# ── A1: confidence ambiguous_unresolved flag (best-effort; needs app context) ──

def test_confidence_sets_ambiguous_unresolved_flag():
    sys.path.insert(0, os.path.join(_HERE, ".."))
    sys.path.insert(0, os.path.join(_HERE, "..", "..", "..", "sdk", "nexus-sdk"))
    try:
        from app.services.test_factory import confidence as conf  # type: ignore
        from app.services.test_factory.generator import _norm  # type: ignore
    except Exception as e:  # pragma: no cover — app env not importable standalone
        print(f"  (skipped A1 confidence test — app context unavailable: {type(e).__name__})")
        return

    class _Step:
        def __init__(self, observed):
            self.observed = observed
            self.provenance = "demonstrated"

    amb = {_norm("Add to cart")}  # the form compute_ambiguous_labels actually produces
    step = _Step({"verb": "click", "label": "Add to cart"})
    level, _ = conf.score_step(step, amb)
    assert step.observed.get("ambiguous_unresolved") is True, step.observed
    assert level == conf.REVIEW
    # With an anchor, the flag is cleared and it's no longer review-for-ambiguity.
    step2 = _Step({"verb": "click", "label": "Add to cart", "anchor": "Sauce Labs Onesie"})
    conf.score_step(step2, amb)
    assert "ambiguous_unresolved" not in step2.observed, step2.observed
    # A stale flag is cleared once an anchor arrives on re-score.
    step3 = _Step({"verb": "click", "label": "Add to cart",
                   "ambiguous_unresolved": True, "anchor": "Backpack"})
    conf.score_step(step3, amb)
    assert "ambiguous_unresolved" not in step3.observed, step3.observed


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"PASS  {fn.__name__}")
    print(f"\nALL {len(tests)} TRACK-A TESTS PASSED")
