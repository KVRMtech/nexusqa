"""Failure attribution (F4) — whose fault was a failed step?

Doctrine: NEVER classify a product limitation as an application defect. When a
step fails on an assertion the PRODUCT generated badly, the run report must say
"product-side script defect" — not paint the client's application red.

Deterministic + evidence-only: an attribution is claimed ONLY for failure
shapes PROVABLE from the Playwright error text itself; anything else returns
``None`` (no claim — honest silence over speculation, and the existing verdict
stands unchanged).

First class covered — the URL-as-text oracle (run 7c89de7e step 7,
2026-07-24): ``expect(page.getByText(/https/i).first()).toBeVisible()``
asserts text NO page ever renders (a page does not display its own URL), so
its failure is a product defect BY CONSTRUCTION — the application may have
behaved perfectly (in the evidenced run, the click worked and the hard
toHaveURL navigation oracle PASSED before this assertion fired).

Defence-in-depth pairing:
  * playwright_auditor.V_URL_TEXT BLOCKS newly-compiled specs of this shape;
  * compiler._strip_urls stops the generator grounding such oracles at all;
  * this module keeps runs of LEGACY specs honestly attributed at ingest.

Generic across apps: keys on URL *shape* in the error text, never a host.
"""
from __future__ import annotations

import re

# Attribution tiers — "script_defect" is CONFIRMED product-side (cannot be an
# application failure); "…_candidate" is probable, surfaced for review.
ATTR_SCRIPT_DEFECT = "script_defect"
ATTR_SCRIPT_DEFECT_CANDIDATE = "script_defect_candidate"

# Bare URL scheme/www token asserted as visible text — always-RED by construction.
_URL_TOKEN_ORACLE_RX = re.compile(
    r"getByText\(\s*/\s*(?:https?|www)\b", re.IGNORECASE)
# Full quoted URL asserted as visible text — wrong unless the page renders URLs.
_URL_QUOTED_ORACLE_RX = re.compile(
    r"getByText\(\s*['\"](?:https?://|www\.)", re.IGNORECASE)


def classify_step_failure(error_message: str | None) -> dict | None:
    """Classify a failed step's error into a product-vs-application attribution.

    Returns a dict ``{attribution, cause, blame, detail}`` when the failure
    shape is provably product-side, else ``None`` (no claim).  Pure + $0 —
    safe to call on every ingested failed step.
    """
    err = str(error_message or "")
    if not err.strip() or "expect(" not in err:
        # Only EXPECT (oracle) failures are in scope — action/locator failures
        # route to the heal pipeline, and absence of evidence is not evidence.
        return None

    if _URL_TOKEN_ORACLE_RX.search(err):
        return {
            "attribution": ATTR_SCRIPT_DEFECT,
            "cause": "url_as_text_oracle",
            "blame": "product",
            "detail": (
                "The failing assertion expects a URL fragment to be visible as "
                "page text (getByText(/https|http|www/)). No page renders its "
                "own URL as text — this is a generated-oracle defect in the "
                "test script, NOT an application failure. The step's real "
                "oracles (the action and any navigation check) are unaffected."
            ),
        }
    if _URL_QUOTED_ORACLE_RX.search(err):
        return {
            "attribution": ATTR_SCRIPT_DEFECT_CANDIDATE,
            "cause": "url_string_text_oracle",
            "blame": "product_probable",
            "detail": (
                "The failing assertion expects a full URL string as visible "
                "page text. Unless this page genuinely renders URLs as text, "
                "this is a generated-oracle defect (product-side), not an "
                "application failure."
            ),
        }
    return None


def summarize_attributions(attributions: list[dict | None]) -> dict | None:
    """Run-level rollup for ``test_run.metadata_json`` — counts per tier so
    list views can say "failed on a product-side script defect" without
    re-reading every step row.  Returns None when nothing was attributed."""
    counts: dict[str, int] = {}
    for a in attributions:
        if a:
            counts[a["attribution"]] = counts.get(a["attribution"], 0) + 1
    if not counts:
        return None
    return {
        "counts": counts,
        "product_side": ATTR_SCRIPT_DEFECT in counts,
    }
