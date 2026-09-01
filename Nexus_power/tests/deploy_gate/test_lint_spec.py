"""M0.4 / T-GT-07 — the API-policy lint four reports had always advertised.

Before this milestone ``lint_spec`` did not exist. Every call site wrapped it in
``except: lint = []``, so every certificate carried ``"lint": []`` /
``"lint_errors": 0`` and the rubric line claimed "+ API-policy lint". An empty
finding list is indistinguishable from a clean asset, so the claim could never be
falsified by reading the report — which is the definition of a phantom
validation.
"""
from __future__ import annotations

import os
import sys

import pytest

_API = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "platform", "api")
if _API not in sys.path:
    sys.path.insert(0, _API)

from app.services.test_factory import playwright_auditor as P  # noqa: E402


def errors(spec: str) -> list[dict]:
    return [f for f in P.lint_spec(spec) if f["severity"] == "error"]


def rules(spec: str) -> set[str]:
    return {f["rule"] for f in P.lint_spec(spec)}


# ══════════════════════════════════════════════════════════════════════════
#  The contract the redteam benchmark had already written down
# ══════════════════════════════════════════════════════════════════════════
def test_the_redteam_forbidden_api_case_produces_at_least_three_errors():
    """``benchmarks/redteam/run_redteam.py`` asserts ``expect_lint_errors_min: 3``
    on this exact spec. It passed for months with lint_err=0, because the
    except-swallow zeroed the count it was checking."""
    spec = ("await page.goto('/x');\n"
            "await page.waitForTimeout(3000);\n"
            "await page.click('#btn');\n"
            "const h: ElementHandle = await page.$('#y');")
    assert len(errors(spec)) >= 3
    assert rules(spec) >= {"no-arbitrary-sleep", "no-page-selector-action",
                           "no-element-handle"}


# ══════════════════════════════════════════════════════════════════════════
#  Error rules — wrong under any circumstance
# ══════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("line,rule", [
    ("await page.waitForTimeout(500);", "no-arbitrary-sleep"),
    ("await page.click('#a');", "no-page-selector-action"),
    ("await page.fill('#a', 'x');", "no-page-selector-action"),
    ("await page.selectOption('#a', 'x');", "no-page-selector-action"),
    ("const h = await page.$('#a');", "no-element-handle"),
    ("const hs = await page.$$('#a');", "no-element-handle"),
    ("await page.waitForSelector('#a');", "no-element-handle"),
    ("expect(page).toHaveURL(/x/);", "no-floating-expect"),
])
def test_error_rules_fire(line, rule):
    found = [f for f in P.lint_spec(line) if f["rule"] == rule]
    assert found, f"{rule} did not fire on: {line}"
    assert found[0]["severity"] == "error"
    assert found[0]["line"] == 1


def test_a_floating_retrying_matcher_is_an_error_but_a_sync_one_is_not():
    """A retrying matcher that is not awaited never runs — the step goes green on
    an unresolved promise. A sync matcher on a boolean the compiler computed
    itself is legitimate, and flagging it would fire on every generated spec."""
    assert errors("expect(field).toHaveValue('x');")
    assert not errors("expect(okV || okT).toBeTruthy();")
    assert not errors("await expect(field).toHaveValue('x');")


# ══════════════════════════════════════════════════════════════════════════
#  Calibration — compiler output must score ZERO errors
# ══════════════════════════════════════════════════════════════════════════
COMPILER_OUTPUT = "\n".join([
    "import { test, expect } from '@playwright/test';",
    "await page.goto('https://app.example/apply'); // entry navigation only",
    "await page.waitForLoadState('domcontentloaded').catch(() => {});",
    "await page.waitForLoadState('networkidle', { timeout: 2000 }).catch(() => {});",
    "await expect(page).toHaveURL(new RegExp('/apply'));",
    "await page.waitForURL(/apply/, { timeout: 15000 });",
    "await page.getByLabel('Email').fill('a@b.c');",
    "await expect(field).toHaveValue(__nxTok(D.email)).catch(() => {});",
    "await page.getByRole('radio', { name: v }).first().check({ timeout: 3000 });",
    "await expect(sel).not.toHaveValue('');",
    "const sp = page.locator('[class*=spinner i]').first();",
])


def test_compiler_generated_specs_produce_no_lint_errors():
    """The severity calibration that keeps this change regression-free: `error`
    is reserved for idioms the compiler provably never emits, so no
    COMPILER-GENERATED asset's risk score moves because lint became real."""
    assert errors(COMPILER_OUTPUT) == []


