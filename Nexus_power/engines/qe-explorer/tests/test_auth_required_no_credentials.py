"""Honest STOP when the entry is behind a login wall and NO credentials were given.

The client scenario (app "bene", base URL /portal/beneficiaries): an app onboarded at
a GATED entry with NO credentials and NO session. The app redirects the entry to a
login wall the crawl cannot pass. Before this fix the crawl treated that login form as
a business form, filled synthetic data, clicked "Continue", was rejected, and LOOPED on
/login until the wall-clock budget expired — surfacing DISHONESTLY as
``budget_max_wall_ms`` and telling the operator to "re-crawl". Now it STOPS honestly
with ``stop_reason == STOP_AUTH_REQUIRED`` and ``coverage.auth_blocked``, so the app UI
can say "behind a login, no credentials — record a login".

Same FakeBrowser(BrowserPort) harness as test_crawler_logic / test_auth: no browser, no
network. The REDIRECT (requested != landed) to a FULL login form is the language-
agnostic gate signal — and the false-positive guards below pin that a directly-targeted
/login, a redirect to a mere "Sign in" link, and an injected session are all left alone.
"""
from __future__ import annotations

import asyncio
import base64

from app.browser import BrowserPort, NavResult, RawObservation
from app.config import Settings
from app.crawler import STOP_AUTH_REQUIRED, Budget, Crawler, GuardContext
from app.guard import load_refuse_pack

PNG_1x1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR4nGNgAAIAAAUAAen63NgAAAAASUVORK5CYII="
)
_REFUSE = load_refuse_pack(Settings().refuse_pack_path)


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


def _link(name):
    return _raw("link", name, tag="a")


async def _no_sleep(_seconds: float) -> None:
    return None


class FakeBrowser(BrowserPort):
    """Scripted pages keyed by URL, with optional REDIRECTS (goto X → lands on Y), so
    a gated entry can model the app bouncing us to its login wall. Same method set as
    the test_crawler_logic harness (BrowserPort is a Protocol — no strict ABC)."""

    def __init__(self, pages: dict, start: str, *, redirects: dict | None = None) -> None:
        self._pages = pages
        self._cur = start
        self._redirects = dict(redirects or {})

    def _page(self) -> dict:
        return self._pages.get(self._cur, {"controls": [], "errors": [], "nav": {}, "title": ""})

    async def goto(self, url: str) -> NavResult:
        landed = self._redirects.get(url, url)
        self._cur = landed
        return NavResult(url=landed, ok=landed in self._pages)

    async def current_url(self) -> str:
        return self._cur

    async def title(self) -> str:
        return self._page().get("title", "")

    async def collect_controls(self):
        return [dict(c) for c in self._page().get("controls", [])]

    async def dialog_flags(self):
        return []

    async def error_texts(self):
        return list(self._page().get("errors", []))

    async def screenshot_png(self) -> bytes:
        return PNG_1x1

    async def click(self, control):
        before = self._cur
        dest = self._page().get("nav", {}).get(control.get("name"))
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

    async def hover(self, control):
        return RawObservation(url_before=self._cur, url_after=self._cur)

    async def set_input_files(self, control, paths):
        return RawObservation(url_before=self._cur, url_after=self._cur)

    async def storage_state(self):
        return {"cookies": [], "origins": []}

    async def materialize(self):
        return None

    async def drain_network(self):
        return []


def _build_crawler(port, work_dir, *, target_url, credentials=None,
                   session_injected=False, budget=None):
    guard_ctx = GuardContext(refuse_pack=_REFUSE)
    return Crawler(
        port,
        crawl_id="c1", tenant_id="t1", target_url=target_url, work_dir=str(work_dir),
        refuse_pack=_REFUSE, budget=budget or Budget(rate_per_s=0, max_states=5),
        explorer_version="test/1.0", guard_version="test",
        refuse_pack_version=_REFUSE.version, config_fingerprint="fp",
        guard_context=guard_ctx, sleep=_no_sleep,
        credentials=credentials, session_injected=session_injected,
    )


_GATED = "https://app.example/portal/beneficiaries"
_LOGIN = "https://app.example/login"
_LOGIN_FORM = [_user(), _pass(), _btn("Sign in")]


def test_gated_entry_no_credentials_stops_honestly(tmp_path):
    """Entry redirects to a FULL login form and no credentials/session were given → the
    crawl STOPS with auth_required_no_credentials and marks coverage.auth_blocked,
    instead of looping the login form until the wall-clock budget expires."""
    pages = {_LOGIN: {"controls": _LOGIN_FORM, "title": "Sign in"}}
    port = FakeBrowser(pages, _GATED, redirects={_GATED: _LOGIN})
    summary = asyncio.run(_build_crawler(port, tmp_path, target_url=_GATED).run())

    assert summary.stop_reason == STOP_AUTH_REQUIRED
    cov = summary.coverage
    assert cov["auth_blocked"] is True
    assert cov["auth_blocked_reason"] == "no_credentials"
    # The LOUD, honest remediation — never "credentials were supplied but no form".
    assert cov["summary"].startswith("AUTHENTICATED AREAS NOT COVERED")
    assert "NO credentials" in cov["summary"]
    # The wizard never drove the login form (that was the loop) — no submits exercised.
    assert cov["forms_submitted"] == 0


