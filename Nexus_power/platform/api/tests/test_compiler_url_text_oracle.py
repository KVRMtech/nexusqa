"""F1 regression — URL-shaped text-oracle guard (run 7c89de7e step 7, 2026-07-24).

Root cause pinned by evidence: the generator threads the post-action URL into
``observed.after`` (crawl-substrate steps) and embeds destination URLs in the
step Expected Result ("The application proceeds to https://…").  The compiler's
``_grounded_expectation_token`` then grounded the token "https" against that
URL-valued "observed text" and emitted::

    await expect(page.getByText(/https/i).first()).toBeVisible()

— asserting the literal text "https" renders on the page.  No page renders its
own URL as text: a guaranteed false RED on EVERY app (the client-facing
"execution stops at step 7" failure).  The guard strips URL substrings from
both the token source and the ground, so a URL fragment can never ground a
text-visibility oracle; "proceeds to <URL>" stays covered by the CORRECT
oracle — the hard toHaveURL compiled from ``observed.next_url``.

Generic across apps: the guard keys on URL *shape* (scheme://, www.), not on
any host or domain vocabulary.

NO live stack / NO DB.  Run from Nexus_power/platform/api:
    python -m pytest tests/test_compiler_url_text_oracle.py -q
"""
from __future__ import annotations

import importlib
import os
import sys
import types

import pytest

_APP = os.path.join(os.path.dirname(__file__), "..", "app", "services")

# ── compiler: load as a SYNTHETIC package so its relative imports resolve ────
# (same approach as test_upload_generation.py)
_svc = types.ModuleType("svc"); _svc.__path__ = [_APP]
_sf = types.ModuleType("svc.script_factory")
_sf.__path__ = [os.path.join(_APP, "script_factory")]
sys.modules.setdefault("svc", _svc)
sys.modules.setdefault("svc.script_factory", _sf)
compiler = importlib.import_module("svc.script_factory.compiler")


# The REAL failing step (factory_test_cases fd3e65e9…, verbatim shape).
_STEP7_ER = (
    "The application proceeds to https://vkpowerlife.35-186-147-245.sslip.io/portal/apply; "
    "https://vkpowerlife.35-186-147-245.sslip.io/portal/apply?step=2&fname=Test&lname=User"
    "&dob=2026-07-24&gender=male&state=CT"
)
_STEP7_OBSERVED = {
    "kind": "button",
    "verb": "click",
    "label": "Continue",
    "after": (
        "https://vkpowerlife.35-186-147-245.sslip.io/portal/apply?step=2"
        "&fname=Test&lname=User&dob=2026-07-24&gender=male&state=CT"
    ),
    "next_url": "https://vkpowerlife.35-186-147-245.sslip.io/portal/apply",
    "navigation_grounded": True,
}


@pytest.fixture(autouse=True)
def _hard_oracle_policy(monkeypatch):
    """Pin the LEGACY hard-assert mode explicitly (P0.2 made the proven-only
    soft policy the DEFAULT; these tests prove the URL-guard holds even in the
    strict hard mode, where a bad oracle would fail a run)."""
    monkeypatch.setenv("NEXUS_PROVEN_NAV_ORACLE", "0")


# ── token-level guard ────────────────────────────────────────────────────────

def test_url_valued_ground_yields_no_token():
    """The exact run-7c89de7e inputs must ground NO text token."""
    tok = compiler._grounded_expectation_token(_STEP7_ER, _STEP7_OBSERVED["after"])
    assert tok == ""


def test_url_fragment_never_selected_from_er():
    """Scheme/host words inside a URL never become the grounded token."""
    er = "vkpowerlife shown at https://vkpowerlife.example/apply"
    ground = "Loaded https://vkpowerlife.example/apply"
    # After URL-stripping, the ground is "Loaded" — no ER word matches.
    assert compiler._grounded_expectation_token(er, ground) == ""


def test_real_page_text_grounding_unchanged():
    """Legit prose grounding is byte-identical to the pre-guard behavior."""
    tok = compiler._grounded_expectation_token(
        "A Thank message appears", "Thank you for your order"
    )
    assert tok == "Thank"


# ── step-level assertions (the compiled output the runner executes) ──────────

def test_step7_regression_nav_oracle_kept_bogus_text_oracle_gone():
    out = compiler._assertion_from_expected_result(
        _STEP7_OBSERVED, _STEP7_ER, nav_proven=True
    )
    joined = "\n".join(out)
    # The CORRECT oracle survives: hard toHaveURL on the recorded next path.
    assert "toHaveURL" in joined and "/portal/apply" in joined.replace("\\", "")
    # The guaranteed-red oracle is gone — no URL-fragment text assertion.
    assert "getByText(/https" not in joined
    assert "getByText(/http" not in joined
    assert "getByText(/www" not in joined
    # With a real oracle present, no UNVERIFIED hedge is added.
    assert "UNVERIFIED" not in joined


def test_url_only_after_without_nexturl_is_honest_unverified():
    """No grounded oracle at all -> the honest UNVERIFIED comment, never a
    fabricated assertion (and never a silent nothing)."""
    out = compiler._assertion_from_expected_result(
        {"after": "https://app.example/portal/apply?step=2"},
        "The application proceeds to https://app.example/portal/apply",
        nav_proven=True,
    )
    joined = "\n".join(out)
    assert "await expect(" not in joined
    assert "UNVERIFIED" in joined


def test_region_oracle_not_faked_by_url_path():
    """A region word living only in a URL path/query must not mint a region
    text oracle ('/portal/dashboard?view=summary' renders neither word)."""
    out = compiler._assertion_from_expected_result(
        {"after": "https://app.example/portal/dashboard?view=summary"}, "",
        nav_proven=True,
    )
    assert not any("getByText" in line for line in out)


def test_region_oracle_still_fires_on_real_page_text():
    """Positive control: genuine outcome text keeps its region oracle."""
    out = compiler._assertion_from_expected_result(
        {"after": "Order summary is shown"}, "", nav_proven=True
    )
    joined = "\n".join(out)
    assert "getByText(/summary/i)" in joined and "toBeVisible" in joined
