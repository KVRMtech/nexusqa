"""Pure-logic tests for the generalized authenticator (auth.py).

No browser, no network: the login state machine is driven through a scripted
:class:`FakeBrowser` (BrowserPort), proving single-step, multi-step, and MFA
(fixed-OTP + TOTP) logins + honest failure — the same harness pattern as
test_crawler_logic. Also pins the stdlib RFC-6238 TOTP against the spec vector.
"""
from __future__ import annotations

import asyncio
import base64

from app import auth
from app.auth import (
    Authenticator,
    AuthWindow,
    Credentials,
    MfaConfig,
    match_delivery_control,
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
