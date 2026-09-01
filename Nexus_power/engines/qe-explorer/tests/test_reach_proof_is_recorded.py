"""A LOG LINE IS NOT EVIDENCE — the proof must reach the manifest.

``_reach_in_app`` clicks an in-app link to reach a route, confirms it landed on
the right one, confirms it did not bounce back to the sign-in wall, and logged:

    qec.crawler.reached_in_app via='Reported Claims'
      — navigated by clicking, so the login survived and the link is PROVEN

It then discarded the click observation and never built an action record. Nothing
downstream ever saw the navigation, so the generator counted it as an unproven
page-jump and refused to build an end-to-end test:

    "the crawl stitched 16 pages together but PROVED only 0 of the 15 navigations"

Every route on that app had in fact been reached by clicking. The proof existed
and was thrown away one line after being asserted — a claim of evidence with no
evidence behind it, in the instrument of a product whose entire premise is that
it never does that.

The generator's bar (test_factory ``_navigation_backbone``) is an ACTION carrying
a real navigation outcome. These tests hold this path to that bar, and to the
honesty rule that goes with it: a navigation that did not happen must still
record nothing.
"""
from __future__ import annotations

import asyncio
import base64

from app.browser import BrowserPort, NavResult, RawObservation
from app.config import Settings
from app.crawler import Budget, Crawler, GuardContext
from subsystem_source import crawler_subsystem_source
from app.guard import load_refuse_pack

PNG_1x1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR4nGNgAAIAAAUAAen63NgAAAAASUVORK5CYII="
)
_REFUSE = load_refuse_pack(Settings().refuse_pack_path)

_LANDING = "https://admin.example/dashboard/overview"
_CLAIMS = "https://admin.example/claims/reported"


def _link(name: str, href: str) -> dict:
    return {
        "role": "link", "name": name, "name_source": "content", "best_effort": False,
        "kind": "link", "tag": "a", "input_type": "", "options": [],
        "required": False, "disabled": False, "frame_selector": "", "testid": "",
        "css_hint": "", "value_committed": "", "href": href,
        "qec": {"href": href}, "landmark": {"role": "", "name": ""},
    }


class ClickToReachBrowser(BrowserPort):
    """An admin console that routes by pushState (the click event reports nothing)
    and, in the ``lands_elsewhere`` variant, does not go where the link claims."""

    def __init__(self, *, lands_elsewhere: bool = False) -> None:
        self._cur = _LANDING
        self.lands_elsewhere = lands_elsewhere

    async def goto(self, url: str) -> NavResult:
        self._cur = url
        return NavResult(url=url, ok=True)

    async def current_url(self) -> str:
        return self._cur

    async def title(self) -> str:
        return "Admin"

    async def collect_controls(self):
        return [dict(_link("Reported Claims", "/claims/reported"))]

    async def dialog_flags(self):
        return []

    async def error_texts(self):
        return []

    async def screenshot_png(self) -> bytes:
        return PNG_1x1

    async def click(self, control):
        before = self._cur
        self._cur = (_LANDING + "/somewhere-else" if self.lands_elsewhere else _CLAIMS)
        # pushState: the click event sees no navigation at all.
        return RawObservation(url_before=before, url_after=before)

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


def _crawler(port, work_dir) -> Crawler:
    return Crawler(
        port, crawl_id="c1", tenant_id="t1", target_url=_LANDING,
        work_dir=str(work_dir), refuse_pack=_REFUSE,
        budget=Budget(rate_per_s=0, max_states=6, max_depth=4),
        explorer_version="test/1.0", guard_version="test",
        refuse_pack_version=_REFUSE.version, config_fingerprint="fp",
        guard_context=GuardContext(refuse_pack=_REFUSE),
    )


def _reach(crawler, port, url=_CLAIMS, via="Reported Claims"):
    controls = asyncio.run(port.collect_controls())
    return asyncio.run(crawler._reach_in_app(controls, url, via))


# ── the proof is recorded, not merely logged ────────────────────────────────

def test_reaching_a_route_by_clicking_records_a_grounded_action(tmp_path):
    """THE FIX. Without this record the navigation is invisible downstream, and
    a crawl that reached every page by clicking still reports zero proven
    navigations."""
    port = ClickToReachBrowser()
    crawler = _crawler(port, tmp_path)
    assert _reach(crawler, port) is not None

    assert crawler._pending_reach_actions, (
        "the click was proven and then discarded — nothing downstream can see it")
    action = crawler._pending_reach_actions[0]
    assert action.to_state, "a grounded navigation must name where it went"
    assert "/claims/reported" in str(action.to_state)


