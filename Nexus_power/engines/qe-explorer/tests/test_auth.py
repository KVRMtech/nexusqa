"""Pure-logic tests for the generalized authenticator (auth.py).

No browser, no network: the login state machine is driven through a scripted
:class:`FakeBrowser` (BrowserPort), proving single-step, multi-step, and MFA
(fixed-OTP + TOTP) logins + honest failure — the same harness pattern as
test_crawler_logic. Also pins the stdlib RFC-6238 TOTP against the spec vector.
"""
from __future__ import annotations

import asyncio
import base64
import dataclasses

from app import auth
from app.auth import (
    Authenticator,
    AuthWindow,
    Credentials,
    MfaConfig,
    match_delivery_control,
    match_login_controls,
    match_otp_control,
    totp_code,
)
from app.browser import BrowserPort, NavResult, RawObservation, PageObservation
from app.config import Settings
from app.guard import load_refuse_pack
from app.inventory import build_inventory

_REFUSE = load_refuse_pack(Settings().refuse_pack_path)


# ─── fakes ──────────────────────────────────────────────────────────────────

class FakeClock:
    def __init__(self) -> None:
        self.t = 0

    def now_ms(self) -> int:
        self.t += 1
        return self.t


def _raw(role: str, name: str, **over):
    base = {
        "role": role, "name": name, "name_source": "content", "best_effort": False,
        "kind": role, "tag": over.pop("tag", "input" if role == "textbox" else role),
        "input_type": "", "options": [], "required": False, "disabled": False,
        "frame_selector": "", "testid": "", "css_hint": "", "value_committed": "",
        "landmark": {"role": "", "name": ""},
    }
    base.update(over)
    return base


def _user(name="Username"):
    return _raw("textbox", name, input_type="text")


def _pass(name="Password"):
    return _raw("textbox", name, input_type="password")


def _otp(name="Verification code"):
    return _raw("textbox", name, input_type="text")


def _btn(name):
    return _raw("button", name, tag="button")


def _radio(name):
    return _raw("radio", name)


class FakeBrowser(BrowserPort):
    """Scripted pages keyed by URL. A click on a control whose name is in the
    page's ``nav`` map navigates to the destination URL (models submit → next
    screen); fills stay on the page."""

    def __init__(self, pages: dict, start: str) -> None:
        self._pages = pages
        self._cur = start

    def _p(self) -> dict:
        return self._pages.get(self._cur, {"controls": [], "errors": [], "nav": {}})

    async def goto(self, url: str) -> NavResult:
        self._cur = url
        return NavResult(url=url, ok=url in self._pages)

    async def current_url(self) -> str:
        return self._cur

    async def title(self) -> str:
        return ""

    async def collect_controls(self):
        return [dict(c) for c in self._p().get("controls", [])]

    async def dialog_flags(self):
        return []

    async def error_texts(self):
        return list(self._p().get("errors", []))

    async def screenshot_png(self) -> bytes:
        return b"x"

    async def click(self, control):
        before = self._cur
        dest = self._p().get("nav", {}).get(control.get("name"))
        if dest:
            self._cur = dest
            return RawObservation(url_before=before, url_after=dest)
        return RawObservation(url_before=before, url_after=before)

    async def fill(self, control, value):
        return RawObservation(url_before=self._cur, url_after=self._cur, committed_value=value)

    async def select_option(self, control, value):
        return RawObservation(url_before=self._cur, url_after=self._cur, committed_value=value)

    async def set_checked(self, control, checked):
        return RawObservation(url_before=self._cur, url_after=self._cur,
                              committed_value="true" if checked else "false")

    async def storage_state(self):
        return {"cookies": [{"name": "sid", "value": "abc"}], "origins": []}


def _mk_auth(pages: dict, start: str, creds: Credentials) -> tuple[Authenticator, FakeBrowser]:
    port = FakeBrowser(pages, start)
    a = Authenticator(port, creds, FakeClock(), _REFUSE,
                      AuthWindow(max_requests=50, window_ms=10 ** 9), max_relogins=1)
    return a, port


def _observe(port: FakeBrowser) -> PageObservation:
    return asyncio.run(_observe_async(port))


async def _observe_async(port: FakeBrowser) -> PageObservation:
    return PageObservation(
        url=await port.current_url(), title="",
        raw_controls=await port.collect_controls(),
        dialog_flags=await port.dialog_flags(),
        error_texts=await port.error_texts(),
    )


