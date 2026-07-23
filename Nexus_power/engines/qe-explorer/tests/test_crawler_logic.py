"""Pure-logic tests for the crawler state machine (design §7 item 5).

NO browser, NO network, NO Playwright: the crawler is driven through a scripted
:class:`FakeBrowser` implementing :class:`app.browser.BrowserPort`, so the
frontier / budget / after-classification / dedup / resume logic is proven here
and the live crawl is verified separately on the VM.  Also pins the shared
manifest↔``ExplorationBundle`` field-name contract against the REAL qe-central
schema (design §7 item 10 cross-subsystem fixtures).
"""
from __future__ import annotations

import asyncio
import base64
import dataclasses
import importlib.util
from pathlib import Path

from app import emit
from app.browser import (
    OUTCOME_DIALOG,
    OUTCOME_DOM_CHANGED,
    OUTCOME_ERROR,
    OUTCOME_NAVIGATION,
    OUTCOME_NONE,
    OUTCOME_VALUE_COMMITTED,
    BrowserPort,
    NavResult,
    RawObservation,
    classify_after,
)
from app.config import Settings
from app.auth import Credentials
from app.crawler import (
    STOP_AUTH_FAILED,
    STOP_COMPLETED,
    STOP_MAX_REQUESTS,
    STOP_MAX_STATES,
    STOP_MAX_WALL_MS,
    Budget,
    BudgetTracker,
    Crawler,
    Frontier,
    FrontierItem,
    GuardContext,
)
from app.emit import REC_PAGE_STATE, read_records
from app.guard import load_refuse_pack

# A minimal valid 1x1 PNG (schema/asset validation happens qe-central-side; the
# emitter only requires non-empty bytes).
PNG_1x1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR4nGNgAAIAAAUAAen63NgAAAAASUVORK5CYII="
)

_REFUSE_PACK = load_refuse_pack(Settings().refuse_pack_path)


# ─── Fakes ────────────────────────────────────────────────────────────────────


class FakeClock:
    """Deterministic clock for budget tests (duck-types MonotonicClock)."""

    def __init__(self) -> None:
        self.t = 0

    def now_ms(self) -> int:
        return self.t


def _raw(role: str, name: str, **over):
    base = {"role": role, "name": name, "name_source": "content", "best_effort": False,
            "kind": role, "tag": over.pop("tag", "a" if role == "link" else role),
            "input_type": "", "options": [], "required": False, "disabled": False,
            "frame_selector": "", "testid": "", "css_hint": "", "value_committed": "",
            "landmark": {"role": "", "name": ""}}
    base.update(over)
    return base


class FakePage:
    def __init__(self, url, controls, *, title="", click_targets=None, errors=()):
        self.url = url
        self.controls = controls
        self.title = title
        self.click_targets = dict(click_targets or {})
        self.errors = list(errors)


class FakeBrowser(BrowserPort):
    """Scripted BrowserPort over a fixed set of pages keyed by URL."""

    def __init__(self, pages: dict, start_url: str) -> None:
        self._pages = pages
        self._current = start_url
        self.uploads: list = []
        self.materialize_calls = 0
        # API/network mining — scripted per-URL raw network events + a drain count.
        self.network_by_url: dict = {}
        self.network_drains = 0

    def _page(self) -> FakePage:
        return self._pages.get(self._current) or FakePage(self._current, [])

    async def goto(self, url: str) -> NavResult:
        self._current = url
        return NavResult(url=url, ok=url in self._pages)

    async def current_url(self) -> str:
        return self._current

    async def title(self) -> str:
        return self._page().title

    async def collect_controls(self):
        return [dict(c) for c in self._page().controls]

    async def dialog_flags(self):
        return []

    async def error_texts(self):
        return list(self._page().errors)

    async def screenshot_png(self) -> bytes:
        return PNG_1x1

    async def click(self, control):
        before = self._current
        dest = self._page().click_targets.get(control.get("name"))
        if dest:
            self._current = dest
            return RawObservation(url_before=before, url_after=dest)
        return RawObservation(url_before=before, url_after=before)

    async def fill(self, control, value):
        return RawObservation(url_before=self._current, url_after=self._current,
                              committed_value=value)

    async def select_option(self, control, value):
        return RawObservation(url_before=self._current, url_after=self._current,
                              committed_value=value)

    async def set_checked(self, control, checked):
        return RawObservation(url_before=self._current, url_after=self._current,
                              committed_value="true" if checked else "false")

    async def storage_state(self):
        return {"cookies": [], "origins": []}

    async def hover(self, control):
        return RawObservation(url_before=self._current, url_after=self._current)

    async def set_input_files(self, control, paths):
        self.uploads.append((control.get("name"), list(paths)))
        fname = (paths[0].rsplit("/", 1)[-1].rsplit("\\", 1)[-1]) if paths else ""
        return RawObservation(url_before=self._current, url_after=self._current,
                              committed_value=fname)

    async def materialize(self):
        self.materialize_calls += 1

    async def drain_network(self):
        self.network_drains += 1
        return list(self.network_by_url.pop(self._current, []))


async def _no_sleep(_seconds: float) -> None:
    return None


def _build_crawler(port, work_dir, *, budget=None, target_url="https://app.example/home",
                   credentials=None, scope_path_prefixes=()):
    guard_ctx = GuardContext(refuse_pack=_REFUSE_PACK)
    return Crawler(
        port,
        crawl_id="c1", tenant_id="t1", target_url=target_url, work_dir=str(work_dir),
        refuse_pack=_REFUSE_PACK, budget=budget or Budget(rate_per_s=0),
        explorer_version="test/1.0", guard_version="test", refuse_pack_version=_REFUSE_PACK.version,
        config_fingerprint="fp", guard_context=guard_ctx, sleep=_no_sleep,
        credentials=credentials, scope_path_prefixes=scope_path_prefixes,
    )


# ─── classify_after (synthetic observations) ───────────────────────────────────


def test_classify_after_navigation_wins():
    out = classify_after(RawObservation(url_before="https://a/x", url_after="https://a/y"))
    assert out.outcome == OUTCOME_NAVIGATION and out.navigated and out.url_changed


def test_classify_after_navigation_ignores_fragment_and_trailing_slash():
    out = classify_after(RawObservation(url_before="https://a/x", url_after="https://a/x/#frag"))
    assert out.outcome == OUTCOME_NONE and not out.navigated


def test_classify_after_value_committed():
    out = classify_after(RawObservation(url_before="u", url_after="u", committed_value="hi"))
    assert out.outcome == OUTCOME_VALUE_COMMITTED and not out.navigated