def test_the_action_carries_a_navigation_outcome(tmp_path):
    """The generator's bar is a real navigation outcome; a pushState route change
    reports none at click time, so it is asserted from the route we PROVED we
    landed on — and labelled, so it stays distinguishable from a hard nav."""
    port = ClickToReachBrowser()
    crawler = _crawler(port, tmp_path)
    _reach(crawler, port)

    action = crawler._pending_reach_actions[0]
    after = action.after or {}
    assert after.get("navigated") is True
    assert after.get("outcome") == "navigation"
    assert set(after) <= {"outcome", "detail", "navigated"}, (
        "after is a STRICT mirrored contract (AfterBundle, extra=forbid); a "
        "foreign key here makes the writer refuse the whole crawl — proven live")
    assert action.qec.get("navigation_kind") == "pushstate"


def test_the_click_that_was_actually_performed_is_the_one_recorded(tmp_path):
    port = ClickToReachBrowser()
    crawler = _crawler(port, tmp_path)
    _reach(crawler, port)
    action = crawler._pending_reach_actions[0]
    assert "Reported Claims" in str(action.target_label or "")
    assert action.verb == "click"


# ── the honesty rule: a navigation that did not happen records nothing ──────

def test_landing_somewhere_else_records_no_proof(tmp_path):
    """THE LINE THIS MUST NOT CROSS. The method already refuses to RETURN a hop
    that missed its target; it must equally refuse to leave a grounded action
    behind. Recording one would put a navigation in the evidence that never
    happened, and the generator would stitch a journey through a step no user can
    take."""
    port = ClickToReachBrowser(lands_elsewhere=True)
    crawler = _crawler(port, tmp_path)

    assert _reach(crawler, port) is None
    assert crawler._pending_reach_actions == []


def test_a_grounded_navigation_EDGE_is_staged_not_just_an_action(tmp_path):
    """An action says a control was clicked. The journey graph is built from
    state-to-state EDGES, so without one the click is only an interaction that
    happened to be followed by a page — which is how eight recorded proofs still
    produced "PROVED 0 of 15"."""
    port = ClickToReachBrowser()
    crawler = _crawler(port, tmp_path)
    _reach(crawler, port)

    assert crawler._pending_reach_edge is not None, (
        "the navigation was recorded as an action but no edge — nothing joins the "
        "two states, so no journey can be built from it")
    source_fp, label = crawler._pending_reach_edge
    assert source_fp, "an edge needs a real source state, not an empty one"
    assert "Reported Claims" in label


def test_a_failed_hop_stages_no_edge(tmp_path):
    port = ClickToReachBrowser(lands_elsewhere=True)
    crawler = _crawler(port, tmp_path)
    assert _reach(crawler, port) is None
    assert crawler._pending_reach_edge is None


def test_no_matching_link_records_nothing(tmp_path):
    port = ClickToReachBrowser()
    crawler = _crawler(port, tmp_path)
    assert _reach(crawler, port, url="https://admin.example/actuarial/pricing",
                  via="Nothing Here") is None
    assert crawler._pending_reach_actions == []


# ── the tripwire for the whole class of bug ─────────────────────────────────

def test_no_code_path_claims_PROVEN_without_recording_an_action():
    """THE CLASS OF DEFECT, not just this instance.

    A log line that asserts proof is a claim about evidence. Any site that makes
    that claim must also produce the record — otherwise the crawler is doing
    precisely what the product exists to stop an application from doing: saying a
    thing is verified when nothing verified it.

    Scans LOGGER CALLS (not comments or docstrings — an explanation may discuss
    the word freely) and requires each one that asserts PROVEN to build an action
    record nearby. Crude on purpose: it fails loudly when a new claim appears
    without its evidence, which is the moment to think about it.
    """
    source = crawler_subsystem_source()  # M0.3: subsystem, not one file

    def _statement(start: int) -> str:
        """Just this log call — a multi-line message, and nothing after it.

        Bounding only at the NEXT logger call is far too greedy: it swallows
        everything in between, including the next function's docstring, so a
        docstring that merely DISCUSSES proof is read as a log asserting it.
        """
        stops = [source.find(m, start + 7)
                 for m in ("logger.", "\n\n", "\n    def ", "\n    async def ")]
        ends = [s for s in stops if s != -1]
        return source[start:min(ends)] if ends else source[start:start + 400]

    calls = []
    at = source.find("logger.")
    while at != -1:
        calls.append((at, _statement(at)))
        at = source.find("logger.", at + 7)

    claiming = [(at, c) for at, c in calls if "PROVEN" in c]
    assert claiming, (
        "no log asserts PROVEN any more — if that claim was removed on purpose, "
        "update this tripwire deliberately rather than deleting it")

    for at, call in claiming:
        window = source[max(0, at - 2500):at + 500]
        assert "build_action_record" in window, (
            "a log asserts a navigation is PROVEN without an action record being "
            "built for it — the claim would reach a human while the evidence "
            "reaches nobody, which is exactly the failure this product exists to "
            f"prevent. Offending log: {call[:160]!r}")
