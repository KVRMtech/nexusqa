"""F3 regression — auditor V_URL_TEXT: an always-RED URL-as-text oracle BLOCKS.

Run 7c89de7e (2026-07-24) shipped a compiled spec asserting
``expect(page.getByText(/https/i).first()).toBeVisible()`` — text no page ever
renders — with the auditor gate deployed in BLOCK mode (NEXUS_AUDITOR_GATE=block,
MIN_SCORE=9).  The gate let it through because no dimension recognised a
URL-fragment text oracle.  These tests pin the new D5 check + gate block shut:

  * bare scheme/www REGEX oracle (getByText(/https/…))  → spec DEFECT, blocks;
  * ACTION locators on URL-shaped labels                → never flagged
    (clicking a link whose visible label is 'www.example.com' is legitimate);
  * quoted FULL-URL text oracle                          → warning-only
    (a docs-style page genuinely can render a URL as text — reviewer decides).

Generic across apps: keys on URL *shape* in the compiled spec, never a host.
NO live stack / NO DB.  Run from Nexus_power/platform/api:
    python -m pytest tests/test_auditor_url_text_oracle.py -q
"""
from __future__ import annotations

import importlib.util
import os
import sys

_AUD_PATH = os.path.join(
    os.path.dirname(__file__), "..", "app", "services", "test_factory",
    "playwright_auditor.py")
_spec = importlib.util.spec_from_file_location("nexus_auditor_urltext_ut", _AUD_PATH)
aud = importlib.util.module_from_spec(_spec)
sys.modules["nexus_auditor_urltext_ut"] = aud
_spec.loader.exec_module(aud)


# The verbatim shape of the failed run's compiled assertion.
_FAILING_LINE = (
    "await expect(page.getByText(/https/i).first()).toBeVisible(); "
    "// grounded: step Expected Result, verified against the observed outcome"
)

_CLEAN_SPEC = """
import { test, expect } from '@playwright/test';
test('clean', async ({ page }) => {
  await page.goto('/portal/apply');
  await expect(page).toHaveURL(/\\/portal\\/apply/, { timeout: 30000 });
  await expect(page.getByText(/summary/i).first()).toBeVisible();
});
"""


def _verdicts(report):
    return {p.get("verdict") for p in report.get("per_step", [])}


def test_bare_scheme_regex_oracle_tanks_d5_and_blocks():
    spec = f"test('x', async ({{ page }}) => {{ {_FAILING_LINE} }});"
    report = aud.score_spec(spec, steps=[])
    assert aud.V_URL_TEXT in _verdicts(report)
    assert report["dimension_scores"]["assertion_correctness"] <= 1
    assert report["overall_score"] <= 1          # MIN-gated
    assert report["decision"] != aud.DECISION_CERTIFIED
    g = aud.gate(report, blocking=True)
    assert g["passed"] is False and g["would_block"] is True
    assert g["url_text_oracles"] == 1
    assert any("URL-as-text" in r for r in g["block_reasons"])


def test_www_and_http_regex_forms_also_block():
    for tok in ("http", "www"):
        spec = f"await expect(page.getByText(/{tok}/i).first()).toBeVisible();"
        report = aud.score_spec(spec, steps=[])
        assert aud.V_URL_TEXT in _verdicts(report), tok


def test_action_click_on_url_labelled_link_is_not_flagged():
    """A link whose VISIBLE label is a bare domain is legitimately clicked by
    text — only expect(...) oracles are in scope."""
    spec = "await page.getByText('www.example.com').click();"
    report = aud.score_spec(spec, steps=[])
    assert aud.V_URL_TEXT not in _verdicts(report)
    g = aud.gate(report, blocking=True)
    assert g["would_block"] is False and g["url_text_oracles"] == 0


def test_quoted_full_url_expect_warns_but_does_not_block():
    spec = "await expect(page.getByText('https://app.example/x')).toBeVisible();"
    report = aud.score_spec(spec, steps=[])
    assert aud.V_URL_TEXT not in _verdicts(report)       # not the always-RED class
    assert any("full URL string" in f for f in report["findings"])
    g = aud.gate(report, blocking=True)
    assert g["would_block"] is False                     # warning-first
    assert any("full URL string" in w for w in g["warnings"])


def test_clean_spec_unaffected():
    report = aud.score_spec(_CLEAN_SPEC, steps=[])
    assert aud.V_URL_TEXT not in _verdicts(report)
    assert report["dimension_scores"]["assertion_correctness"] == 10
    assert not any("URL" in f and "oracle" in f for f in report["findings"])