def test_classify_after_error_beats_value_and_dialog():
    out = classify_after(RawObservation(url_before="u", url_after="u",
                                        committed_value="hi", dialog_opened=True,
                                        error_detail="Invalid input"))
    assert out.outcome == OUTCOME_ERROR


def test_classify_after_dialog_and_dom_and_none():
    assert classify_after(RawObservation(url_before="u", url_after="u",
                                         dialog_opened=True)).outcome == OUTCOME_DIALOG
    assert classify_after(RawObservation(url_before="u", url_after="u",
                                         dom_changed=True)).outcome == OUTCOME_DOM_CHANGED
    assert classify_after(RawObservation(url_before="u", url_after="u")).outcome == OUTCOME_NONE


def test_classify_after_navigation_over_error_when_url_changed():
    out = classify_after(RawObservation(url_before="https://a/x", url_after="https://a/y",
                                        error_detail="whatever"))
    assert out.outcome == OUTCOME_NAVIGATION and out.navigated


# ─── Frontier (dedup + priority) ────────────────────────────────────────────────


def test_frontier_dedups_by_reach_key():
    f = Frontier()
    assert f.push(FrontierItem(url="https://a/home"), key="a/home") is True
    assert f.push(FrontierItem(url="https://a/home?x=1"), key="a/home") is False
    assert len(f) == 1
    assert f.pop().url == "https://a/home"
    assert f.pop() is None


def test_frontier_orders_by_priority_then_depth():
    f = Frontier()
    f.push(FrontierItem(url="d2", depth=2), key="d2")
    f.push(FrontierItem(url="d0", depth=0), key="d0")
    f.push(FrontierItem(url="p_hi", depth=9, priority=-1), key="p_hi")
    assert f.pop().url == "p_hi"   # lower priority value first
    assert f.pop().url == "d0"     # then lower depth
    assert f.pop().url == "d2"


def test_frontier_info_gain_interleaves_across_app_sections():
    """Information-gain planner (#3): a newly-seen section is visited before the
    SECOND sibling of an already-queued section — so a finite budget spends on
    breadth of app regions, not on draining one link-heavy section first."""
    f = Frontier()
    # two items in one section, then a first item of a fresh section pushed LAST.
    f.push(FrontierItem(url="saws"), key="https://a/catalog/hand-tools/saws")
    f.push(FrontierItem(url="drills"), key="https://a/catalog/hand-tools/drills")
    f.push(FrontierItem(url="policies"), key="https://a/account/policies")
    # the novel section (policies, rank 0) outranks the saturated section's 2nd
    # sibling (drills, rank 1) even though drills was queued earlier.
    assert [f.pop().url for _ in range(3)] == ["saws", "policies", "drills"]


def test_frontier_novelty_falls_back_to_depth_and_fifo_within_a_tier():
    """Within one novelty tier the order is the prior breadth-first/FIFO contract
    (regression guard: distinct sections all rank 0 → depth then insertion)."""
    f = Frontier()
    f.push(FrontierItem(url="deep", depth=3), key="https://a/x/deep")
    f.push(FrontierItem(url="shallow", depth=0), key="https://a/y/shallow")
    assert [f.pop().url for _ in range(2)] == ["shallow", "deep"]


def test_caged_planner_raises_a_high_value_section_ahead():
    """The caged planner (C): a grounded priority pattern lifts a matching route
    ahead of an unmatched one — even when the unmatched one was queued first and
    is shallower. Ordering-only; nothing about reachability changes."""
    from app.crawler import _parse_plan_patterns
    patterns = _parse_plan_patterns({"priority_patterns": [
        {"pattern": "quote", "weight": 3, "reason": "money funnel"}]})
    f = Frontier(patterns)
    f.push(FrontierItem(url="privacy", depth=0), key="https://a/privacy")
    f.push(FrontierItem(url="quote", depth=2), key="https://a/quote/step1")
    # 'quote' (priority -3) outranks 'privacy' (priority 0) despite being deeper
    # and queued later.
    assert [f.pop().url for _ in range(2)] == ["quote", "privacy"]


def test_caged_planner_never_overrides_an_explicit_seed_priority():
    from app.crawler import _parse_plan_patterns
    f = Frontier(_parse_plan_patterns({"priority_patterns": [{"pattern": "quote", "weight": 3}]}))
    # an explicit -5 seed still wins over a plan-boosted -3 route.
    f.push(FrontierItem(url="seed", priority=-5), key="https://a/other")
    f.push(FrontierItem(url="quote"), key="https://a/quote")
    assert f.pop().url == "seed"


def test_plan_parse_is_fully_defensive():
    """The explorer RE-BOUNDS the plan (defense in depth): unsafe patterns, bad
    weights, oversized lists all drop; an empty plan ⇒ byte-identical crawl."""
    from app.crawler import _parse_plan_patterns
    assert _parse_plan_patterns(None) == []
    assert _parse_plan_patterns({}) == []
    got = _parse_plan_patterns({"priority_patterns": [
        {"pattern": "quote", "weight": 9},          # weight clamped to 3
        {"pattern": "bad pattern!"},                 # space/'!' → rejected by regex
        {"pattern": ".*(evil)"},                     # regex metachars → rejected
        {"pattern": "quote"},                        # duplicate → dropped
        {"pattern": "checkout", "weight": 0},        # clamped to 1
        "not-a-dict",                                # ignored
    ]})
    assert got == [("quote", 3), ("checkout", 1)]
    # oversized list is capped at 8.
    big = {"priority_patterns": [{"pattern": f"sec{i}", "weight": 2} for i in range(20)]}
    assert len(_parse_plan_patterns(big)) == 8


# ─── BudgetTracker (honest stop_reason + precedence) ────────────────────────────


def _tracker(**budget_over):
    clock = FakeClock()
    return BudgetTracker(Budget(**budget_over), clock), clock


def test_budget_states_stop():
    tr, _ = _tracker(max_states=2, max_wall_ms=0, max_requests=0)
    assert tr.stop_reason() == ""
    tr.note_state(); tr.note_state()
    assert tr.stop_reason() == STOP_MAX_STATES


def test_budget_requests_stop():
    tr, _ = _tracker(max_requests=3, max_wall_ms=0, max_states=0)
    tr.note_request(); tr.note_request(); tr.note_request()
    assert tr.stop_reason() == STOP_MAX_REQUESTS


def test_budget_wall_stop_and_precedence():
    tr, clock = _tracker(max_wall_ms=1000, max_requests=1, max_states=1)
    clock.t = 1000
    tr.note_request(); tr.note_state()
    # wall + requests + states all breached → wall reported first (precedence).
    assert tr.stop_reason() == STOP_MAX_WALL_MS