def _login(pages: dict, start: str, creds: Credentials):
    a, port = _mk_auth(pages, start, creds)
    obs = _observe(port)
    return asyncio.run(a.login(obs))


# ─── TOTP (RFC 6238) ────────────────────────────────────────────────────────

_RFC_SEED = base64.b32encode(b"12345678901234567890").decode()  # the spec's SHA1 seed


def test_totp_matches_rfc6238_vector():
    # RFC 6238 Appendix B, SHA1, T=59 ⇒ 8-digit code 94287082.
    assert totp_code(_RFC_SEED, digits=8, period=30, at_unix=59) == "94287082"
    # 6-digit truncation is the last 6 digits.
    assert totp_code(_RFC_SEED, digits=6, period=30, at_unix=59) == "287082"


def test_totp_bad_seed_is_empty_not_crash():
    assert totp_code("not base32!!!", at_unix=59) == ""
    assert totp_code("", at_unix=59) == ""


def test_mfaconfig_current_code():
    assert MfaConfig(kind="otp", otp="123456").current_code() == "123456"
    assert MfaConfig(kind="totp", seed=_RFC_SEED).current_code(at_unix=59) == "287082"


def test_mfaconfig_from_payload():
    assert MfaConfig.from_payload({"kind": "otp", "otp": "123456"}).otp == "123456"
    assert MfaConfig.from_payload({"seed": _RFC_SEED}).kind == "totp"  # inferred
    assert MfaConfig.from_payload({"otp": "999"}).kind == "otp"        # inferred
    assert MfaConfig.from_payload(None) is None
    assert MfaConfig.from_payload({"kind": "totp"}) is None            # no seed → invalid


def test_credentials_backward_compat_and_mfa():
    c = Credentials.from_payload({"username": "u", "password": "p"})
    assert c.mfa is None and c.username == "u"
    c2 = Credentials.from_payload({"username": "u", "password": "p",
                                   "mfa": {"kind": "otp", "otp": "123456", "delivery": "email"}})
    assert c2.mfa.otp == "123456" and c2.mfa.delivery == "email"


# ─── field matchers ─────────────────────────────────────────────────────────

def _inv(raws):
    return build_inventory(raws, _REFUSE, url="https://app/x")


def test_match_otp_control_finds_code_field_not_password():
    controls = _inv([_user(), _pass(), _otp("One-time passcode")])
    hit = match_otp_control(controls)
    assert hit is not None and "passcode" in hit["name"].lower()
    # a plain login screen has no OTP field
    assert match_otp_control(_inv([_user(), _pass()])) is None


def test_match_delivery_only_when_named():
    controls = _inv([_radio("Email"), _radio("Text message"), _btn("Continue")])
    assert match_delivery_control(controls, delivery="email")["name"].lower() == "email"
    assert match_delivery_control(controls, delivery="text")["name"].lower() == "text message"
    # unset delivery → never guesses
    assert match_delivery_control(controls, delivery="") is None


# ─── login flows ────────────────────────────────────────────────────────────

_CREDS = Credentials.from_payload({"username": "member", "password": "secret"})


def test_login_single_step_backward_compat():
    pages = {
        "https://app/login": {"controls": [_user(), _pass(), _btn("Sign in")],
                              "nav": {"Sign in": "https://app/home"}},
        "https://app/home": {"controls": [_btn("Log out")]},
    }
    res = _login(pages, "https://app/login", _CREDS)
    assert res.success, res.reason
    assert res.storage_state is not None  # session captured on success


def test_login_multi_step_username_then_password():
    pages = {
        "https://app/login": {"controls": [_user(), _btn("Next")],
                              "nav": {"Next": "https://app/pw"}},
        "https://app/pw": {"controls": [_pass(), _btn("Sign in")],
                           "nav": {"Sign in": "https://app/home"}},
        "https://app/home": {"controls": [_btn("Log out")]},
    }
    res = _login(pages, "https://app/login", _CREDS)
    assert res.success, res.reason


