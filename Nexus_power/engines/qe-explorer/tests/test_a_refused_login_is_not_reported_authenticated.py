"""A CRAWL THE APPLICATION REFUSED MUST NOT PRINT auth=True.

MEASURED (ERPNext v16, 2026-08-28). The crawl drove the login correctly --

    type   input#login_email      value_committed
    type   input#login_password   value_committed
    submit button.btn-login       none

-- ERPNext refused it, and the crawl stopped `auth_failed` with ONE state. The
summary line then read:

    erpnext   pages=1  fields=0/2  forms=0  ...  auth=True

The credentials were valid; `POST /api/method/login` with the same pair returns
{"message":"Logged In"}. So a reader of that line concludes ERPNext has one page
and authentication is fine. Both halves are wrong.

WHERE THE BUG IS NOT. `app/coverage.py` leaves `auth_incomplete` False here on
purpose: that flag means "no login form was found, so public content was crawled
unauthenticated and partial coverage exists". A refused login is a different
terminal -- a hard stop with nothing crawled. Setting the flag would mislead in
the other direction, and test_crawler_logic.py::
test_credentialed_crawl_login_wall_still_aborts_auth_failed pins that.

WHERE IT IS. The runner asked a THIRD question -- "did this crawl end up
authenticated?" -- and answered it with `not auth_incomplete`, a flag that never
claimed to answer it. The terminal already knew.
"""
from __future__ import annotations

from crawl_target import _stop_reason_of


def _authenticated(cov: dict) -> bool:
    return (not cov.get("auth_incomplete")
            and _stop_reason_of(cov) != "auth_failed")


# ── the measured regression ────────────────────────────────────────────────

def test_the_erpnext_shape_refused_login_is_not_authenticated():
    """THE BUG, with the bundle exactly as ERPNext's crawl wrote it."""
    cov = {"auth_incomplete": False, "auth_reason": "",
           "summary": {"stop_reason": "auth_failed"}}
    assert _authenticated(cov) is False


def test_a_top_level_stop_reason_is_read_too():
    """Bundles carry the terminal in either place; both must be seen."""
    cov = {"auth_incomplete": False, "stop_reason": "auth_failed"}
    assert _authenticated(cov) is False


# ── controls: what must NOT change ─────────────────────────────────────────

def test_a_genuine_authenticated_crawl_still_reports_true():
    """THE CONTROL. Without it the fix could just return False."""
    cov = {"auth_incomplete": False, "summary": {"stop_reason": "completed"}}
    assert _authenticated(cov) is True


def test_a_public_only_crawl_is_still_not_authenticated():
    cov = {"auth_incomplete": True, "auth_reason": "no_credentials",
           "summary": {"stop_reason": "completed"}}
    assert _authenticated(cov) is False


def test_a_bundle_with_no_terminal_at_all_is_unchanged():
    assert _authenticated({"auth_incomplete": False}) is True
    assert _stop_reason_of({}) == ""