# ─── End-to-end crawl (dedup, monotonic sequence, honest completion) ────────────


def _two_page_site():
    home = "https://app.example/home"
    products = "https://app.example/products"
    pages = {
        home: FakePage(home, [
            _raw("link", "Products"), _raw("link", "Home"),
            _raw("button", "Delete account", tag="button"),  # danger — never clicked
        ], title="Home", click_targets={"Products": products, "Home": home}),
        products: FakePage(products, [
            _raw("link", "Home"),
        ], title="Products", click_targets={"Home": home}),
    }
    return pages, home


def test_crawl_records_each_unique_state_once():
    import tempfile
    with tempfile.TemporaryDirectory() as work:
        pages, home = _two_page_site()
        crawler = _build_crawler(FakeBrowser(pages, home), work)
        summary = asyncio.run(crawler.run())
        assert summary.stop_reason == STOP_COMPLETED
        states = [r for r in read_records(work, "c1") if r["type"] == REC_PAGE_STATE]
        # home + products, each once (home is reachable again via a link → deduped)
        assert len(states) == 2
        seqs = [s["sequence_index"] for s in states]
        assert seqs == sorted(seqs) and len(set(seqs)) == 2  # strictly monotonic
        assert summary.screenshots == 2


def test_crawl_never_clicks_irreversible_control():
    import tempfile
    with tempfile.TemporaryDirectory() as work:
        pages, home = _two_page_site()
        crawler = _build_crawler(FakeBrowser(pages, home), work)
        asyncio.run(crawler.run())
        actions = [r for r in read_records(work, "c1") if r["type"] == "action"]
        labels = {a["target_label"] for a in actions}
        assert "Delete account" not in labels  # the never-click leaf was skipped


def test_crawl_resume_skips_visited_states():
    import tempfile
    with tempfile.TemporaryDirectory() as work:
        pages, home = _two_page_site()
        asyncio.run(_build_crawler(FakeBrowser(pages, home), work).run())
        before = len([r for r in read_records(work, "c1") if r["type"] == REC_PAGE_STATE])
        # Re-run same crawl_id/work_dir: resume seeds visited fingerprints.
        summary2 = asyncio.run(_build_crawler(FakeBrowser(pages, home), work).run())
        after = len([r for r in read_records(work, "c1") if r["type"] == REC_PAGE_STATE])
        assert before == 2
        assert after == before  # no state re-recorded on resume
        assert summary2.stop_reason == STOP_COMPLETED


def test_crawl_budget_max_states_stops_honestly():
    import tempfile
    with tempfile.TemporaryDirectory() as work:
        pages, home = _two_page_site()
        crawler = _build_crawler(FakeBrowser(pages, home), work,
                                 budget=Budget(max_states=1, rate_per_s=0))
        summary = asyncio.run(crawler.run())
        assert summary.stop_reason == STOP_MAX_STATES
        assert summary.states == 1


# ─── TARGET MODE (R3 Mode 2): journey-confined crawling ─────────────────────────


def _journey_site():
    """A quote journey (/quote → /quote/review) plus unrelated pages the scope
    must exclude — including /quotes, the prefix-boundary trap."""
    quote = "https://app.example/quote"
    review = "https://app.example/quote/review"
    products = "https://app.example/products"
    quotes_list = "https://app.example/quotes"
    pages = {
        quote: FakePage(quote, [
            _raw("link", "Review"), _raw("link", "Products"), _raw("link", "All quotes"),
        ], title="Quote", click_targets={
            "Review": review, "Products": products, "All quotes": quotes_list}),
        review: FakePage(review, [_raw("link", "Back")], title="Review",
                         click_targets={"Back": quote}),
        products: FakePage(products, [], title="Products"),
        quotes_list: FakePage(quotes_list, [], title="Quotes"),
    }
    return pages, quote


def test_target_scope_confines_crawl_to_the_journey():
    """scope=['/quote']: the crawl records /quote and /quote/review ONLY —
    /products (unrelated) and /quotes (prefix-boundary trap) are out of scope."""
    import tempfile
    with tempfile.TemporaryDirectory() as work:
        pages, quote = _journey_site()
        crawler = _build_crawler(FakeBrowser(pages, quote), work, target_url=quote,
                                 scope_path_prefixes=["/quote"])
        summary = asyncio.run(crawler.run())
        assert summary.stop_reason == STOP_COMPLETED
        states = [r for r in read_records(work, "c1") if r["type"] == REC_PAGE_STATE]
        paths = sorted({s["url_path"] for s in states})
        assert paths == ["/quote", "/quote/review"], paths


def test_no_scope_is_byte_identical_whole_app_explore():
    """Empty scope: classic Explore mode — every reachable page is visited."""
    import tempfile
    with tempfile.TemporaryDirectory() as work:
        pages, quote = _journey_site()
        crawler = _build_crawler(FakeBrowser(pages, quote), work, target_url=quote)
        asyncio.run(crawler.run())
        states = [r for r in read_records(work, "c1") if r["type"] == REC_PAGE_STATE]
        paths = sorted({s["url_path"] for s in states})
        assert paths == ["/products", "/quote", "/quote/review", "/quotes"], paths


def test_scope_meta_is_recorded_for_audit():
    import tempfile
    with tempfile.TemporaryDirectory() as work:
        pages, quote = _journey_site()
        crawler = _build_crawler(FakeBrowser(pages, quote), work, target_url=quote,
                                 scope_path_prefixes=["/quote/", "bad-no-slash", "/quote"])
        asyncio.run(crawler.run())
        metas = [r for r in read_records(work, "c1") if r["type"] == "crawl_meta"]
        assert metas and metas[-1].get("scope_path_prefixes") == ["/quote"], \
            "normalised scope (deduped, trailing slash stripped, junk dropped) must be auditable"


# ─── auth: public-page resilience vs honest login-wall failure (Fix #1 + #2) ────


def _quote_page_no_login():
    """A PUBLIC quote page: a native Product <select> + a benign nav, and NO login
    form anywhere — the exact shape that aborted the client's crawl auth_failed."""
    quote = "https://app.example/quote"
    pages = {
        quote: FakePage(quote, [
            _raw("combobox", "Product", input_type="select-one", tag="select",
                 options=["VKPower Term", "Heritage Whole Life"]),
            _raw("link", "Home"),
        ], title="Get a quote", click_targets={"Home": quote}),
    }
    return pages, quote


