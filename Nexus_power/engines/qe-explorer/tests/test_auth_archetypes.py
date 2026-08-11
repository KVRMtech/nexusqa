"""AUTH ARCHETYPE MATRIX — the regression net for "1000 apps, not 1000 defects".

Every auth defect found in production so far was the same class of failure: an assumption
that held for the ONE app shape we had tested and was silently wrong for another. The
count of such defects scales with the number of distinct auth ARCHETYPES, not with the
number of customer apps — so this file enumerates the archetypes and pins the correct
behaviour for each. A new archetype is added HERE first; an app that shares an archetype
already listed is expected to work with no new code.

Archetypes covered (see also test_auth_required_no_credentials.py for the no-credentials
blocking rules):

  1. cookie session, single screen        — logs in, explores authenticated
  2. IN-MEMORY (client-side) auth         — login verified but dropped on every page
                                            load; must be DETECTED and continued IN
                                            PLACE, never reported as "session expired"
  3. username-first / multi-step          — email -> Next -> password
  4. passwordless member# + PIN           — the PIN is the PRIMARY secret
  5. MFA (identifier + secret + code)     — a computable second factor
  6. public app (no auth)                 — must never be treated as gated

Regression origin for #2: an admin SPA signed in successfully THREE times and never left
its sign-in page, while the product advised "the session has expired — re-record the
login". Both the behaviour and the advice were wrong; this pins both.
"""
from __future__ import annotations

import asyncio
import base64

import pytest

from app.auth import Credentials
from app.browser import BrowserPort, NavResult, RawObservation
from app.config import Settings
from app.crawler import AUTH_NOT_PERSISTED, Budget, Crawler, GuardContext
from app.guard import load_refuse_pack

PNG_1x1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR4nGNgAAIAAAUAAen63NgAAAAASUVORK5CYII="
)
_REFUSE = load_refuse_pack(Settings().refuse_pack_path)
_CREDS = Credentials.from_payload({"username": "member", "password": "secret12"})


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


def _user(name="Email"):
    return _raw("textbox", name, input_type="text")


def _pass(name="Password"):
    return _raw("textbox", name, input_type="password")


def _btn(name):
    return _raw("button", name, tag="button")


async def _no_sleep(_s: float) -> None:
    return None


class InMemoryAuthBrowser(BrowserPort):
    """ARCHETYPE 2 — an SPA that keeps the signed-in user in CLIENT-SIDE state.

    Signing in works and lands on the dashboard, but because nothing is stored in a
    cookie, EVERY fresh navigation drops the login and answers with the sign-in screen
    again. This is the shape that defeated the crawler in production: it re-logged in
    and then immediately navigated cold, throwing the login away each time.
    """

    def __init__(self, login_url: str, dashboard_url: str) -> None:
        self._login, self._dash = login_url, dashboard_url
        self._cur = login_url
        self._signed_in = False          # lives in the "page", not a cookie
        self.logins = 0

    async def goto(self, url: str) -> NavResult:
        # A fresh page load ALWAYS drops the in-memory session.
        self._signed_in = False
        self._cur = self._login
        return NavResult(url=self._login, ok=True)

    async def current_url(self) -> str:
        return self._cur

    async def title(self) -> str:
        return "Dashboard" if self._signed_in else "Sign in"

    async def collect_controls(self):
        if self._signed_in:
            return [dict(c) for c in (
                _raw("link", "Policies", tag="a"), _raw("link", "Claims", tag="a"),
                _btn("New application"),
            )]
        return [dict(c) for c in (_user(), _pass(), _btn("Sign in"))]

    async def dialog_flags(self):
        return []

    async def error_texts(self):
        return []

    async def screenshot_png(self) -> bytes:
        return PNG_1x1

    async def click(self, control):
        before = self._cur
        if not self._signed_in and str(control.get("name") or "") == "Sign in":
            self._signed_in = True       # in-memory only
            self._cur = self._dash
            self.logins += 1
            return RawObservation(url_before=before, url_after=self._dash)
        return RawObservation(url_before=before, url_after=before)

    async def fill(self, control, value):
        return RawObservation(url_before=self._cur, url_after=self._cur, committed_value=value)

    async def select_option(self, control, value):
        return RawObservation(url_before=self._cur, url_after=self._cur, committed_value=value)

    async def set_checked(self, control, checked):
        return RawObservation(url_before=self._cur, url_after=self._cur,
                              committed_value="true" if checked else "false")

    async def hover(self, control):
        return RawObservation(url_before=self._cur, url_after=self._cur)

    async def set_input_files(self, control, paths):
        return RawObservation(url_before=self._cur, url_after=self._cur)

    async def storage_state(self):
        return {"cookies": [], "origins": []}     # nothing to carry — the whole point

    async def materialize(self):
        return None

    async def drain_network(self):
        return []