def test_login_mfa_fixed_otp():
    creds = Credentials.from_payload({"username": "member", "password": "secret",
                                      "mfa": {"kind": "otp", "otp": "123456"}})
    pages = {
        "https://app/login": {"controls": [_user(), _pass(), _btn("Sign in")],
                              "nav": {"Sign in": "https://app/mfa"}},
        "https://app/mfa": {"controls": [_otp("Verification code"), _btn("Verify")],
                            "nav": {"Verify": "https://app/home"}},
        "https://app/home": {"controls": [_btn("Log out")]},
    }
    res = _login(pages, "https://app/login", creds)
    assert res.success, res.reason


def test_login_mfa_totp():
    creds = Credentials.from_payload({"username": "member", "password": "secret",
                                      "mfa": {"kind": "totp", "seed": _RFC_SEED}})
    pages = {
        "https://app/login": {"controls": [_user(), _pass(), _btn("Sign in")],
                              "nav": {"Sign in": "https://app/mfa"}},
        "https://app/mfa": {"controls": [_otp("Authenticator code"), _btn("Verify")],
                            "nav": {"Verify": "https://app/home"}},
        "https://app/home": {"controls": [_btn("Log out")]},
    }
    res = _login(pages, "https://app/login", creds)
    assert res.success, res.reason


def test_login_mfa_with_delivery_choice_completes_not_early():
    # username+password → "how do you want your code?" (email/mobile) → OTP → home.
    # The delivery screen has no password/OTP field, so an eager success check would
    # green-wash a half-finished login. Assert it drives all the way THROUGH to home.
    creds = Credentials.from_payload({"username": "member", "password": "secret",
                                      "mfa": {"kind": "otp", "otp": "123456", "delivery": "email"}})
    pages = {
        "https://app/login": {"controls": [_user(), _pass(), _btn("Sign in")],
                              "nav": {"Sign in": "https://app/deliver"}},
        "https://app/deliver": {"controls": [_radio("Email"), _radio("Text message"), _btn("Continue")],
                                "nav": {"Continue": "https://app/otp"}},
        "https://app/otp": {"controls": [_otp("Verification code"), _btn("Verify")],
                            "nav": {"Verify": "https://app/home"}},
        "https://app/home": {"controls": [_btn("Log out")]},
    }
    a, port = _mk_auth(pages, "https://app/login", creds)
    res = asyncio.run(a.login(_observe(port)))
    assert res.success, res.reason
    assert asyncio.run(port.current_url()) == "https://app/home"  # not stopped at /deliver


def test_login_behind_signin_affordance():
    pages = {
        "https://app/": {"controls": [_raw("link", "Sign in", tag="a")],
                         "nav": {"Sign in": "https://app/login"}},
        "https://app/login": {"controls": [_user(), _pass(), _btn("Log in")],
                              "nav": {"Log in": "https://app/home"}},
        "https://app/home": {"controls": [_btn("Log out")]},
    }
    res = _login(pages, "https://app/", _CREDS)
    assert res.success, res.reason


def test_login_fails_on_error_region_never_greenwashes():
    pages = {
        "https://app/login": {"controls": [_user(), _pass(), _btn("Sign in")],
                              "nav": {"Sign in": "https://app/login_err"}},
        "https://app/login_err": {"controls": [_user(), _pass(), _btn("Sign in")],
                                  "errors": ["Invalid username or password"]},
    }
    res = _login(pages, "https://app/login", _CREDS)
    assert not res.success
    assert "login_failed" in res.reason


def test_login_unmatched_is_honest_failure():
    pages = {"https://app/x": {"controls": [_btn("Somewhere")]}}
    res = _login(pages, "https://app/x", _CREDS)
    assert not res.success and "login_unverified" in res.reason


# ─── uncommitted fills are never recorded (the live Age-spinbutton incident) ────


class _NumberRejectingBrowser(FakeBrowser):
    """Playwright semantics for the live incident: ``fill()`` on an
    ``input[type=number]`` with non-numeric text ERRORS and commits nothing."""

    async def fill(self, control, value):
        if (control.get("input_type") or "").lower() == "number":
            return RawObservation(
                url_before=self._cur, url_after=self._cur, committed_value=None,
                error_detail="action_error: Locator.fill: Cannot type text into input[type=number]",
            )
        return await super().fill(control, value)