def test_credentialed_crawl_of_public_page_explores_and_flags_auth_incomplete():
    """Fix #2: credentials supplied, but the entry has NO login form (a public quote
    page). The crawl must NOT abort auth_failed — it explores unauthenticated and flags
    ``auth_incomplete`` LOUDLY so the operator knows authenticated areas were skipped.
    Also proves Fix #1: the Product <select> is never mistaken for a username field
    (no valueless ``type Product`` action is ever produced)."""
    import tempfile
    with tempfile.TemporaryDirectory() as work:
        pages, quote = _quote_page_no_login()
        crawler = _build_crawler(FakeBrowser(pages, quote), work, target_url=quote,
                                 credentials=Credentials(username="u", password="p"))
        summary = asyncio.run(crawler.run())
        assert summary.stop_reason != STOP_AUTH_FAILED          # did NOT throw the page away
        assert summary.stop_reason == STOP_COMPLETED            # explored + finished honestly
        assert summary.coverage.get("auth_incomplete") is True
        assert "AUTHENTICATED AREAS NOT COVERED" in summary.coverage.get("summary", "")
        # Fix #1: the <select> never became a valueless 'type Product' (the crash's signature).
        actions = [r for r in read_records(work, "c1") if r["type"] == "action"]
        product = [a for a in actions if a.get("target_label") == "Product"]
        assert all(not (a["verb"] == "type" and a.get("value") in (None, "")) for a in product)


def test_credentialed_crawl_login_wall_still_aborts_auth_failed():
    """Fix #2 must NOT green-wash a genuine auth failure: a REAL login wall (a password
    is submitted but login can't be verified) stays an honest auth_failed hard stop, and
    ``auth_incomplete`` is NOT set (that flag is only for public, no-login-form pages)."""
    import tempfile
    with tempfile.TemporaryDirectory() as work:
        login = "https://app.example/login"
        pages = {
            login: FakePage(login, [
                _raw("textbox", "Username", input_type="text"),
                _raw("textbox", "Password", input_type="password"),
                _raw("button", "Sign in", tag="button"),
            ], title="Login"),   # submit has no click_target → never advances → login_unverified
        }
        crawler = _build_crawler(FakeBrowser(pages, login), work, target_url=login,
                                 credentials=Credentials(username="u", password="p"))
        summary = asyncio.run(crawler.run())
        assert summary.stop_reason == STOP_AUTH_FAILED
        assert summary.coverage.get("auth_incomplete") is not True


def _href_site():
    """A pushState/SPA-style site: the nav links' CLICKS do NOT change page.url
    (empty click_targets), so traversal must come from the links' HREFS."""
    home = "https://app.example/home"
    catalog = "https://app.example/catalog"
    pages = {
        home: FakePage(home, [
            _raw("link", "Catalog", href=catalog),                    # in-scope route → follow
            _raw("link", "Docs", href="https://external.example/x"),  # off-site → skip
            _raw("link", "Jump", href="#section-2"),                  # cosmetic anchor → skip
            _raw("link", "Email", href="mailto:hi@app.example"),      # non-navigational → skip
        ], title="Home", click_targets={}),
        catalog: FakePage(catalog, [
            _raw("link", "Home", href=home),                          # already enqueued → deduped
        ], title="Catalog", click_targets={}),
    }
    return pages, home


def test_crawl_follows_link_hrefs_without_a_click_navigation():
    """SPA traversal (Fix A): a link whose CLICK does not change the URL is still
    traversed via its href; off-site / cosmetic-anchor / mailto hrefs are not."""
    import tempfile
    with tempfile.TemporaryDirectory() as work:
        pages, home = _href_site()
        crawler = _build_crawler(FakeBrowser(pages, home), work)
        summary = asyncio.run(crawler.run())
        assert summary.stop_reason == STOP_COMPLETED
        states = [r for r in read_records(work, "c1") if r["type"] == REC_PAGE_STATE]
        locations = {s["location"] for s in states}
        # materialize (lazy/virtual-scroll harvest) ran before inventorying states.
        assert crawler._port.materialize_calls >= 1
        # href-follow reached the catalog page even though NO click navigated,
        assert home in locations
        assert "https://app.example/catalog" in locations
        assert len(states) == 2
        # and never wandered off-site / to a cosmetic anchor / mailto.
        assert not any("external.example" in loc for loc in locations)


def test_href_link_click_without_navigation_records_no_grounded_action():
    """Direct-nav grounding is ATTEMPTED on an in-scope nav link, but when the click
    produces NO URL change (a pushState SPA whose delta isn't observable in the settle
    window — the FakeBrowser here has no click target), no grounded action is recorded
    (never a fabricated navigation); href-follow still traverses the page."""
    import tempfile
    with tempfile.TemporaryDirectory() as work:
        pages, home = _href_site()
        crawler = _build_crawler(FakeBrowser(pages, home), work)
        asyncio.run(crawler.run())
        actions = [r for r in read_records(work, "c1") if r["type"] == "action"]
        # a click that did not navigate is NOT recorded as a grounded nav action:
        cat_navs = [a for a in actions if a["target_label"] == "Catalog"
                    and a.get("after", {}).get("outcome") == "navigation"]
        assert not cat_navs, "a non-navigating click must not record a grounded navigation"
        states = [r for r in read_records(work, "c1") if r["type"] == REC_PAGE_STATE]
        locations = {s["location"] for s in states}
        assert "https://app.example/catalog" in locations  # href-follow still traversed it


def test_ground_nav_links_records_grounded_click_for_plain_href_nav():
    """Part 2 (direct-nav grounding): a plain ``<a href>`` nav link (NOT menu-gated)
    is CLICKED and its ``[click → navigation]`` is recorded — so a classic multi-page
    site (whose nav is ordinary links, not a menu or SPA) produces a grounded
    click-path the journey generator can turn into a coherent test, instead of only
    an href-followed milestone with no proven click."""
    import tempfile
    with tempfile.TemporaryDirectory() as work:
        home = "https://app.example/home"
        products = "https://app.example/products"
        pages = {
            home: FakePage(home, [_raw("link", "Products", href=products)],
                           title="Home", click_targets={"Products": products}),
            products: FakePage(products, [_raw("link", "Home", href=home)],
                               title="Products", click_targets={"Home": home}),
        }
        crawler = _build_crawler(FakeBrowser(pages, home), work, target_url=home)
        summary = asyncio.run(crawler.run())
        assert summary.stop_reason == STOP_COMPLETED
        states = [r for r in read_records(work, "c1") if r["type"] == REC_PAGE_STATE]
        home_state = next(s for s in states if s["location"] == home)
        prod = [a for a in home_state["actions"]
                if a["target_label"] == "Products" and a["verb"] == "click"]
        assert prod, "the plain nav link must be CLICKED (grounded), not only href-followed"
        assert prod[0]["after"]["outcome"] == "navigation", "the click records a grounded navigation"
        assert prod[0].get("to_state"), "the grounded nav carries its destination state"


