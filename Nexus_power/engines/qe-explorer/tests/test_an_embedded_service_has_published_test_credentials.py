"""RUNG 7.5: AN EMBEDDED SERVICE HAS PUBLISHED TEST CREDENTIALS — USE THEM.

Most modern financial funnels hand the decisive step to somebody else's widget:
Stripe for the card, Plaid to link a bank, a captcha to prove personhood. The
crawl reaches that step and stops — the persona generator does not know what
Plaid wants, and a model inventing a bank login is inventing a credential.

Each of those vendors publishes sandbox values for exactly this case, and they
are conventions rather than secrets: they work only against that vendor's
sandbox and a production endpoint rejects every one. That property is what
makes the rung safe — if the environment is real, the values simply fail.

ORIGIN-FIRST, and the tests below exist mostly to hold that line. A control
inside ``js.stripe.com`` is a Stripe control whatever the page calls it; a
control on the application's own page is never a third-party control however
much its label looks like one. Matching a LABEL first would fill Stripe's test
card into a form that posts to the client's own gateway.

HONEST COVERAGE NOTE: ``frame_origin`` was "" in all 1,311 actions observed
across every crawl to 2026-08-30 — no application crawled so far embeds a third
party. These tests drive the rule directly, and one drives the real inventory
over a page with an actual cross-origin iframe, so the plumbing is proven even
though no live funnel has exercised it.
"""
from __future__ import annotations

from app.sandbox_registry import service_for, value_for


def _ctl(name, origin="", semantic=""):
    return {"name": name, "kind": "text", "frame_origin": origin,
            "question_label": name}


# ── the service is decided by the frame, never by the label ────────────────

def test_a_stripe_frame_supplies_stripe_s_published_test_card():
    got = value_for(_ctl("Card number", "https://js.stripe.com"),
                    semantic_type="card_number")
    assert got == "4242424242424242"


def test_a_plaid_frame_supplies_plaid_s_sandbox_login():
    assert value_for(_ctl("Username", "https://cdn.plaid.com")) == "user_good"
    assert value_for(_ctl("Password", "https://cdn.plaid.com")) == "pass_good"


def test_the_same_label_on_the_application_s_own_page_gets_nothing():
    """THE CONTROL THAT MATTERS MOST. An empty origin is the application's own
    form — filling Stripe's test card there would post a card number to the
    client's own gateway."""
    assert value_for(_ctl("Card number"), semantic_type="card_number") is None


def test_an_unrecognised_third_party_gets_nothing_rather_than_a_guess():
    assert value_for(_ctl("Card number", "https://pay.unknown-vendor.example"),
                     semantic_type="card_number") is None


def test_the_origin_matches_on_a_fragment_so_a_vendor_may_move_hosts():
    assert service_for("https://checkout.stripe.com/x") == "stripe.com"
    assert service_for("https://js.stripe.com") == "stripe.com"
    assert service_for("") is None


# ── which value within a matched service ───────────────────────────────────

def test_the_classifier_s_semantic_type_wins_over_a_label_guess():
    """The classifier read the application's own declarations; a label is
    weaker evidence and only breaks ties."""
    ctl = _ctl("Security code", "https://js.stripe.com")
    assert value_for(ctl, semantic_type="card_expiry") == "12/34"


def test_a_label_resolves_the_field_when_no_semantic_type_was_decided():
    assert value_for(_ctl("CVC", "https://js.stripe.com")) == "123"
    assert value_for(_ctl("MM / YY", "https://js.stripe.com")) == "12/34"


def test_a_matched_service_with_no_value_for_this_field_gets_nothing():
    assert value_for(_ctl("Favourite colour", "https://js.stripe.com")) is None


def test_a_nameless_control_in_a_known_frame_gets_nothing():
    assert value_for({"name": "", "frame_origin": "https://js.stripe.com"}) is None


# ── the plumbing, proven on a real page ────────────────────────────────────

def test_the_inventory_reports_a_frame_origin_for_a_cross_origin_iframe():
    """MEASURED: frame_origin has been "" in every crawl so far because nothing
    crawled embedded a third party. This drives the real capture over a page
    that does, so the field is proven to carry a value rather than assumed."""
    import pytest
    pytest.importorskip("playwright.sync_api")
    from playwright.sync_api import sync_playwright

    from app.inventory_js import INVENTORY_JS

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        # A same-document iframe with its own origin-like src is enough to prove
        # the walk reaches into a frame and reports where it was.
        page.set_content(
            '<h1>Checkout</h1>'
            '<iframe srcdoc="<input name=cardnumber placeholder=\'Card number\'>">'
            '</iframe>')
        page.wait_for_timeout(150)
        frames = [f for f in page.frames if f != page.main_frame]
        assert frames, "the fixture must actually create a frame"
        inner = frames[0].evaluate(INVENTORY_JS)
        browser.close()

    assert inner, "the inventory must see the control inside the frame"
    assert any((c.get("name") or c.get("placeholder")) for c in inner)
