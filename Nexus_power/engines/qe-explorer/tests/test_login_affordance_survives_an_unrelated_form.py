"""A MARKETING FORM ON THE LANDING PAGE MUST NOT HIDE THE LOGIN.

WHAT WAS WRONG.  ``Authenticator.login`` already knows how to reach a login
that sits behind a button -- step (a) clicks a sign-in affordance when the page
carries neither a username nor a password field, bounded to two hops.  The
guard on that loop is:

    while hops < 2 and _match_password_control(...) is None
                   and _match_username_control(...) is None:

and ``_match_username_control`` ENDS IN AN UNCONDITIONAL FALLBACK -- ``return
text_fields[0]`` -- so it never answers None on any page that has a text input.
The fallback is deliberate and is needed by the step loop, where a
username-first screen legitimately has a username and no password.  Used as the
loop's guard, though, it means ANY text field anywhere suppresses the hop.

MEASURED (LifeOps, 2026-08-27, from 7d7408b).  The landing page carries a quote
form -- Age, Coverage amount, Term length -- and three separate sign-in
affordances ("Login", "Sign in", "Member login").  Running the real
Authenticator against the live application:

    qec.auth.username_fill_uncommitted control='age'
      detail=action_error: Cannot type text into input[type=number]
    qec.auth.login_attempt success=False reason=login_unverified

``Age`` was taken as the username, so the hop never ran, no affordance was ever
clicked, and a correct member number + PIN + OTP went unused.  The crawl then
explored the application unauthenticated and said so honestly -- but the entire
authenticated surface (12 sections, 6 personas) was never reached.

THE FIX IS TO ASK A STRICTER QUESTION AT THE GUARD, not to remove the fallback.
Deciding "is there a login form here?" needs a CONFIDENT username match -- one
that matched a hint -- while the step loop keeps the positional fallback it
relies on.  A field that matched no hint, on a page with no password field, is
not evidence of a login form.

WHAT IS ASSERTED HERE.  The defect and the control it must not break:
multi-step (username-first) login, whose whole shape is a username field with
no password, must still work.
"""
from __future__ import annotations

import asyncio

from app.auth import AuthWindow, Authenticator
from test_auth import (_CREDS, _REFUSE, FakeClock, _NumberRejectingBrowser,
                       _btn, _login, _observe, _pass, _raw, _user)


def _number(name: str):
    """A quote-form field. Playwright REFUSES text into input[type=number], so
    this is also the shape that produced the live `fill` error."""
    # POST-INVENTORY shape, which is what login() sees: build_inventory
    # normalises role=spinbutton to kind="text", which is precisely why
    # _text_fields accepts it and the fallback claims it as the username.
    return _raw("textbox", name, kind="text", input_type="number")


def test_a_quote_form_does_not_suppress_the_sign_in_hop():
    """THE DEFECT, with the measured page shape: an unrelated form plus a
    sign-in affordance on the same landing page."""
    pages = {
        "https://app/": {
            "controls": [_number("Age"), _raw("combobox", "Coverage amount"),
                         _btn("Member login")],
            "nav": {"Member login": "https://app/login"}},
        "https://app/login": {
            "controls": [_user("Member ID"), _pass("PIN"), _btn("Continue")],
            "nav": {"Continue": "https://app/home"}},
        "https://app/home": {"controls": [_btn("Log out")]},
    }
    # The NUMBER-REJECTING port is what makes this faithful. Playwright
    # REFUSES `fill` on input[type=number], so on the live application the
    # mis-chosen "Age" username could not even be typed and the sequence
    # died there. A permissive fake would type into Age, then click
    # "Member login" as if it were the submit button, and accidentally
    # succeed -- hiding the defect entirely.
    port = _NumberRejectingBrowser(pages, "https://app/")
    auth = Authenticator(port, _CREDS, FakeClock(), _REFUSE,
                         AuthWindow(max_requests=50, window_ms=10 ** 9),
                         max_relogins=1)
    res = asyncio.run(auth.login(_observe(port)))
    assert res.success, (
        f"the login behind the affordance was never reached: {res.reason}")


def test_a_username_first_multi_step_login_still_works():
    """THE CONTROL.  A username-first screen has a username field and NO
    password -- structurally identical to the defect case.  Tightening the
    guard must not make the authenticator hop AWAY from a real login form."""
    pages = {
        "https://app/login": {
            "controls": [_user("Email"), _btn("Next")],
            "nav": {"Next": "https://app/login2"}},
        "https://app/login2": {
            "controls": [_pass(), _btn("Sign in")],
            "nav": {"Sign in": "https://app/home"}},
        "https://app/home": {"controls": [_btn("Log out")]},
    }
    res = _login(pages, "https://app/login", _CREDS)
    assert res.success, res.reason


def test_site_chrome_does_not_supply_the_login_forms_submit_button():
    """THE SECOND HALF OF THE SAME DEFECT.

    Reaching the login form is not enough if the button then clicked belongs to
    the page's navigation. ``_match_submit_control`` matched hints against every
    control on the page and took the first, and site chrome wins on DOM order.

    Measured (LifeOps, 2026-08-27), after the hop was fixed:

        username -> Member ID    password -> PIN    SUBMIT -> Login

    "Login" is a persistent top-nav item emitted before the form's own
    "Continue". Clicking it re-rendered the same screen, and the sequence
    reported "the screen did not move, and was given time to" -- a failed login
    with correct credentials already typed into the right fields.
    """
    pages = {
        "https://app/login": {
            # nav chrome FIRST, exactly as the application emits it
            "controls": [_btn("Login"), _btn("Documents"),
                         _user("Member ID"), _pass("PIN"), _btn("Continue")],
            # the nav item re-renders this same screen; only Continue advances
            "nav": {"Login": "https://app/login", "Continue": "https://app/home"}},
        "https://app/home": {"controls": [_btn("Log out")]},
    }
    res = _login(pages, "https://app/login", _CREDS)
    assert res.success, (
        f"the navigation item was clicked instead of the form's submit: {res.reason}")