def test_file_input_upload_is_recorded_as_a_grounded_action():
    """File-upload RECORDING (#8): a document-upload field is attached a seed file
    in Phase-A and the attach is RECORDED as a grounded ``upload`` action carrying
    the filename — the substrate now carries the verb and the frozen factory
    demotes it honestly (generation of an upload step stays deferred)."""
    import tempfile
    with tempfile.TemporaryDirectory() as work:
        home = "https://app.example/apply"
        pages = {home: FakePage(home, [
            _raw("textbox", "Full name", tag="input", input_type="text"),
            _raw("textbox", "Upload ID document", tag="input", input_type="file"),
        ], title="Apply", click_targets={})}
        browser = FakeBrowser(pages, home)
        crawler = _build_crawler(browser, work, target_url=home)
        asyncio.run(crawler.run())
        # the file input was populated exactly once, with one seed path:
        assert len(browser.uploads) == 1
        assert browser.uploads[0][0] == "Upload ID document"
        assert len(browser.uploads[0][1]) == 1
        # the attach is now a grounded 'upload' action carrying the seed filename.
        states = [r for r in read_records(work, "c1") if r["type"] == REC_PAGE_STATE]
        uploads = [a for s in states for a in s.get("actions", []) if a.get("verb") == "upload"]
        assert len(uploads) == 1
        assert uploads[0]["target_label"] == "Upload ID document"
        assert uploads[0]["value"]                      # the attached filename (evidence)
        assert uploads[0]["after"]["outcome"] == "value_committed"


def test_upload_verb_is_in_the_substrate_vocabulary_and_validates():
    """The 'upload' verb is accepted by the real qe-central substrate schema (so the
    recorded action ingests instead of 422-ing the whole crawl)."""
    schema = _load_schema_module()
    assert "upload" in schema.ACTION_VERBS
    action = schema.ActionRecord.model_validate({
        "subaction_index": 0, "verb": "upload", "target_kind": "text_field",
        "target_label": "Upload ID document", "value": "qec-seed.pdf",
        "after": {"outcome": "value_committed"},
    })
    assert action.verb == "upload"


class _HoverBrowser(FakeBrowser):
    """FakeBrowser where a fly-out nav link exists ONLY while its ``aria-haspopup``
    trigger is hovered — a mega-menu whose destinations never appear in the static
    DOM.  A fresh ``goto`` collapses the menu (as a real browser would)."""

    def __init__(self, pages, start_url, *, trigger, revealed):
        super().__init__(pages, start_url)
        self._trigger = trigger
        self._revealed = revealed
        self._open_on = None
        self.hover_calls: list = []

    async def goto(self, url):
        self._open_on = None
        return await super().goto(url)

    async def hover(self, control):
        self.hover_calls.append(control.get("name"))
        if control.get("name") == self._trigger:
            self._open_on = self._current
        return await super().hover(control)

    async def collect_controls(self):
        base = await super().collect_controls()
        if self._open_on == self._current:
            return base + [dict(self._revealed)]
        return base


class _DropdownBrowser(FakeBrowser):
    """A CLICK dropdown (aria-expanded) whose nav item is HIDDEN until the toggle
    is clicked — the live practicesoftwaretesting 'Categories' pattern. A bare
    click on the hidden item can't reach it; the crawler must OPEN the toggle
    first, then click the revealed item to record a grounded navigation."""

    def __init__(self, home: str, cat_url: str) -> None:
        super().__init__({
            home: FakePage(home, [], title="Home"),
            cat_url: FakePage(cat_url, [_raw("link", "Home", href=home)], title="Hand Tools"),
        }, home)
        self._home, self._cat = home, cat_url
        self._menu_open = False

    async def goto(self, url):
        self._menu_open = False                     # a fresh load closes the menu
        self._current = url
        return NavResult(url=url, ok=(url in self._pages))

    async def collect_controls(self):
        if self._current == self._home:
            base = [_raw("button", "Categories", tag="button", expanded="false")]
            if self._menu_open:                     # the item appears only when open
                base.append(_raw("link", "Hand Tools", href=self._cat))
            return [dict(c) for c in base]
        return [dict(c) for c in self._page().controls]

    async def click(self, control):
        name = control.get("name")
        if name == "Categories":                    # toggle opens the dropdown (no nav)
            self._menu_open = True
            return RawObservation(url_before=self._current, url_after=self._current, dom_changed=True)
        if name == "Hand Tools" and self._menu_open:  # revealed item navigates
            self._current = self._cat
            return RawObservation(url_before=self._home, url_after=self._cat)
        return RawObservation(url_before=self._current, url_after=self._current)


def test_menu_reveal_opens_click_dropdown_and_records_grounded_click_path():
    """The live-defect fix: a category link hidden in a closed aria-expanded
    dropdown is reached by OPENING the toggle then CLICKING the item, and the
    grounded [open, nav-click] path is recorded so the generated flow is RUNNABLE
    (not an un-driveable href milestone)."""
    import tempfile
    with tempfile.TemporaryDirectory() as work:
        home = "https://app.example/home"
        cat = "https://app.example/category/hand-tools"
        browser = _DropdownBrowser(home, cat)
        crawler = _build_crawler(browser, work, target_url=home)
        summary = asyncio.run(crawler.run())
        assert summary.stop_reason == STOP_COMPLETED

        states = [r for r in read_records(work, "c1") if r["type"] == REC_PAGE_STATE]
        home_state = next(s for s in states if s["location"] == home)
        acts = home_state["actions"]
        # the grounded click-path: open 'Categories' THEN click 'Hand Tools' → navigation.
        assert any(a["verb"] == "click" and a["target_label"] == "Categories" for a in acts)
        hand = [a for a in acts if a["target_label"] == "Hand Tools"]
        assert hand and hand[0]["after"]["outcome"] == "navigation"
        # and the category page was actually reached (a recorded milestone).
        assert any(s["url_path"] == "/category/hand-tools" for s in states)


