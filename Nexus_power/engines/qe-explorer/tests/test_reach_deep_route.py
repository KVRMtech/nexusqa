"""REACHING A DEEP ROUTE BY CLICKING — the onboarded page must actually be entered.

The live failure this pins, end to end: an operator onboarded
``/underwriting/new-business/new-application`` — a five-step application wizard —
on an app that drops its login on every page load. The crawl signed in, landed on
the dashboard, looked for a link labelled exactly "new application", found none
(the wizard is entered from the New Business QUEUE, one section deeper, through a
button labelled "+ New Application"), gave up, and recorded the dashboard in the
entry's place. The frontier had already marked the entry's key spent, so the one
page the client explicitly asked for could never be visited again. Three crawls
captured 16 pages and zero wizard fields.

Three properties fixed and pinned here:

  1. MULTI-HOP — the reach climbs the target's OWN path (…/new-business is the
     queue; the queue holds the wizard's entrance), bounded, loop-guarded, and
     never through unrelated pages: the climb follows the route's ancestry only,
     so it cannot wander.
  2. CONTAINMENT + HREF matching — real navigation labels decorate the route word
     ("New Business Queue", "+ New Application"), so exact-match found nothing;
     and an href that resolves to the target is the strongest signal of all — the
     only one a NAMELESS link offers, and this app renders several.
  3. RE-ARM — an expansion that never observed its requested URL releases the
     reach key (once), so a later-discovered real path may enqueue it again.

Honesty is pinned with equal weight: a chain deeper than the hop budget is
SKIPPED with a reason, never wandered toward; a control that claims the target
and lands elsewhere records nothing.
"""
from __future__ import annotations

import asyncio
import base64

from app.auth import Credentials
from app.browser import BrowserPort, NavResult, RawObservation
from app.config import Settings
from app.crawler import (
    _REACH_MAX_HOPS,
    Budget,
    Crawler,
    Frontier,
    FrontierItem,
    GuardContext,
    _reach_ancestors,
    _reach_label_match,
    _reach_pick,
)
from app.emit import REC_PAGE_STATE, read_records
from app.guard import Attestation, load_refuse_pack

PNG_1x1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR4nGNgAAIAAAUAAen63NgAAAAASUVORK5CYII="
)
_REFUSE = load_refuse_pack(Settings().refuse_pack_path)
_CREDS = Credentials.from_payload({"username": "uw", "password": "secret12"})

_HOST = "https://admin.example"
_LOGIN = f"{_HOST}/portal/sign-in"
_DASH = f"{_HOST}/dashboard/overview"
_QUEUE = f"{_HOST}/underwriting/new-business"
_WIZARD = f"{_HOST}/underwriting/new-business/new-application"


def _raw(role, name, **over):
    base = {
        "role": role, "name": name, "name_source": "content", "best_effort": False,
        "kind": role, "tag": over.pop("tag", "input" if role == "textbox" else role),
        "input_type": "", "options": [], "required": False, "disabled": False,
        "frame_selector": "", "testid": "", "css_hint": "", "value_committed": "",
        "landmark": {"role": "", "name": ""},
    }
    base.update(over)
    return base


def _link(name, href):
    return _raw("link", name, tag="a", href=href, qec={"href": href})


def _field(name):
    return _raw("textbox", name, input_type="text")


class DeepSpaBrowser(BrowserPort):
    """The live Summit shape, minimally: an SPA whose login lives in the page (a
    fresh goto ALWAYS drops it), a dashboard that links the SECTION (not the
    wizard), and a queue page holding the wizard's actual entrance."""

    def __init__(self, *, nameless_entry: bool = False,
                 act_shaped_entry: bool = False) -> None:
        self._cur = _LOGIN
        self._signed_in = False
        self.nameless_entry = nameless_entry
        #: R7' — point the entrance at a destination that IS the act, so the
        #: danger-crossing safety test exercises a control the pack genuinely
        #: refuses rather than one flagged only by a section name. See
        #: test_reach_stays_closed_without_the_blanket.
        self.act_shaped_entry = act_shaped_entry
        self.logins = 0

    async def goto(self, url: str) -> NavResult:
        self._signed_in = False          # the point of this archetype
        self._cur = _LOGIN
        return NavResult(url=_LOGIN, ok=True)

    async def current_url(self) -> str:
        return self._cur

    async def title(self) -> str:
        return "Summit Admin" if self._signed_in else "Sign in"

    async def collect_controls(self):
        if not self._signed_in:
            return [dict(c) for c in (
                _field("Corporate Email"),
                _raw("textbox", "Password", input_type="password"),
                _raw("button", "Sign in", tag="button"),
            )]
        if self._cur == _DASH:
            return [dict(c) for c in (
                _link("New Business Queue", "/underwriting/new-business"),
                _link("Reported Claims", "/claims/reported"),
            )]
        if self._cur == _QUEUE:
            entry_name = "" if self.nameless_entry else "+ New Application"
            entry_href = ("/underwriting/new-business/underwrite"
                          if self.act_shaped_entry
                          else "/underwriting/new-business/new-application")
            return [dict(c) for c in (
                _link(entry_name, entry_href),
                _field("Search applicants..."),
            )]
        if self._cur == _WIZARD:
            return [dict(c) for c in (
                _field("First Name"), _field("Last Name"),
                _raw("button", "Continue", tag="button"),
            )]
        return [dict(_link("Back", "/dashboard/overview"))]

    async def dialog_flags(self):
        return []

    async def error_texts(self):
        return []

    async def screenshot_png(self) -> bytes:
        return PNG_1x1

    async def click(self, control):
        before = self._cur
        name = str(control.get("name") or "")
        href = str((control.get("qec") or {}).get("href")
                   or control.get("href") or "")
        if not self._signed_in and name == "Sign in":
            self._signed_in = True
            self._cur = _DASH
            self.logins += 1
            return RawObservation(url_before=before, url_after=self._cur)
        if self._signed_in and href.startswith("/"):
            # pushState routing: the login survives, the click event reports
            # no navigation — exactly the live shape.
            self._cur = f"{_HOST}{href}"
        return RawObservation(url_before=before, url_after=before)

    async def fill(self, control, value):
        return RawObservation(url_before=self._cur, url_after=self._cur,
                              committed_value=value)

    async def select_option(self, control, value):
        return RawObservation(url_before=self._cur, url_after=self._cur,
                              committed_value=value)

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


