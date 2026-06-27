"""Tests for the Agentic Playwright Auditor's deterministic rubric + LLM grounding.

The deterministic scorer is the source of truth for the /10 — it must rate the
auditor's OLD 2/10 script low (impossible transition ⇒ navigation_correctness=0)
and the Track-1 HONEST script high (no impossible assertion, fills replayed, gap
honestly UNPROVEN ⇒ certified). And the LLM-finding validator must demote any
ungrounded "fix to green" to an honest UNPROVEN (never fabricate a passing step).

NO SDK / NO LLM: score_spec + validate_audit have no app imports (the LLM router
is imported lazily inside audit()), so we load the module by file path. Run from
Nexus_power/platform/api:
    python -m pytest tests/test_playwright_auditor.py -q
"""
from __future__ import annotations

import importlib.util
import os
import sys

_PATH = os.path.join(
    os.path.dirname(__file__), "..", "app", "services", "test_factory", "playwright_auditor.py"
)
_spec = importlib.util.spec_from_file_location("nexus_pw_auditor", _PATH)
aud = importlib.util.module_from_spec(_spec)
sys.modules["nexus_pw_auditor"] = aud
_spec.loader.exec_module(aud)

# Shared recording evidence (the saucedemo flow's grounded values).
EVIDENCE = [
    {"verb": "click", "target_label": "Add to cart (Sauce Labs Backpack)", "value": None},
    {"verb": "type", "target_label": "First Name", "value": "Venkata"},
    {"verb": "type", "target_label": "Last Name", "value": "karnam"},
]

# ── The OLD, broken script (pre-Track-1): impossible transition + dropped fills + dead code ──
OLD_STEPS = [
    {"step_number": 1, "action": "Open https://saucedemo.com/inventory", "provenance": "demonstrated",
     "observed": {"verb": "navigate", "url": "https://saucedemo.com/inventory"}},
    {"step_number": 2, "action": "Click 'Add to cart (Sauce Labs Backpack)'", "provenance": "demonstrated",
     "observed": {"verb": "click", "label": "Add to cart (Sauce Labs Backpack)",
                  "next_url": "/checkout-step-one"}},  # next_url WITHOUT navigation_grounded → ungrounded
    {"step_number": 3, "action": "Verify the application navigated to /checkout-step-one",
     "provenance": "demonstrated", "observed": {"verb": "navigate", "url": "/checkout-step-one"}},
]
OLD_SPEC = """import { test, expect } from '@playwright/test';
const D = (() => { try { const __a = require('../../nexus.data.json'); return __a; } catch { return {}; } })();
function __nxTok(v){ return /x/; }
test('e2e', async ({ page }) => {
  await page.goto('/inventory');
  await page.getByRole('button', { name: 'Add to cart (Sauce Labs Backpack)' }).click();
  await expect(page).toHaveURL(/\\/checkout-step-one/);
});"""

# ── The NEW, honest script (post-Track-1): no impossible assertion, fills replayed, gap UNPROVEN ──
NEW_STEPS = [
    {"step_number": 1, "action": "Open https://saucedemo.com/inventory", "provenance": "demonstrated",
     "observed": {"verb": "navigate", "url": "https://saucedemo.com/inventory"}},
    {"step_number": 2, "action": "Click 'Add to cart (Sauce Labs Backpack)'", "provenance": "demonstrated",
     "observed": {"verb": "click", "label": "Add to cart (Sauce Labs Backpack)",
                  "after": "button now reads Remove; cart shows 1 item"}},  # NO next_url
    {"step_number": 3, "action": "Verify the application navigated to /checkout-step-one",
     "provenance": "inferred", "observed": {"verb": "navigate", "url": "/checkout-step-one"}},  # UNPROVEN
    {"step_number": 4, "action": "Enter 'Venkata' in the 'First Name' field", "provenance": "demonstrated",
     "observed": {"verb": "type", "label": "First Name", "value": "Venkata"}},
    {"step_number": 5, "action": "Enter 'karnam' in the 'Last Name' field", "provenance": "demonstrated",
     "observed": {"verb": "type", "label": "Last Name", "value": "karnam"}},
]
NEW_SPEC = """import { test, expect } from '@playwright/test';
const D = (() => { try { const __a = require('../../nexus.data.json'); return __a; } catch { return {}; } })();
function __nxTok(v){ const m=String(v).match(/[A-Za-z]{2,}/); return m?new RegExp(m[0],'i'):/\\S/; }
test('e2e', async ({ page }) => {
  await page.goto('/inventory');
  await page.getByRole('button', { name: 'Add to cart (Sauce Labs Backpack)' }).click();
  await expect(page.getByText(/remove/i).first()).toBeVisible();
  // UNPROVEN transition: the action that advanced the page was not captured.
  await page.getByLabel('First Name').fill(D['first-name'] ?? 'Venkata');
  await expect(page.getByLabel('First Name')).toHaveValue(__nxTok('Venkata'));
  await page.getByLabel('Last Name').fill(D['last-name'] ?? 'karnam');
});"""


def test_old_broken_script_scores_low():
    r = aud.score_spec(OLD_SPEC, OLD_STEPS, evidence=EVIDENCE)
    assert r["dimension_scores"]["navigation_correctness"] == 0, r["dimension_scores"]
    assert r["dimension_scores"]["assertion_correctness"] <= 1
    assert r["overall_score"] <= 2, r
    assert r["decision"] == aud.DECISION_REPAIR
    assert any(p["verdict"] == aud.V_IMPOSSIBLE for p in r["per_step"])


def test_new_honest_script_certifies():
    r = aud.score_spec(NEW_SPEC, NEW_STEPS, evidence=EVIDENCE)
    d = r["dimension_scores"]
    assert d["navigation_correctness"] == 10, d
    assert d["grounded_replay"] >= 9, d        # both typed values replayed as fills
    assert d["playwright_apis"] >= 9, d        # D + __nxTok both used, no sleeps
    assert d["assertion_correctness"] >= 9, d
    assert r["overall_score"] >= 9, r
    assert r["decision"] == aud.DECISION_CERTIFIED
    assert r["gaps"] >= 1                        # the uncaptured cart→checkout gap, honestly UNPROVEN
    assert not any(p["verdict"] == aud.V_IMPOSSIBLE for p in r["per_step"])


def test_llm_finding_validation_demotes_ungrounded_fix():
    evidence_text = "First Name = Venkata; Last Name = karnam"
    raw = [
        {"step_number": 2, "verdict": "impossible_transition", "evidence_quote": "", "recommended": "drop_assertion"},
        {"step_number": 4, "verdict": "data_not_replayed", "evidence_quote": "Venkata", "recommended": "add_fill_step"},
        {"step_number": 9, "verdict": "missing_prerequisite", "evidence_quote": "a wholly invented value",
         "recommended": "add_fill_step"},
    ]
    out = {f["step_number"]: f for f in aud.validate_audit(raw, evidence_text=evidence_text)}
    assert out[2]["recommended"] == "drop_assertion"                 # safe action, untouched
    assert out[4]["recommended"] == "add_fill_step" and out[4]["grounded"]  # verbatim-grounded → kept
    assert out[9]["recommended"] == "mark_unproven" and not out[9]["grounded"]  # ungrounded → demoted


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {fn.__name__}: {e}")
    print("ALL PASS" if not failed else f"{failed} FAILED")
    sys.exit(1 if failed else 0)