def test_uncommitted_username_fill_is_not_recorded_and_not_a_secret_submit():
    """The exact client scenario: a PUBLIC quote page (no login form) whose first
    text-like field is a number input ('Age'). The username heuristic grabs it, the
    fill errors — the manifest must NOT carry a valueless 'type' action (that record
    is what refused the whole crawl), login fails honestly, and ``secret_submitted``
    stays False so the crawler explores the page unauthenticated (Fix #2)."""
    pages = {"https://app/quote": {"controls": [
        _raw("textbox", "Age", input_type="number", kind="number"),
        _raw("combobox", "Product", input_type="select-one", tag="select", kind="select",
             options=["VKPower Term", "Heritage Whole Life"]),
        _btn("Get my quote"),
    ]}}
    port = _NumberRejectingBrowser(pages, "https://app/quote")
    a = Authenticator(port, _CREDS, FakeClock(), _REFUSE,
                      AuthWindow(max_requests=50, window_ms=10 ** 9), max_relogins=1)
    res = asyncio.run(a.login(_observe(port)))

    assert not res.success                       # honest failure, never a fake login
    assert res.secret_submitted is False         # no password/OTP went anywhere
    dicts = [dataclasses.asdict(rec) for rec in res.actions]
    valueless_fills = [d for d in dicts
                       if d.get("verb") in ("type", "select") and d.get("value") in (None, "")]
    assert valueless_fills == []                 # the refusal-causing record never exists


# ─── U6: passwordless / alternate-identifier logins ─────────────────────────────

def test_credentials_passwordless_member_pin():
    c = Credentials.from_payload({"member_number": "1234567", "pin": "4321"})
    assert c is not None and c.username == "1234567" and c.password == "4321"


def test_credentials_email_identifier_and_secret_aliases():
    c = Credentials.from_payload({"email": "a@b.test", "passcode": "9999"})
    assert c.username == "a@b.test" and c.password == "9999"


def test_credentials_mfa_allows_empty_secret():
    c = Credentials.from_payload({"username": "u", "mfa": {"otp": "123456"}})
    assert c is not None and c.password == "" and c.mfa is not None


def test_credentials_passwordless_flag_allows_empty_secret():
    c = Credentials.from_payload({"member": "m1", "passwordless": True})
    assert c is not None and c.password == ""


def test_credentials_identifier_without_secret_or_mfa_is_refused():
    assert Credentials.from_payload({"username": "u"}) is None
    assert Credentials.from_payload({"member_number": "1"}) is None


def test_credentials_no_identifier_is_refused():
    assert Credentials.from_payload({"password": "p"}) is None
    assert Credentials.from_payload({}) is None
    assert Credentials.from_payload(None) is None


# ─── U6: passwordless member#+PIN matched on a single screen ────────────────────

def test_match_login_controls_accepts_pin_as_secret_when_no_password_field():
    controls = _inv([_user("Member Number"),
                     _raw("textbox", "PIN", input_type="text"),
                     _btn("Sign in")])
    lc = match_login_controls(controls)
    assert lc is not None
    assert "member" in lc.username["name"].lower()
    assert "pin" in lc.password["name"].lower()      # PIN serves as the secret


def test_match_login_controls_real_password_still_wins():
    lc = match_login_controls(_inv([_user(), _pass(), _btn("Sign in")]))
    assert lc is not None and lc.password["name"].lower() == "password"


def test_match_login_controls_none_without_password_or_pin():
    # an identifier + Continue only (no secret on this screen) → not a single-screen
    # login match; the multi-screen Authenticator.login handles that path.
    assert match_login_controls(_inv([_user("Member Number"), _btn("Continue")])) is None


# ─── A16 · a login that answers LATE, and a password typed once ─────────────
#
# Both defects were found by crawling summit-life-carrier, whose sign-in awaits
# 1200ms before revealing its MFA step. Neither was reachable from any existing
# fake, because every fake here answers a click synchronously.