def test_hover_reveals_flyout_nav_that_is_never_clicked_statically():
    """Hover-reveal (#9): a route exposed only by hovering an ``aria-haspopup``
    mega-menu trigger is discovered and traversed — even though it is absent from
    the static inventory and is never clicked."""
    import tempfile
    with tempfile.TemporaryDirectory() as work:
        home = "https://app.example/home"
        products = "https://app.example/products"
        pages = {
            home: FakePage(home, [
                _raw("button", "Products", tag="button", haspopup="menu"),
            ], title="Home"),
            products: FakePage(products, [
                _raw("link", "Home", href=home),
            ], title="Products"),
        }
        revealed = _raw("link", "All Products", href=products)
        browser = _HoverBrowser(pages, home, trigger="Products", revealed=revealed)
        crawler = _build_crawler(browser, work, target_url=home)
        summary = asyncio.run(crawler.run())

        assert summary.stop_reason == STOP_COMPLETED
        states = [r for r in read_records(work, "c1") if r["type"] == REC_PAGE_STATE]
        locations = {s["location"] for s in states}
        # the hover trigger was actually hovered, and the fly-out route was reached:
        assert "Products" in browser.hover_calls
        assert products in locations
        # the fly-out link is NOT in the static inventory, so it was never CLICKED —
        # discovery was purely via the hover-reveal pass.
        actions = [r for r in read_records(work, "c1") if r["type"] == "action"]
        assert "All Products" not in {a["target_label"] for a in actions}


class _ValueBrowser(FakeBrowser):
    """FakeBrowser that renders value nodes (a premium + a decision) so the #2
    inference can be exercised end-to-end through the recorded page_state."""

    def __init__(self, pages, start_url, *, values):
        super().__init__(pages, start_url)
        self._values = values

    async def collect_displayed_values(self):
        return [dict(v) for v in self._values]


def test_displayed_values_carry_candidate_inference():
    """Value-oracle inference (#2): recorded displayed_values are annotated with a
    crawl-side classification so candidate expected outcomes (a premium, a
    decision) are surfaced for confirmation — proving stays in the frozen oracle."""
    import tempfile
    with tempfile.TemporaryDirectory() as work:
        home = "https://app.example/quote"
        pages = {home: FakePage(home, [], title="Quote")}
        values = [
            {"label": "Monthly Premium", "selector": "#premium", "text": "$75.00"},
            {"label": "Decision", "selector": "#decision", "text": "Approved"},
            {"label": "Heading", "selector": "#h", "text": "Your quote is ready"},
        ]
        browser = _ValueBrowser(pages, home, values=values)
        crawler = _build_crawler(browser, work, target_url=home)
        asyncio.run(crawler.run())

        states = [r for r in read_records(work, "c1") if r["type"] == REC_PAGE_STATE]
        nodes = {n["selector"]: n for s in states for n in s.get("displayed_values", [])}
        assert nodes["#premium"]["value_type"] == "currency"
        assert nodes["#premium"]["value_candidate"] == "true"
        assert float(nodes["#premium"]["value_confidence"]) >= 0.9
        assert nodes["#decision"]["value_type"] == "decision"
        assert nodes["#decision"]["value_candidate"] == "true"
        # prose is context, never a candidate expected value.
        assert nodes["#h"]["value_candidate"] == "false"


def test_wizard_advance_vocabulary_is_fail_closed():
    """The advance gate (#1): a Next/Continue/Proceed/Forward label advances ONLY
    when it carries no commit/terminal word — any commit word vetoes (fail-closed),
    independent of the guard's danger flag."""
    from app.crawler import _is_wizard_advance
    assert _is_wizard_advance("Next")
    assert _is_wizard_advance("Continue")
    assert _is_wizard_advance("Proceed")
    assert _is_wizard_advance("Save and Continue")   # 'save' alone is not a commit word
    # commit / terminal words VETO even alongside an advance word:
    assert not _is_wizard_advance("Continue to payment")
    assert not _is_wizard_advance("Proceed to checkout")
    assert not _is_wizard_advance("Submit")
    assert not _is_wizard_advance("Place order")
    assert not _is_wizard_advance("Confirm and Continue")
    # not an advance word at all:
    assert not _is_wizard_advance("Back")
    assert not _is_wizard_advance("Apply filters")


class _WizardBrowser(FakeBrowser):
    """A multi-step SPA wizard living at ONE url: each ``click`` on the current
    step's advance button swaps the DOM to the next step in place (dom_changed,
    no navigation) — step N is reachable ONLY by the click sequence.  A fresh
    ``goto`` resets to step 0 (as a real reload would)."""

    def __init__(self, apply_url: str, steps: list) -> None:
        super().__init__({apply_url: FakePage(apply_url, [], title="Apply")}, apply_url)
        self._apply_url = apply_url
        self._steps = steps           # [(controls, advance_button_name_or_None), ...]
        self._i = 0
        self.advance_clicks: list = []

    async def goto(self, url: str) -> NavResult:
        self._i = 0
        self._current = url
        return NavResult(url=url, ok=(url == self._apply_url))

    async def collect_controls(self):
        return [dict(c) for c in self._steps[self._i][0]]

    async def click(self, control):
        name = control.get("name")
        advance_name = self._steps[self._i][1]
        if name == advance_name and self._i + 1 < len(self._steps):
            self._i += 1
            self.advance_clicks.append(name)
            # in-place step swap: DOM changed, URL unchanged.
            return RawObservation(url_before=self._current, url_after=self._current,
                                  dom_changed=True)
        return RawObservation(url_before=self._current, url_after=self._current)


def test_wizard_walk_records_deep_steps_and_stops_before_submit():
    """Wizard/stepper traversal (#1): the crawler advances non-danger Next/Continue
    to record deeper SPA wizard steps in place, and STOPS at the terminal Submit —
    the submit boundary is never crossed."""
    import tempfile
    with tempfile.TemporaryDirectory() as work:
        apply_url = "https://app.example/apply"
        steps = [
            ([_raw("textbox", "Full name", tag="input", input_type="text"),
              _raw("button", "Next", tag="button")], "Next"),
            ([_raw("textbox", "Email", tag="input", input_type="text"),
              _raw("button", "Continue", tag="button")], "Continue"),
            # terminal review step — the commit button must NOT be advanced.
            ([_raw("textbox", "Coverage amount", tag="input", input_type="text"),
              _raw("button", "Submit application", tag="button")], "Submit application"),
        ]
        browser = _WizardBrowser(apply_url, steps)
        crawler = _build_crawler(browser, work, target_url=apply_url)
        summary = asyncio.run(crawler.run())

        assert summary.stop_reason == STOP_COMPLETED
        # advanced exactly twice (step0→1, step1→2); the Submit step ended the walk.
        assert browser.advance_clicks == ["Next", "Continue"]
        assert crawler._wizard_advances == 2
        states = [r for r in read_records(work, "c1") if r["type"] == REC_PAGE_STATE]
        assert len(states) == 3               # all three steps recorded (deep coverage)
        # the deepest step carries the review field — proof step 3 was reached.
        deepest = states[-1]
        assert any(lbl == "Coverage amount" for lbl in deepest["form_snapshot"])
        # the terminal Submit was NEVER clicked (submit boundary held).
        clicked = {a["target_label"] for s in states for a in s.get("actions", [])}
        assert "Submit application" not in clicked
        assert {"Next", "Continue"}.issubset(clicked)


