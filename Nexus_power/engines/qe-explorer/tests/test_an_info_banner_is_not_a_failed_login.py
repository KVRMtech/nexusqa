"""An informational banner on the landing page is not a failed login.

MEASURED on orangehrm.136-85-106-73.sslip.io, 2026-09-04. A Target-mode crawl
entered at /web/index.php/recruitment/viewCandidates - the page a client had
actually asked us to crawl - authenticated correctly, and was then failed:

    qec.crawler.login_failed reason=login_failed: error region present
        ('Info / No Records Found / x')
    qec.crawler.completed stop_reason=auth_failed states=1

"No Records Found" is OrangeHRM's EMPTY-LIST notice on the candidates page -
the page you reach BY logging in. It renders informational toasts as
role="alert", and role="alert" is exactly what BrowserPort.error_texts
collects, so a benign notice was indistinguishable from a rejected password.

WHY IT HAD NEVER BEEN SEEN. Entering at the site root lands on the dashboard,
which shows no banner, so every previous crawl passed. It bit only the deep
entry point - which is the entire reason a user selects Target mode.

WHY THE CONTROLS BELOW ARE THE POINT. The guard being narrowed is what stops a
REJECTED PASSWORD being reported as a successful login, which would green-wash
every gated application at once. So the tests that must never stop passing are
the ones asserting that a real login failure still fails.
"""

from __future__ import annotations

import pytest

from app.auth import _reads_as_login_error, verify_login_success


def _verify(after_errors, *, controls=None, moved=True):
    return verify_login_success(
        before_fingerprint="fp_login",
        after_fingerprint="fp_landing" if moved else "fp_login",
        after_controls=controls or [],
        after_errors=after_errors,
    )


#: Verbatim from the measured crawl (the multiplication sign is OrangeHRM's
#: dismiss affordance, and it rides inside the same live region).
ORANGEHRM_BANNER = "Info\n\nNo Records Found\n\n×"

PASSWORD_FIELD = {"kind": "text", "name": "Password", "role": "textbox",
                  "qec": {"input_type": "password", "role": "textbox"}}


def test_the_measured_banner_no_longer_fails_the_login():
    """The exact string that cost a client's crawl every one of its pages."""
    ok, reason = _verify([ORANGEHRM_BANNER])
    assert ok, (
        "an empty-list notice on the page reached BY logging in was read as a "
        "login failure; got reason=%r" % reason
    )
    assert reason == "login_verified"


@pytest.mark.parametrize("text", [
    "Invalid credentials",
    "Username or password is incorrect",
    "Login failed. Please try again.",
    "Access denied",
    "Your account has been locked",
    "Password expired",
    "Unauthorized",
])
def test_a_real_login_failure_still_fails(text):
    """CONTROL - the guard this narrows.

    If these ever pass, a rejected password reads as a successful login and
    every gated application is silently crawled as an anonymous visitor while
    reporting that it authenticated.
    """
    ok, reason = _verify([text])
    assert not ok, "a rejected login must never verify: %r" % text
    assert reason.startswith("login_failed: error region present")


def test_a_benign_region_with_the_form_still_present_fails():
    """CONTROL - never left the login screen.

    A password field still on screen is direct evidence the form was not
    passed, whatever the banner says. verify_login_success returns on the
    password check before the region is even considered, and that ordering is
    what this pins.
    """
    ok, reason = _verify([ORANGEHRM_BANNER], controls=[PASSWORD_FIELD])
    assert not ok
    assert "password field is still present" in reason


def test_a_screen_that_never_moved_still_fails():
    """CONTROL - the first of the three independent signals."""
    ok, reason = _verify([ORANGEHRM_BANNER], moved=False)
    assert not ok
    assert "fingerprint unchanged" in reason


def test_no_region_at_all_is_unchanged():
    ok, reason = _verify([])
    assert ok and reason == "login_verified"


@pytest.mark.parametrize("benign", [
    "Info\n\nNo Records Found\n\n×",
    "No Records Found",
    "Showing 1 to 20 of 45 entries",
    "Success",
    "Record saved",
    "3 results",
])
def test_the_classifier_lets_page_notices_through(benign):
    assert not _reads_as_login_error(benign)


@pytest.mark.parametrize("bad", [
    "Invalid credentials", "incorrect password", "Login FAILED",
    "Access Denied", "session expired", "account locked",
    "This field is required", "Unable to sign in",
])
def test_the_classifier_catches_error_vocabulary(bad):
    assert _reads_as_login_error(bad)


def test_the_classifier_is_not_fooled_by_an_empty_value():
    for empty in ("", None, "   "):
        assert not _reads_as_login_error(empty)