def test_directly_targeted_login_page_is_not_hard_stopped(tmp_path):
    """A login page the operator points DIRECTLY at (requested == landed, no redirect)
    is explored as before — the hard-stop needs the REDIRECT signal, so a legitimate
    visit to /login is never mistaken for a gated business entry."""
    pages = {_LOGIN: {"controls": _LOGIN_FORM, "title": "Sign in"}}
    port = FakeBrowser(pages, _LOGIN)  # no redirect
    summary = asyncio.run(_build_crawler(port, tmp_path, target_url=_LOGIN).run())
    assert summary.stop_reason != STOP_AUTH_REQUIRED
    assert not summary.coverage["auth_blocked"]


def test_public_entry_redirect_without_a_full_login_form_is_not_stopped(tmp_path):
    """A redirect to a page with only a 'Sign in' LINK (no password field) is NOT a
    login wall — match_login_controls returns None — so the crawl explores it and the
    hard-stop does not fire."""
    landing = "https://app.example/welcome"
    pages = {landing: {"controls": [_link("Sign in"), _btn("Get a quote")], "title": "Welcome"}}
    port = FakeBrowser(pages, _GATED, redirects={_GATED: landing})
    summary = asyncio.run(_build_crawler(port, tmp_path, target_url=_GATED).run())
    assert summary.stop_reason != STOP_AUTH_REQUIRED
    assert not summary.coverage["auth_blocked"]


def test_injected_session_skips_the_no_credentials_gate(tmp_path):
    """With a session injected (no typed credentials), the no-credentials gate is
    skipped — the injected-session wall detector owns that case — so a redirect to a
    login wall does NOT trip the new hard-stop."""
    pages = {_LOGIN: {"controls": _LOGIN_FORM, "title": "Sign in"}}
    port = FakeBrowser(pages, _GATED, redirects={_GATED: _LOGIN})
    summary = asyncio.run(
        _build_crawler(port, tmp_path, target_url=_GATED, session_injected=True).run())
    assert summary.stop_reason != STOP_AUTH_REQUIRED


def test_content_rich_entry_with_inline_login_is_not_stopped(tmp_path):
    """An entry that redirects to a CONTENT-RICH page with an inline sign-in widget
    BESIDE a public funnel (age/zip inputs) is NOT a dedicated login wall — the crawl
    must explore the public content, not stop and mislabel a public app 'behind a
    login'. (Adversarial-review HIGH finding: only a dedicated sign-in screen is a wall.)"""
    landing = "https://app.example/home"
    controls = [
        _user(), _pass(), _btn("Sign in"),                 # an inline login widget …
        _raw("textbox", "Age", input_type="text"),         # … beside a public quote funnel
        _raw("textbox", "ZIP", input_type="text"),
        _btn("Get a quote"),
    ]
    pages = {landing: {"controls": controls, "title": "Home"}}
    port = FakeBrowser(pages, _GATED, redirects={_GATED: landing})
    summary = asyncio.run(_build_crawler(port, tmp_path, target_url=_GATED).run())
    assert summary.stop_reason != STOP_AUTH_REQUIRED
    assert not summary.coverage["auth_blocked"]


def test_checkbox_driven_funnel_entry_with_inline_login_is_not_stopped(tmp_path):
    """A public funnel whose OTHER fields are checkboxes/radios (not text) beside an
    inline login widget is still content-rich, not a dedicated wall — the dedicated
    check counts every form-field kind, so it must NOT stop. (Adversarial re-verify.)"""
    landing = "https://app.example/home"
    controls = [
        _user(), _pass(), _btn("Sign in"),
        _raw("checkbox", "Smoker"), _raw("checkbox", "Accept terms"),
        _btn("Get a quote"),
    ]
    pages = {landing: {"controls": controls, "title": "Home"}}
    port = FakeBrowser(pages, _GATED, redirects={_GATED: landing})
    summary = asyncio.run(_build_crawler(port, tmp_path, target_url=_GATED).run())
    assert summary.stop_reason != STOP_AUTH_REQUIRED
    assert not summary.coverage["auth_blocked"]


def test_dedicated_login_with_a_remember_me_checkbox_still_stops(tmp_path):
    """A real dedicated login often carries ONE extra control (a 'remember me'
    checkbox) — that must NOT disqualify it as a wall. One extra is allowed; two+
    business inputs is content-rich."""
    login_with_remember = [_user(), _pass(), _raw("checkbox", "Remember me"), _btn("Sign in")]
    pages = {_LOGIN: {"controls": login_with_remember, "title": "Sign in"}}
    port = FakeBrowser(pages, _GATED, redirects={_GATED: _LOGIN})
    summary = asyncio.run(_build_crawler(port, tmp_path, target_url=_GATED).run())
    assert summary.stop_reason == STOP_AUTH_REQUIRED
    assert summary.coverage["auth_blocked"] is True
