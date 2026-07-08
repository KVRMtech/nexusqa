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
from app.crawler import (
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


def _build_crawler(port, work_dir, *, budget=None, target_url="https://app.example/home"):
    guard_ctx = GuardContext(refuse_pack=_REFUSE_PACK)
    return Crawler(
        port,
        crawl_id="c1", tenant_id="t1", target_url=target_url, work_dir=str(work_dir),
        refuse_pack=_REFUSE_PACK, budget=budget or Budget(rate_per_s=0),
        explorer_version="test/1.0", guard_version="test", refuse_pack_version=_REFUSE_PACK.version,
        config_fingerprint="fp", guard_context=guard_ctx,
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


# ─── Shared manifest ↔ ExplorationBundle field-name contract ────────────────────


def _load_schema_module():
    """Load the REAL qe-central ExplorationBundle schema by file path.

    The package dir ``platform/qe-central`` is not importable (hyphen), so we
    load the standalone module (stdlib + pydantic only, no relative imports).
    """
    root = Path(__file__).resolve().parents[3]
    schema_path = root / "platform" / "qe-central" / "app" / "substrate" / "schema.py"
    spec = importlib.util.spec_from_file_location("_qec_schema_under_test", schema_path)
    module = importlib.util.module_from_spec(spec)
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