def test_deliberate_compiler_idioms_are_warnings_not_errors():
    """The visual coordinate fallback and tolerant `.catch()` oracles are real
    smells worth surfacing, and deliberate. Warnings are reported and never
    scored — `risk()` counts only severity == error."""
    spec = ("await page.mouse.click(120, 340);\n"
            "await expect(field).toHaveValue('x').catch(() => {});")
    assert errors(spec) == []
    assert rules(spec) == {"coordinate-interaction", "swallowed-assertion"}


# ══════════════════════════════════════════════════════════════════════════
#  Totality — [] must mean "ran, found nothing", never "failed to run"
# ══════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("bad", [None, "", "   ", 12345, {"a": 1}, b"bytes"])
def test_lint_never_raises_on_any_input(bad):
    """The call sites dropped their try/except on the strength of this. If
    lint_spec can raise, `lint = []` silently becomes a lie again."""
    assert isinstance(P.lint_spec(bad), list)


def test_a_clean_spec_returns_an_empty_list():
    assert P.lint_spec("await page.getByRole('button', { name: 'Go' }).click();") == []


def test_the_rules_version_is_published():
    """Reports state WHICH lint ran, so 'no findings' is attributable to a
    specific ruleset rather than to an unknown."""
    assert isinstance(P.LINT_RULES_VERSION, str) and P.LINT_RULES_VERSION


# ══════════════════════════════════════════════════════════════════════════
#  Precision — a lint nobody trusts gets switched off
# ══════════════════════════════════════════════════════════════════════════
def test_one_finding_per_rule_per_line():
    """`const h: ElementHandle = await page.$('#y')` violates the handle rule
    twice on one line. Reporting it twice inflates lint_errors — which feeds the
    risk model — without adding a fact anyone can act on."""
    spec = "const h: ElementHandle = await page.$('#y');"
    handle = [f for f in P.lint_spec(spec) if f["rule"] == "no-element-handle"]
    assert len(handle) == 1


def test_prose_in_a_trailing_comment_does_not_trip_a_code_rule():
    assert errors("await locator.click(); // do not use page.click() here") == []


def test_a_url_inside_a_string_is_not_mistaken_for_a_comment():
    """The comment stripper must not truncate at the `//` of a URL, or every rule
    after it on that line goes unchecked."""
    spec = "await page.goto('https://x.example/a'); await page.waitForTimeout(1);"
    assert [f["rule"] for f in errors(spec)] == ["no-arbitrary-sleep"]


def test_page_mouse_click_is_not_read_as_page_click():
    assert not [f for f in P.lint_spec("await page.mouse.click(1, 2);")
                if f["rule"] == "no-page-selector-action"]


def test_locator_actions_are_never_flagged():
    spec = "\n".join([
        "await page.getByLabel('Email').fill('a@b.c');",
        "await page.getByRole('button', { name: 'Submit' }).click();",
        "await page.locator('#x').check();",
        "await page.getByText('Next').press('Enter');",
    ])
    assert P.lint_spec(spec) == []


def test_line_numbers_are_reported_accurately():
    spec = "// header\nawait ok.click();\nawait page.waitForTimeout(1);"
    assert errors(spec)[0]["line"] == 3


def test_an_env_read_credential_is_not_reported_as_hardcoded():
    assert P.lint_spec("const password = process.env.APP_PASSWORD;") == []
    assert {f["rule"] for f in P.lint_spec("const password = 'hunter2xyz';")} == {"hardcoded-credential"}


# ══════════════════════════════════════════════════════════════════════════
#  Audit integrity — the risk model now sees a signal that was always zero
# ══════════════════════════════════════════════════════════════════════════
def test_lint_errors_raise_the_risk_likelihood():
    """`risk()` has weighted `lint_err` since it was written; with lint_spec
    missing that term was structurally always 0. The advertised model only
    starts computing what it claims once the lint actually runs."""
    from app.services.test_factory import verdict_events as ve
    det = {"gaps": 0, "per_step": []}
    clean = ve.risk(steps=[{"action": "x"}], det=det, lint=[], preflight=None)
    dirty = ve.risk(steps=[{"action": "x"}], det=det,
                    lint=P.lint_spec("await page.waitForTimeout(1);"), preflight=None)
    assert dirty["score"] > clean["score"]


def test_no_call_site_still_swallows_a_lint_failure():
    """Structural guard on the defect CLASS. A future `try: lint_spec() except:
    lint = []` would restore the exact ambiguity this task removed."""
    router = os.path.join(_API, "app", "routers", "test_factory.py")
    with open(router, "r", encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    for i, line in enumerate(lines):
        if "lint_spec(" in line:
            window = "\n".join(lines[max(0, i - 3):i + 4])
            assert "except Exception:\n            lint = []" not in window
            assert "lint = []" not in window, f"line {i+1} still swallows lint failures"