class _GatedWizardBrowser(FakeBrowser):
    """A VALIDATION-GATED multi-step wizard: 'Next' advances only if the current
    step was filled since the last goto (a real reload discards fills).  Step 0 also
    carries an ``aria-haspopup`` nav trigger, so ``_discover``'s hover-reveal does a
    goto reset BEFORE the wizard walk — exercising the walk's re-establish-filled-
    state path (the fix for a nav menu silently defeating a gated wizard)."""

    def __init__(self, apply_url: str, steps: list) -> None:
        super().__init__({apply_url: FakePage(apply_url, [], title="Apply")}, apply_url)
        self._apply_url = apply_url
        self._steps = steps
        self._i = 0
        self._filled = False
        self.advance_clicks: list = []

    async def goto(self, url: str) -> NavResult:
        self._i = 0
        self._filled = False           # a fresh load clears the form
        self._current = url
        return NavResult(url=url, ok=(url == self._apply_url))

    async def collect_controls(self):
        return [dict(c) for c in self._steps[self._i][0]]

    async def fill(self, control, value):
        self._filled = True
        return await super().fill(control, value)

    async def hover(self, control):
        return RawObservation(url_before=self._current, url_after=self._current)

    async def click(self, control):
        name = control.get("name")
        advance_name = self._steps[self._i][1]
        # validation gate: advance ONLY when the step was filled since the last load.
        if name == advance_name and self._filled and self._i + 1 < len(self._steps):
            self._i += 1
            self._filled = False
            self.advance_clicks.append(name)
            return RawObservation(url_before=self._current, url_after=self._current,
                                  dom_changed=True)
        return RawObservation(url_before=self._current, url_after=self._current)


def test_wizard_walk_reestablishes_fills_after_a_discover_reset():
    """Wizard/stepper (#1) regression: a validation-gated wizard whose page ALSO
    has an aria-haspopup nav menu (so hover-reveal resets the page to an unfilled
    step 0) still advances — the walk re-navigates + re-fills before advancing."""
    import tempfile
    with tempfile.TemporaryDirectory() as work:
        apply_url = "https://app.example/apply"
        steps = [
            ([_raw("textbox", "Full name", tag="input", input_type="text"),
              _raw("button", "Menu", tag="button", haspopup="menu"),   # forces a reset
              _raw("button", "Next", tag="button")], "Next"),
            ([_raw("textbox", "Email", tag="input", input_type="text"),
              _raw("button", "Continue", tag="button")], "Continue"),
            ([_raw("textbox", "Coverage amount", tag="input", input_type="text"),
              _raw("button", "Submit application", tag="button")], "Submit application"),
        ]
        browser = _GatedWizardBrowser(apply_url, steps)
        crawler = _build_crawler(browser, work, target_url=apply_url)
        asyncio.run(crawler.run())

        # despite the hover-reveal reset, the gated wizard advanced both steps.
        assert browser.advance_clicks == ["Next", "Continue"]
        states = [r for r in read_records(work, "c1") if r["type"] == REC_PAGE_STATE]
        assert any("Coverage amount" in s["form_snapshot"] for s in states)


def test_wizard_walk_is_disabled_by_the_kill_switch():
    """``wizard_enabled=False`` restores the strict submit-boundary: a form's
    Next is never clicked, only the entry step is recorded."""
    import tempfile
    with tempfile.TemporaryDirectory() as work:
        apply_url = "https://app.example/apply"
        steps = [
            ([_raw("textbox", "Full name", tag="input", input_type="text"),
              _raw("button", "Next", tag="button")], "Next"),
            ([_raw("textbox", "Email", tag="input", input_type="text"),
              _raw("button", "Continue", tag="button")], "Continue"),
        ]
        browser = _WizardBrowser(apply_url, steps)
        guard_ctx = GuardContext(refuse_pack=_REFUSE_PACK)
        crawler = Crawler(
            browser, crawl_id="c1", tenant_id="t1", target_url=apply_url,
            work_dir=str(work), refuse_pack=_REFUSE_PACK, budget=Budget(rate_per_s=0),
            explorer_version="test/1.0", guard_version="test",
            refuse_pack_version=_REFUSE_PACK.version, config_fingerprint="fp",
            guard_context=guard_ctx, wizard_enabled=False,
        )
        asyncio.run(crawler.run())
        assert browser.advance_clicks == []
        assert crawler._wizard_advances == 0
        states = [r for r in read_records(work, "c1") if r["type"] == REC_PAGE_STATE]
        assert len(states) == 1


class _FlakyEntryBrowser(FakeBrowser):
    """The entry goto fails N times (the egress-fence reconfigure race: squid
    refuses the tunnel until it re-reads the just-written allowlist), then works."""

    def __init__(self, pages, start_url, *, failures):
        super().__init__(pages, start_url)
        self._failures = failures
        self.goto_attempts = 0

    async def goto(self, url):
        self.goto_attempts += 1
        if self._failures > 0:
            self._failures -= 1
            return NavResult(url=url, ok=False,
                             error="net::ERR_TUNNEL_CONNECTION_FAILED")
        return await super().goto(url)


def test_entry_goto_retries_through_the_fence_reconfigure_race():
    """A transiently-refused ENTRY navigation (live-observed squid allowlist
    reconfigure race) is retried and the crawl proceeds — instead of dying as an
    honest-but-avoidable 0-state crawl."""
    import tempfile
    with tempfile.TemporaryDirectory() as work:
        home = "https://app.example/home"
        pages = {home: FakePage(home, [
            _raw("link", "Privacy", href="https://app.example/privacy"),
        ], title="Home")}
        browser = _FlakyEntryBrowser(pages, home, failures=2)
        crawler = _build_crawler(browser, work, target_url=home)
        summary = asyncio.run(crawler.run())
        assert summary.stop_reason == STOP_COMPLETED
        states = [r for r in read_records(work, "c1") if r["type"] == REC_PAGE_STATE]
        assert any(s["location"] == home for s in states)
        assert browser.goto_attempts >= 3          # 1 + 2 retries


