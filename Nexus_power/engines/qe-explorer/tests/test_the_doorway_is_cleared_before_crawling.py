"""A COOKIE BANNER IS NOT THE APPLICATION.

MEASURED (OWASP Juice Shop, 2026-08-27). The site opens behind two stacked
overlays — a welcome banner and a cookie-consent bar — and the crawl ended with
TWO pages of a shop that has a catalogue, a basket, a login and a registration
form. The inventory was not wrong; it reported the nine controls that were
genuinely on screen:

    cookieconsent · learn more about cookies · dismiss cookie message
    Close Welcome Banner · school Help getting started · Force page reload

The exits were catalogued and never taken. A human clears the doorway without
thinking about it; the crawl stood in it.

WHY THIS IS AN ENTRY-TIME STEP AND NOTHING MORE. Clicking "close" wherever it
appears would shut panels the crawl had deliberately opened, and mid-flow it
could dismiss the very confirmation a journey was walked to reach. The doorway
is cleared ONCE, before exploration begins, which is exactly when a consent
layer is on screen and nothing else is.

WHAT IS DELIBERATELY NOT DISMISSABLE. Anything that advances, commits, or is
refuse-pack flagged. "Accept" clears a cookie bar; "Accept Quote" is a business
act, and the vocabulary must not confuse the two.
"""
from __future__ import annotations

import pytest

from app.discovery import overlay_dismiss_candidates


def _c(name, kind="button", **kw):
    return {"name": name, "kind": kind, **kw}


# ── the measured doorway ────────────────────────────────────────────────────

def test_juice_shops_two_overlays_are_both_recognised():
    """THE MEASURED INVENTORY, verbatim from the live crawl."""
    inv = [_c("cookieconsent"), _c("learn more about cookies", "link"),
           _c("dismiss cookie message", "link"), _c(""),
           _c("Open Worldwide Application Security Project (OWASP)", "link"),
           _c("school Help getting started"), _c("Close Welcome Banner"),
           _c("Force page reload")]
    names = [c["name"] for c in overlay_dismiss_candidates(inv)]
    assert "dismiss cookie message" in names
    assert "Close Welcome Banner" in names


@pytest.mark.parametrize("label", [
    "Dismiss", "Close", "Got it", "No thanks", "Accept cookies",
    "Allow cookies", "Allow all cookies", "OK",
])
def test_ordinary_consent_vocabulary_is_recognised(label):
    assert overlay_dismiss_candidates([_c(label)]), label


# ── what must never be clicked as "clearing the doorway" ────────────────────

@pytest.mark.parametrize("label,why", [
    ("Continue", "an advance — the walk owns those"),
    ("Save and continue", "an advance"),
    ("Next", "an advance"),
    ("Submit", "a commit"),
    ("Sign document", "a commit"),
    ("Delete account", "a commit, and destructive"),
    ("Accept Quote", "a BUSINESS act that happens to start with Accept"),
    ("Close Policy", "a business act, not a banner"),
    ("I agree", "ambiguous - agreeing to terms can be a legal act on a real "
                 "form; only a cookie-BOUND consent is admitted"),
    ("Accept", "bare accept says nothing about what is accepted"),
    ("Pay now", "a commit"),
])
def test_an_action_is_never_mistaken_for_a_doorway(label, why):
    assert not overlay_dismiss_candidates([_c(label)]), f"{label!r} — {why}"


def test_a_flagged_control_is_never_dismissable():
    """Whatever it is called, a refuse-pack flagged control is not a banner."""
    assert not overlay_dismiss_candidates([_c("Close", danger=True)])


def test_a_disabled_or_nameless_control_is_skipped():
    assert not overlay_dismiss_candidates([_c("Dismiss", disabled=True)])
    assert not overlay_dismiss_candidates([_c("")])