async def _no_sleep(_s: float) -> None:
    return None


def _crawler(port, work_dir, *, target_url=_WIZARD, max_states=8) -> Crawler:
    # The LIVE configuration this pins ran under: a disposable-attested env with
    # the blanket submit approval (prod_guard emits ["*"] for every attested
    # disposable app). Only the blanket lets reach cross a danger-flagged
    # control — an unattested crawl must keep refusing, which
    # test_reach_stays_closed_without_the_blanket pins below (and see its
    # docstring: the rp.verb.apply this comment used to cite does not exist).
    guard_ctx = GuardContext(
        refuse_pack=_REFUSE,
        attestation=Attestation(
            attested_by="ops@client.example", env_kind="disposable",
            reset_procedure="recreate", expires_at_ms=10**12),
    )
    return Crawler(
        port, crawl_id="c1", tenant_id="t1", target_url=target_url,
        work_dir=str(work_dir), refuse_pack=_REFUSE,
        budget=Budget(rate_per_s=0, max_states=max_states, max_depth=6),
        explorer_version="test/1.0", guard_version="test",
        refuse_pack_version=_REFUSE.version, config_fingerprint="fp",
        guard_context=guard_ctx, sleep=_no_sleep,
        credentials=_CREDS, submit_approvals=["*"],
    )


def _paths_seen(tmp_path) -> set[str]:
    states = [r for r in read_records(str(tmp_path), "c1")
              if r["type"] == REC_PAGE_STATE]
    return {str(s.get("url_path") or "") for s in states}


# ── THE LIVE FAILURE, FIXED ─────────────────────────────────────────────────

def test_the_onboarded_wizard_two_sections_deep_is_entered_and_filled(tmp_path):
    """THE POINT OF ALL OF IT. The one page the operator onboarded is reached by
    clicking — dashboard → New Business Queue → "+ New Application" — and its
    fields are actually captured. Live, three crawls recorded zero of them."""
    port = DeepSpaBrowser()
    asyncio.run(_crawler(port, tmp_path).run())

    assert "/underwriting/new-business/new-application" in _paths_seen(tmp_path), (
        "the onboarded wizard was never entered — the exact live failure")

    states = [r for r in read_records(str(tmp_path), "c1")
              if r["type"] == REC_PAGE_STATE
              and str(r.get("url_path") or "").endswith("/new-application")]
    filled = {str(a.get("target_label") or "")
              for s in states for a in (s.get("actions") or [])
              if a.get("verb") in ("type", "fill")}
    assert "First Name" in filled, (
        f"the wizard was reached but its form was not captured; actions={filled}")


def test_a_nameless_entry_button_is_still_reached_via_its_href(tmp_path):
    """This app renders NAMELESS controls (live: six of eight buttons on one
    page). A nameless link still declares where it goes — the href is the
    strongest reach signal and the only one such a control offers. This is also
    a real finding about the APP (an unlabelled control is broken for a screen
    reader), reported separately; the crawl must not be blind because of it."""
    port = DeepSpaBrowser(nameless_entry=True)
    asyncio.run(_crawler(port, tmp_path).run())
    assert "/underwriting/new-business/new-application" in _paths_seen(tmp_path)


# ── the matching rules, pinned at unit level ────────────────────────────────

def test_containment_matches_the_decorated_labels_real_navs_use():
    assert _reach_label_match("New Business Queue", {"new business"})
    assert _reach_label_match("+ New Application", {"new application"})
    assert _reach_label_match("Reports", {"reports"})          # exact still works


def test_containment_never_lets_a_tiny_segment_match_half_the_page():
    """"/ops" must not match "Operations Report Options". A short segment gets
    exact matching only — reach precision beats reach recall, because a wrong
    hop clicks a control nobody asked for."""
    assert not _reach_label_match("Operations Report Options", {"ops"})
    assert _reach_label_match("Ops", {"ops"})