def test_entry_goto_still_fails_honestly_when_never_reachable():
    """A permanently unreachable entry stays an HONEST empty crawl after the
    bounded retries — the retry never fabricates reachability."""
    import tempfile
    with tempfile.TemporaryDirectory() as work:
        home = "https://app.example/home"
        browser = _FlakyEntryBrowser({}, home, failures=99)
        crawler = _build_crawler(browser, work, target_url=home)
        summary = asyncio.run(crawler.run())
        states = [r for r in read_records(work, "c1") if r["type"] == REC_PAGE_STATE]
        assert states == []                        # nothing fabricated
        assert browser.goto_attempts == 3          # 1 + exactly 2 retries


def test_network_calls_are_captured_scrubbed_and_query_dropped():
    """API/network mining (#6): XHR/fetch calls the app makes are recorded on the
    page_state as diagnostics evidence — query strings DROPPED (``has_query``
    preserves the fact), paths PII-scrubbed, non-http(s) targets excluded, values
    stringified so the schema's ``dict[str, str]`` can never refuse the bundle."""
    import tempfile
    with tempfile.TemporaryDirectory() as work:
        home = "https://app.example/home"
        pages = {home: FakePage(home, [
            _raw("textbox", "Search", tag="input", input_type="text"),
        ], title="Home")}
        browser = FakeBrowser(pages, home)
        browser.network_by_url[home] = [
            # a real API call whose QUERY carries PII — query dropped, path kept.
            {"method": "get",
             "url": "https://app.example/api/quote?email=jane@x.com&id=9",
             "status": 200, "resource_type": "xhr", "has_query": True,
             "request_mime": "application/json", "response_mime": "application/json",
             "response_bytes": "812", "timestamp_ms": 5},
            # a non-http scheme is chrome/noise, not API evidence — excluded.
            {"method": "GET", "url": "data:application/json,{}", "status": 200,
             "resource_type": "fetch"},
        ]
        crawler = _build_crawler(browser, work, target_url=home)
        asyncio.run(crawler.run())

        assert browser.network_drains >= 1
        states = [r for r in read_records(work, "c1") if r["type"] == REC_PAGE_STATE]
        calls = [c for s in states for c in s.get("network_calls", [])]
        assert len(calls) == 1  # the data: URL was dropped
        call = calls[0]
        assert call["method"] == "GET"
        assert call["url"] == "https://app.example/api/quote"     # query dropped
        assert call["has_query"] == "true"
        assert "email" not in call["url"] and "jane" not in call["url"]
        assert call["status"] == "200"                             # stringified
        assert call["response_mime"] == "application/json"


def test_network_normalizer_keeps_websocket_and_sse_evidence():
    """WebSocket/SSE capture (D): a ws(s) endpoint and an SSE stream are real-time
    API evidence — the normalizer keeps them (query dropped, ALL-string) alongside
    xhr/fetch, and still drops non-network schemes."""
    from app.crawler import _network_calls
    out = _network_calls([
        {"method": "WS", "url": "wss://app.example/live?token=abc", "status": "101",
         "resource_type": "websocket", "has_query": True},
        {"method": "GET", "url": "https://app.example/events", "status": "200",
         "resource_type": "sse", "response_mime": "text/event-stream"},
        {"method": "GET", "url": "https://app.example/api/x", "status": "200",
         "resource_type": "xhr"},
        {"method": "GET", "url": "blob:whatever", "status": "200", "resource_type": "other"},
    ])
    by_type = {c["resource_type"]: c for c in out}
    assert set(by_type) == {"websocket", "sse", "xhr"}          # blob dropped
    assert by_type["websocket"]["url"] == "wss://app.example/live"   # query dropped
    assert by_type["websocket"]["has_query"] == "true"
    assert by_type["sse"]["response_mime"] == "text/event-stream"


def test_schema_bundle_accepts_network_calls_additively():
    """The qe-central substrate accepts the new stream additively: a pre-#6
    manifest (no ``network_calls``) still validates (defaults to []), and a
    present stream rides verbatim — no factory contract was broken."""
    schema = _load_schema_module()

    def _bundle(page_extra: dict):
        page = {"sequence_index": 0, "location": "https://a.example/x",
                "first_seen_ms": 0, "last_seen_ms": 1}
        page.update(page_extra)
        return schema.ExplorationBundle.model_validate({
            "crawl_id": "c1", "target_url": "https://a.example/",
            "explorer_version": "t/1", "config_fingerprint": "fp",
            "frame_count": 0, "pages": [page],
        })

    assert _bundle({}).pages[0].network_calls == []  # backward-compatible default
    present = _bundle({"network_calls": [
        {"method": "GET", "url": "https://a.example/api", "status": "200"}]})
    assert present.pages[0].network_calls[0]["url"] == "https://a.example/api"


# ─── Shared manifest ↔ ExplorationBundle field-name contract ────────────────────


def _load_schema_module():
    """Load the REAL qe-central ExplorationBundle schema by file path.

    The package dir ``platform/qe-central`` is not importable (hyphen), so we
    load the standalone module (stdlib + pydantic only, no relative imports).
    """
    import sys
    root = Path(__file__).resolve().parents[3]
    schema_path = root / "platform" / "qe-central" / "app" / "substrate" / "schema.py"
    spec = importlib.util.spec_from_file_location("_qec_schema_under_test", schema_path)
    module = importlib.util.module_from_spec(spec)
    # Register BEFORE exec so the module's own forward refs (PageState↔ActionRecord)
    # resolve when a model is later instantiated (not just introspected).
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def _dc_fields(cls) -> set:
    return {f.name for f in dataclasses.fields(cls)}


def test_manifest_records_mirror_exploration_bundle_schema():
    schema = _load_schema_module()

    # AnchorBundle / AfterBundle — exact field-name mirror.
    assert _dc_fields(emit.AnchorRecord) == set(schema.AnchorBundle.model_fields)
    assert _dc_fields(emit.AfterRecord) == set(schema.AfterBundle.model_fields)

    # ActionRecord / PageState — emit records are a SUPERSET (mirrored substrate
    # fields + manifest-only routing keys the qe-central mapper ignores).
    action_fields = _dc_fields(emit.ActionRecord)
    for name in schema.ActionRecord.model_fields:
        assert name in action_fields, f"ActionRecord missing schema field {name!r}"

    page_fields = _dc_fields(emit.PageStateRecord)
    for name in schema.PageState.model_fields:
        assert name in page_fields, f"PageStateRecord missing schema field {name!r}"

    # ScreenshotRef: the shared identity fields mirror; png_base64 is staged as
    # ``path`` on the shared volume (documented R-5 deviation).
    shot_fields = _dc_fields(emit.ScreenshotRecord)
    assert {"frame_index", "timestamp_ms"}.issubset(shot_fields)
    assert "path" in shot_fields
