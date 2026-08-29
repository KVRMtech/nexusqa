"""AN APPLICATION THAT SIGNS IN BY XHR MUST BE GIVEN TIME TO REDIRECT.

MEASURED (ERPNext v16, 2026-08-28). The crawl drove the login exactly right --

    type   input#login_email      value_committed
    type   input#login_password   value_committed
    submit button.btn-login       none

-- and reported `login_unverified`, stopping `auth_failed` with ONE state. The
credentials were valid: POST /api/method/login with the same pair returns
{"message":"Logged In"}. ERPNext signs in by XHR and redirects client-side once
it resolves.

The login loop already HAS a settle window (LATE_ADVANCE_WAIT_MS = 4000ms over
LATE_ADVANCE_LOOKS = 4). ERPNext got none of it, because the only signal that
kept the loop waiting was a DISABLED control:

    _busy = any(bool(c.get("disabled")) for c in after_controls)
    if not _busy:
        break

ERPNext disables nothing while its XHR is in flight, so the loop broke on the
first look and judged the screen before the redirect could happen -- while
logging "the screen did not move, and was given time to", which was not true.

WHAT MUST NOT REGRESS. The disabled-control rule was chosen after two documented
failures, and both are pinned below as controls:

  * summit-life-carrier flips its button to "Authenticating..." and DISABLES it.
    That MOVES the fingerprint while saying the answer has not arrived, so
    "fingerprint moved" alone cannot end the wait.
  * that same page carries "Sign in with Google SSO" and "Sign in with
    Enterprise SSO", so a submit-shaped control is present on every observation,
    busy or not -- "something to submit" cannot end the wait either.

The added signal is neither: a PASSWORD FIELD still on screen AND a fingerprint
that has not moved is the application saying, structurally, that it is still on
the login form. Bounded by the same 4000ms budget, so a genuinely refused login
costs at most that before being reported honestly.
"""
from __future__ import annotations

from app.auth import login_screen_is_still_settling as settling


# ── the measured regression ────────────────────────────────────────────────

def test_the_erpnext_shape_nothing_disabled_but_screen_has_not_moved():
    """THE BUG. No busy signal, still on the login form -> keep waiting."""
    assert settling(busy=False, fingerprint_moved=False, password_present=True) is True


# ── the two documented failures that must stay fixed ───────────────────────

def test_summits_disabled_button_still_holds_the_wait():
    """Fingerprint MOVED (button became 'Authenticating...') but it is disabled."""
    assert settling(busy=True, fingerprint_moved=True, password_present=False) is True


def test_a_submit_shaped_control_alone_does_not_hold_the_wait():
    """The SSO buttons are always present; they must not extend the wait."""
    assert settling(busy=False, fingerprint_moved=True, password_present=False) is False


# ── the bound: this must still terminate ───────────────────────────────────

def test_a_screen_that_moved_and_lost_its_password_field_is_done():
    assert settling(busy=False, fingerprint_moved=True, password_present=False) is False


def test_a_password_field_on_a_screen_that_MOVED_does_not_hold_the_wait():
    """A second login step (password again on a NEW screen) is an advance."""
    assert settling(busy=False, fingerprint_moved=True, password_present=True) is False
