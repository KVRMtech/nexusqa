"""RUNG 4's PLUMBING, DRIVEN THROUGH A REAL BROWSER.

``MintRegistry`` is proven against dictionaries elsewhere. That proves the RULE
and nothing about whether anything can ever feed it — the failure this repository
calls a blind verifier: a check that passes with its subject absent.

So these drive ``MINTED_JS`` itself, in Chromium, over the three shapes real
confirmation screens actually use (a definition list, a two-cell table row, and
a "Label: VALUE" line), and then run the registry over what the browser really
returned. If the extractor stops seeing a confirmation panel, this goes red.

WHAT IS NOT CLAIMED HERE. This proves the extractor reads a confirmation page
and the registry mints from it. It does NOT prove a live crawl has reached one:
as of 2026-08-30 the vkpowerlife funnel stops at ``/apply/decision/`` — an async
processing step, step 6 of 10 — so ``forms_confirmed`` is still 0 and no crawl
has yet minted a reference in the field. That is a traversal gap, recorded in
the crawl evidence, not a gap in this rung.
"""
from __future__ import annotations

import pytest

from app.minted import MINTED_JS, MintRegistry

pytest.importorskip("playwright.sync_api")

#: The three shapes, on one page, exactly as applications render them — plus the
#: two traps that sit beside a real reference on a real confirmation screen: the
#: customer's OWN account number (pre-existing) and a date (id-shaped).
_CONFIRMATION = """
<h1>Application Submitted</h1>
<p>Application Number: APP-2026-8871</p>
<dl>
  <dt>Policy Number</dt><dd>POL-44120</dd>
  <dt>Effective Date</dt><dd>12/05/2026</dd>
</dl>
<table>
  <tr><th>Claim Reference</th><td>CLM-2026-0091</td></tr>
  <tr><th>Annual Premium</th><td>$1,240.00</td></tr>
</table>
<span>Account Number: ACCT-0001</span>
"""

#: The page as it looked BEFORE the submit. The account number is already here,
#: which is what makes it a trap rather than a decoration.
_BEFORE = "<h1>Review</h1><span>Account Number: ACCT-0001</span>"


def _read(html):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content(html)
        pairs = page.evaluate(MINTED_JS)
        browser.close()
    return pairs


def test_a_real_browser_finds_the_reference_in_all_three_shapes():
    pairs = _read(_CONFIRMATION)
    found = {p["label"].strip().rstrip(":#").strip(): p["value"] for p in pairs}
    assert found.get("Application Number") == "APP-2026-8871", "the inline shape"
    assert found.get("Policy Number") == "POL-44120", "the definition list"
    assert found.get("Claim Reference") == "CLM-2026-0091", "the table row"


def test_the_registry_mints_from_what_the_browser_actually_returned():
    """END TO END for this rung: a real page in, a downstream flow's key out."""
    reg = MintRegistry()
    reg.observe(_read(_BEFORE))
    reg.mint(_read(_CONFIRMATION))

    assert reg.value_for("Policy Number") == "POL-44120"
    assert reg.value_for("Application Number") == "APP-2026-8871"
    assert reg.value_for("Claim Reference") == "CLM-2026-0091"


def test_the_traps_on_the_same_page_are_not_minted():
    """The customer's own account number was there BEFORE the submit; the date
    and the premium are id-shaped but are not references. All three sit on the
    confirmation panel beside the real ones."""
    reg = MintRegistry()
    reg.observe(_read(_BEFORE))
    reg.mint(_read(_CONFIRMATION))

    assert reg.value_for("Account Number") is None, "pre-existing, not minted"
    assert reg.value_for("Effective Date") is None, "a date is not a reference"
    assert reg.value_for("Annual Premium") is None, "money is not a reference"


def test_the_control_without_the_baseline_the_account_number_would_be_minted():
    """FALSIFICATION CONTROL, and the sharpest one in this file. It removes the
    act-then-diff baseline and requires the account number to be minted — which
    is exactly the wrong answer. Without it, an extractor that silently returned
    NOTHING would satisfy every refusal above and look like a working rule."""
    reg = MintRegistry()
    reg.mint(_read(_CONFIRMATION))          # no observe() — no baseline
    assert reg.value_for("Account Number") == "ACCT-0001"


def test_a_page_with_no_confirmation_panel_yields_nothing():
    reg = MintRegistry()
    reg.mint(_read("<h1>Home</h1><p>Welcome back.</p>"))
    assert reg.count == 0