class LateAnsweringBrowser(FakeBrowser):
    """A login whose submit handler answers on a TIMER, not on the click.

    Models the real shape exactly: the click returns immediately, the screen
    goes BUSY (the submit button is replaced by a disabled "Authenticating..."),
    and the next step appears only on a later observation. `busy_reads` is how
    many observations the crawl gets before the application has answered.
    """

    def __init__(self, pages: dict, start: str, *, busy_reads: int = 2) -> None:
        super().__init__(pages, start)
        self._busy_left = 0
        self._busy_reads = busy_reads
        self._pending: str | None = None
        self.fills: list[str] = []

    async def collect_controls(self):
        if self._busy_left > 0:
            self._busy_left -= 1
            if self._busy_left == 0 and self._pending:
                self._cur, self._pending = self._pending, None
            # The screen the application shows while it is working: the form is
            # still there, and its submit is DISABLED. That disabled control is
            # the only honest signal that no answer has arrived yet.
            return [dict(_user()), dict(_pass()),
                    dict(_btn("Authenticating..."), disabled=True)]
        return await super().collect_controls()

    async def click(self, control):
        dest = self._p().get("nav", {}).get(control.get("name"))
        before = self._cur
        if dest:
            self._pending = dest
            self._busy_left = self._busy_reads
            return RawObservation(url_before=before, url_after=before)
        return RawObservation(url_before=before, url_after=before)

    async def fill(self, control, value):
        self.fills.append(str(control.get("name") or ""))
        return await super().fill(control, value)


_LATE_PAGES = {
    "https://app/login": {
        "controls": [_user(), _pass(), _btn("Continue")],
        "errors": [], "nav": {"Continue": "https://app/mfa"},
    },
    "https://app/mfa": {
        "controls": [_user(), _pass(), _otp("MFA Verification Code"),
                     _btn("Verify & Sign In")],
        "errors": [], "nav": {"Verify & Sign In": "https://app/home"},
    },
    "https://app/home": {"controls": [_btn("Sign out")], "errors": [], "nav": {}},
}


def test_a_login_that_answers_late_is_not_declared_stuck():
    """THE A16 DEFECT. Read once, a busy screen is indistinguishable from a form
    that refused to advance — and the loop abandoned the login on that reading.

    Measured before the fix: summit-life-carrier reported stop_reason=auth_failed
    with states=1 on an application it was 1200ms from being signed into."""
    creds = Credentials.from_payload(
        {"username": "u", "password": "p", "mfa": {"kind": "otp", "otp": "123456"}})
    port = LateAnsweringBrowser(_LATE_PAGES, "https://app/login", busy_reads=2)
    a = Authenticator(port, creds, FakeClock(), _REFUSE,
                      AuthWindow(max_requests=50, window_ms=10 ** 9), max_relogins=1)
    a.LATE_ADVANCE_WAIT_MS = 30  # the LOOP is under test, not the duration
    result = asyncio.run(a.login(_observe(port)))
    assert result.success, (
        "a login that answered late was reported as failed: %s" % result.reason)


def test_a_busy_screen_is_not_read_as_an_advance():
    """The FIRST attempted fix moved on as soon as the fingerprint changed, and
    a busy screen changes it — "Continue" becomes "Authenticating...". That is
    the application saying it has NOT answered, so it must not count as one."""
    creds = Credentials.from_payload(
        {"username": "u", "password": "p", "mfa": {"kind": "otp", "otp": "123456"}})
    # Four busy reads: far more than any fingerprint-only rule would survive.
    port = LateAnsweringBrowser(_LATE_PAGES, "https://app/login", busy_reads=3)
    a = Authenticator(port, creds, FakeClock(), _REFUSE,
                      AuthWindow(max_requests=50, window_ms=10 ** 9), max_relogins=1)
    a.LATE_ADVANCE_WAIT_MS = 30
    assert asyncio.run(a.login(_observe(port))).success


def test_a_committed_password_is_never_retyped():
    """A password is typed ONCE per login sequence — the guard the username
    branch always had.

    Without it the loop re-derives a password control from a screen the browser
    is leaving and re-types the secret into it. Measured on summit-life-carrier:
    the fill spent a full 30s action timeout, returned committed_value=None from
    /dashboard/overview, and a login that had ALREADY SUCCEEDED was reported as
    an uncommitted credential."""
    creds = Credentials.from_payload(
        {"username": "u", "password": "p", "mfa": {"kind": "otp", "otp": "123456"}})
    port = LateAnsweringBrowser(_LATE_PAGES, "https://app/login", busy_reads=2)
    a = Authenticator(port, creds, FakeClock(), _REFUSE,
                      AuthWindow(max_requests=50, window_ms=10 ** 9), max_relogins=1)
    a.LATE_ADVANCE_WAIT_MS = 30
    asyncio.run(a.login(_observe(port)))
    assert port.fills.count("Password") == 1, (
        "the password was typed %d times; every repeat is a secret written into "
        "a screen the login had already left: %s"
        % (port.fills.count("Password"), port.fills))
