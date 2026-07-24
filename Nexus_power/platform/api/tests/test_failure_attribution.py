"""F4 regression — failure attribution: never blame the app for a product defect.

Run 7c89de7e (2026-07-24) failed on ``expect(page.getByText(/https/i))`` — a
generated-oracle defect — yet the run report said "Functional · Needs attention
· Last run failed", implicitly pointing at the client's application (which had
provably behaved: the click worked and the hard toHaveURL oracle passed).

These tests pin the attribution contract:
  * the URL-as-text oracle failure  → CONFIRMED ``script_defect`` (product);
  * quoted-full-URL text oracle     → ``script_defect_candidate`` (probable);
  * every unprovable failure        → ``None`` — NO claim (honest silence;
    a real application regression must keep its unqualified RED).

Deterministic, $0, evidence-only — safe on every ingested failed step.
Run from Nexus_power/platform/api:
    python -m pytest tests/test_failure_attribution.py -q
"""
from __future__ import annotations

import importlib.util
import os
import sys

_MOD_PATH = os.path.join(
    os.path.dirname(__file__), "..", "app", "services", "test_factory",
    "failure_attribution.py")
_spec = importlib.util.spec_from_file_location("nexus_failattr_ut", _MOD_PATH)
fa = importlib.util.module_from_spec(_spec)
sys.modules["nexus_failattr_ut"] = fa
_spec.loader.exec_module(fa)


# The verbatim error of run 7c89de7e step 7 (ANSI codes stripped at ingest).
_REAL_ERROR = (
    "Error: Timed out 15000ms waiting for expect(locator).toBeVisible()\n\n"
    "Locator: getByText(/https/i).first()\n"
    "Expected: visible\n"
    "Received: <element(s) not found>\n"
    "Call log:\n"
    "  - expect.toBeVisible with timeout 15000ms\n"
    "  - waiting for getByText(/https/i).first()\n"
)


def test_run_7c89de7e_error_is_confirmed_product_defect():
    a = fa.classify_step_failure(_REAL_ERROR)
    assert a is not None
    assert a["attribution"] == fa.ATTR_SCRIPT_DEFECT
    assert a["cause"] == "url_as_text_oracle"
    assert a["blame"] == "product"
    assert "NOT an application failure" in a["detail"]


def test_http_and_www_token_forms_also_attributed():
    for tok in ("http", "www"):
        err = _REAL_ERROR.replace("/https/i", f"/{tok}/i")
        a = fa.classify_step_failure(err)
        assert a and a["attribution"] == fa.ATTR_SCRIPT_DEFECT, tok


def test_quoted_full_url_oracle_is_candidate_tier():
    err = (
        "Error: Timed out 15000ms waiting for expect(locator).toBeVisible()\n"
        "Locator: getByText('https://app.example/portal/apply')\n"
        "Received: <element(s) not found>"
    )
    a = fa.classify_step_failure(err)
    assert a is not None
    assert a["attribution"] == fa.ATTR_SCRIPT_DEFECT_CANDIDATE
    assert a["blame"] == "product_probable"


def test_real_application_regression_gets_no_claim():
    """A grounded text oracle failing (e.g. the app really lost 'checkout')
    must keep its unqualified RED — attribution stays silent."""
    err = _REAL_ERROR.replace("/https/i", "/checkout/i")
    assert fa.classify_step_failure(err) is None


def test_action_locator_failures_are_out_of_scope():
    """Locator/action timeouts route to the heal pipeline, not attribution."""
    err = (
        "TimeoutError: locator.click: Timeout 30000ms exceeded.\n"
        "waiting for getByRole('button', { name: 'Continue' })"
    )
    assert fa.classify_step_failure(err) is None


def test_empty_error_gets_no_claim():
    assert fa.classify_step_failure("") is None
    assert fa.classify_step_failure(None) is None


def test_href_attribute_locator_is_not_mistaken_for_text_oracle():
    """A URL inside an attribute selector is not a URL-as-TEXT oracle."""
    err = (
        "Error: Timed out waiting for expect(locator).toBeVisible()\n"
        "Locator: locator('a[href=\"https://app.example/next\"]')"
    )
    assert fa.classify_step_failure(err) is None


def test_summary_rollup_counts_and_product_flag():
    a = fa.classify_step_failure(_REAL_ERROR)
    s = fa.summarize_attributions([None, a, None])
    assert s == {"counts": {fa.ATTR_SCRIPT_DEFECT: 1}, "product_side": True}
    assert fa.summarize_attributions([None, None]) is None