def _crawler(port, work_dir, *, target_url, credentials=_CREDS):
    return Crawler(
        port, crawl_id="c1", tenant_id="t1", target_url=target_url,
        work_dir=str(work_dir), refuse_pack=_REFUSE,
        budget=Budget(rate_per_s=0, max_states=6),
        explorer_version="test/1.0", guard_version="test",
        refuse_pack_version=_REFUSE.version, config_fingerprint="fp",
        guard_context=GuardContext(refuse_pack=_REFUSE), sleep=_no_sleep,
        credentials=credentials,
    )


# ─── ARCHETYPE 2 — in-memory (client-side) auth ────────────────────────────────

def test_in_memory_auth_is_detected_and_never_called_an_expired_session(tmp_path):
    """The login VERIFIES every time, so "the session expired — re-record it" is
    provably the wrong diagnosis. It must be reported as auth-not-persisted."""
    port = InMemoryAuthBrowser("https://app.example/login", "https://app.example/dashboard")
    summary = asyncio.run(
        _crawler(port, tmp_path, target_url="https://app.example/dashboard").run())

    assert summary.coverage["auth_reason"] == AUTH_NOT_PERSISTED
    text = summary.coverage["summary"]
    assert "SIGNED IN successfully" in text
    # Re-recording may only be named as the thing that will NOT help — never as the
    # instruction (the loop that sent an operator round three identical recordings).
    assert "will not change this" in text.lower()


def test_in_memory_auth_still_signs_in_rather_than_giving_up(tmp_path):
    """The crawl must keep re-establishing the login (that is the only way into an app
    like this), not stop at the first wall."""
    port = InMemoryAuthBrowser("https://app.example/login", "https://app.example/dashboard")
    asyncio.run(_crawler(port, tmp_path, target_url="https://app.example/dashboard").run())
    assert port.logins >= 2, "the crawl signed in once and then gave up"


def test_in_memory_auth_reaches_the_signed_in_page(tmp_path):
    """The point of continuing IN PLACE: content that only exists behind the login must
    actually be OBSERVED, not merely reported about."""
    from app.emit import REC_PAGE_STATE, read_records

    port = InMemoryAuthBrowser("https://app.example/login", "https://app.example/dashboard")
    asyncio.run(_crawler(port, tmp_path, target_url="https://app.example/dashboard").run())

    states = [r for r in read_records(str(tmp_path), "c1") if r["type"] == REC_PAGE_STATE]
    seen = {str(s.get("url_path") or "") for s in states}
    assert "/dashboard" in seen, (
        f"never recorded the signed-in page; only saw {sorted(seen)}")


@pytest.mark.xfail(strict=True, reason=(
    "KNOWN GAP (open): on an auth-not-persisted app the crawl reaches the POST-LOGIN "
    "LANDING page and captures it, but never the DEEP page it actually asked for — a "
    "cold goto to /reports drops the in-memory login, and the re-login that repairs it "
    "lands on /dashboard instead. So every deep route collapses onto the landing page, "
    "which is why an app onboarded at a deep entry still reports its form uncaptured. "
    "Fix: after continuing in place, reach the requested route from INSIDE the "
    "signed-in page (click its in-app link / SPA navigation) rather than re-navigating "
    "cold. strict=True so this FAILS the moment it is fixed and cannot be forgotten."))
def test_in_memory_auth_reaches_the_DEEP_page_it_was_asked_for(tmp_path):
    """The live failure this pins: an admin app onboarded at a deep route reported
    'never reached this form' even though the crawl signed in successfully."""
    from app.emit import REC_PAGE_STATE, read_records

    port = InMemoryAuthBrowser("https://app.example/login", "https://app.example/dashboard")
    asyncio.run(_crawler(port, tmp_path, target_url="https://app.example/reports").run())

    states = [r for r in read_records(str(tmp_path), "c1") if r["type"] == REC_PAGE_STATE]
    seen = {str(s.get("url_path") or "") for s in states}
    assert "/reports" in seen, (
        f"never reached the requested deep page; only saw {sorted(seen)}")