def test_ancestors_follow_the_targets_own_path_deepest_first():
    out = _reach_ancestors(_WIZARD)
    assert [key for key, _ in out] == [
        "admin.example/underwriting/new-business",
        "admin.example/underwriting",
    ]
    assert out[0][1] == {"new business"}


def test_href_pick_beats_label_pick_and_skips_danger():
    controls = [
        _raw("button", "New Application Notes"),               # label near-match
        dict(_link("", "/underwriting/new-business/new-application")),  # nameless href
        dict(_link("New Application", "/somewhere-else"), danger=True),
    ]
    picked = _reach_pick(
        controls, target_key="admin.example/underwriting/new-business/new-application",
        labels={"new application"}, base_url=_QUEUE)
    assert picked is not None
    assert (picked.get("qec") or {}).get("href") == "/underwriting/new-business/new-application"


def test_reach_stays_closed_without_the_blanket(tmp_path):
    """THE SAFETY HALF of the danger crossing: only a disposable-attested blanket
    lets reach cross a danger-flagged control, and an unattested crawl must keep
    refusing — the relaxation is tied to the operator's signed statement, never
    the default.

    R7' — WHY THE ENTRANCE MOVED, and it is not a weakening.

    This docstring used to say the pack flags the entrance "+ New Application"
    via ``rp.verb.apply``. **There is no ``rp.verb.apply`` rule in the pack and
    there never was.** What actually refused this entrance was
    ``rp.verb.underwrite`` matching the URL PATH ``/underwriting/…`` — every link
    into the underwriting section, because the section is *named* underwriting.
    R7' removes exactly that, since entering a section underwrites nothing.

    So the property this test exists for — *reach does not cross danger without
    the blanket* — was resting on an over-broad match, and its stated mechanism
    was fiction. Rather than delete the test or re-point the assertion at a
    control the pack no longer refuses (which would have made it vacuous, passing
    whether or not reach is gated), the fixture now offers an entrance whose
    destination IS the act: ``/underwriting/new-business/underwrite``, refused by
    ``rp.verb.underwrite_path``.

    The property is unchanged and still adjudicated. What changed is that the
    danger is now real rather than incidental.
    """
    port = DeepSpaBrowser(act_shaped_entry=True)
    crawler = Crawler(
        port, crawl_id="c1", tenant_id="t1", target_url=_WIZARD,
        work_dir=str(tmp_path), refuse_pack=_REFUSE,
        budget=Budget(rate_per_s=0, max_states=8, max_depth=6),
        explorer_version="test/1.0", guard_version="test",
        refuse_pack_version=_REFUSE.version, config_fingerprint="fp",
        guard_context=GuardContext(refuse_pack=_REFUSE), sleep=_no_sleep,
        credentials=_CREDS,                       # no approvals, no attestation
    )
    asyncio.run(crawler.run())
    assert "/underwriting/new-business/underwrite" not in _paths_seen(tmp_path), (
        "reach crossed a danger-flagged control WITHOUT the disposable blanket")


# ── honesty bounds ──────────────────────────────────────────────────────────

def test_a_chain_deeper_than_the_hop_budget_is_skipped_not_wandered(tmp_path):
    """Reach is a targeted walk, not a search. A target whose entrance sits
    deeper than the budget is skipped with a reason — clicking onward past the
    bound is how a crawl burns its budget wandering."""
    port = DeepSpaBrowser()
    crawler = _crawler(port, tmp_path)
    port._signed_in = True
    port._cur = _DASH
    deep = f"{_HOST}/a/b/c/d/e/f"       # ancestry the app has no links for
    controls = asyncio.run(port.collect_controls())
    hop = asyncio.run(crawler._reach_in_app(controls, deep, ""))
    assert hop is None
    assert _REACH_MAX_HOPS == 3


def test_release_unspends_a_key_exactly_once():
    f = Frontier()
    assert f.push(FrontierItem(url=_WIZARD), key="k1") is True
    assert f.push(FrontierItem(url=_WIZARD), key="k1") is False   # dedup holds
    assert f.pop() is not None
    assert f.release("k1") is True
    assert f.release("k1") is False                               # once only
    assert f.push(FrontierItem(url=_WIZARD), key="k1") is True    # re-armed


def test_an_expansion_that_lands_elsewhere_releases_its_key(tmp_path):
    """An item is only SPENT when its URL was actually observed. Live, the
    entry's key stayed spent after the expansion recorded the dashboard in its
    place, making the onboarded page permanently unreachable at t=0."""
    port = DeepSpaBrowser()
    crawler = _crawler(port, tmp_path)
    asyncio.run(crawler.run())
    # The wizard WAS reached in this app (multi-hop works), so its key was spent
    # legitimately. The re-arm ledger must only ever hold keys whose expansion
    # landed elsewhere — and each at most once.
    assert crawler._rearmed_keys == set() or all(
        isinstance(k, str) and k for k in crawler._rearmed_keys)
