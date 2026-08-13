"""GROUNDED NAVIGATION ON A pushState SPA — why a crawl captured every page and
still could not produce one end-to-end test.

Live evidence from an admin console, AFTER every traversal fix was in place:

    16 pages captured
    "PROVED only 0 of the 15 navigations between them"
    generated: 11 per-navigation journeys, no coherent E2E

Every page was known. Nothing recorded how a user gets from one to the next, so a
single stitched flow would have wandered across unrelated screens and the
generator correctly refused to emit one.

The cause was not discovery — href-follow found the routes, which is why the
pages were catalogued at all. It was that the grounded [click -> navigation] edge
required the click-time classifier to report a browser navigation. A framework app
changes route by pushState and re-renders in place: no navigation event, no DOM
mutation on the clicked node, no dialog. The click reported nothing while the app
had demonstrably moved.

This is the same lesson ``_walk_wizard`` already learned and recorded in its own
comments — the new state IS the evidence, and the click-time outcome is
corroboration. The nav-grounding pass had never been given it.

WHAT MUST NOT BE TRADED AWAY: a grounded edge is an assertion that clicking THIS
control really reached THAT route. Reading the live URL after the click keeps that
assertion true; assuming it would fail the crawl's own honesty rule. The last two
tests pin that no edge is invented when nothing moved.
"""
from __future__ import annotations

import asyncio
import base64

from app.browser import BrowserPort, NavResult, RawObservation
from app.config import Settings
from app.crawler import Budget, Crawler, FrontierItem, GuardContext
from app.guard import load_refuse_pack

PNG_1x1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR4nGNgAAIAAAUAAen63NgAAAAASUVORK5CYII="
)
_REFUSE = load_refuse_pack(Settings().refuse_pack_path)

_HOME = "https://admin.example/dashboard/overview"
_CLAIMS = "https://admin.example/claims/reported"


def _link(name: str, href: str) -> dict:
    return {
        "role": "link", "name": name, "name_source": "content", "best_effort": False,
        "kind": "link", "tag": "a", "input_type": "", "options": [],
        "required": False, "disabled": False, "frame_selector": "", "testid": "",
        "css_hint": "", "value_committed": "", "href": href,
        "qec": {"href": href}, "landmark": {"role": "", "name": ""},
    }


class SpaNavBrowser(BrowserPort):
    """A framework admin console whose nav bar routes by pushState.

    ``click`` changes the live route but reports ``url_after == url_before``,
    because no browser navigation event fired — precisely what the real app does
    and what the grounding pass used to read as "nothing happened".
    """

    def __init__(self, *, soft: bool = True, dead: bool = False) -> None:
        self._cur = _HOME
        self.soft = soft      # pushState (no navigation event) vs a hard nav
        self.dead = dead      # a link that genuinely does nothing
        self.clicks: list[str] = []

    async def goto(self, url: str) -> NavResult:
        self._cur = url
        return NavResult(url=url, ok=True)

    async def current_url(self) -> str:
        return self._cur

    async def title(self) -> str:
        return "Admin"

    async def collect_controls(self):
        return [dict(_link("Claims", "/claims/reported"))]

    async def dialog_flags(self):
        return []

    async def error_texts(self):
        return []

    async def screenshot_png(self) -> bytes:
        return PNG_1x1

    async def click(self, control):
        before = self._cur
        self.clicks.append(str(control.get("name") or ""))
        if self.dead:
            return RawObservation(url_before=before, url_after=before)
        self._cur = _CLAIMS
        if self.soft:
            # pushState: the route changed, the click event saw nothing.
            return RawObservation(url_before=before, url_after=before)
        return RawObservation(url_before=before, url_after=_CLAIMS)

    async def fill(self, control, value):
        return RawObservation(url_before=self._cur, url_after=self._cur)

    async def select_option(self, control, value):
        return RawObservation(url_before=self._cur, url_after=self._cur)

    async def set_checked(self, control, checked):
        return RawObservation(url_before=self._cur, url_after=self._cur)

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


async def _no_sleep(_s: float) -> None:
    return None


def _crawler(port, work_dir) -> Crawler:
    return Crawler(
        port, crawl_id="c1", tenant_id="t1", target_url=_HOME,
        work_dir=str(work_dir), refuse_pack=_REFUSE,
        budget=Budget(rate_per_s=0, max_states=6, max_depth=4),
        explorer_version="test/1.0", guard_version="test",
        refuse_pack_version=_REFUSE.version, config_fingerprint="fp",
        guard_context=GuardContext(refuse_pack=_REFUSE), sleep=_no_sleep,
    )


def _ground(crawler, port):
    controls = asyncio.run(port.collect_controls())
    return asyncio.run(crawler._ground_nav_links(
        FrontierItem(url=_HOME, depth=0), controls,
        "fp-home", 4,
    ))


# ── the defect ──────────────────────────────────────────────────────────────

def test_a_pushstate_nav_is_grounded_as_a_real_navigation(tmp_path):
    """THE FIX. Clicking "Claims" moved the app to /claims/reported. That is a
    navigation a user performed by clicking a control, which is exactly what a
    grounded edge asserts — the absence of a browser navigation EVENT does not
    make it less true."""
    port = SpaNavBrowser(soft=True)
    recorded = _ground(_crawler(port, tmp_path), port)

    assert recorded, "a pushState route change recorded no grounded navigation"
    action = recorded[0]
    assert action.to_state, "the grounded edge has no destination"
    assert "/claims/reported" in str(action.to_state)


def test_the_soft_navigation_is_labelled_as_such(tmp_path):
    """A grounded edge must stay auditable: a pushState navigation is recorded as
    a navigation AND says how it was detected, so it is never silently
    indistinguishable from a hard browser navigation."""
    port = SpaNavBrowser(soft=True)
    action = _ground(_crawler(port, tmp_path), port)[0]
    assert action.after.get("navigated") is True
    assert action.after.get("navigation_kind") == "pushstate"


def test_a_hard_navigation_is_unchanged(tmp_path):
    """REGRESSION GUARD: a multi-page app that fires a real navigation event is
    grounded exactly as before, and is NOT relabelled as a soft nav."""
    port = SpaNavBrowser(soft=False)
    action = _ground(_crawler(port, tmp_path), port)[0]
    assert action.to_state and "/claims/reported" in str(action.to_state)
    assert action.after.get("navigation_kind") != "pushstate"


# ── the honesty rule: never invent an edge ──────────────────────────────────

def test_a_click_that_moves_nothing_grounds_nothing(tmp_path):
    """THE LINE THIS MUST NOT CROSS. A grounded edge asserts that clicking this
    control reached that route. A dead link reached nothing, and recording an
    edge for it would put a navigation into the evidence that never happened —
    the generator would then stitch a journey through a step no user can take."""
    port = SpaNavBrowser(dead=True)
    assert _ground(_crawler(port, tmp_path), port) == []


def test_the_destination_is_read_from_the_page_not_assumed_from_the_href(tmp_path):
    """The href says where a link INTENDS to go; only the page says where it
    WENT. An app that redirects, guards a route, or bounces to a sign-in must
    ground the URL actually reached — otherwise a blocked route would be recorded
    as a working navigation."""
    port = SpaNavBrowser(soft=True)
    port._redirect = True

    async def click(control):           # the app sends the click somewhere else
        port.clicks.append(str(control.get("name") or ""))
        port._cur = "https://admin.example/claims/access-denied"
        return RawObservation(url_before=_HOME, url_after=_HOME)

    port.click = click
    action = _ground(_crawler(port, tmp_path), port)[0]
    assert "access-denied" in str(action.to_state)
