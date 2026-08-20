"""M2.6 / T-CAP-03 — the crawl OPENS the page before it catalogues it.

The gap this closes. `isVisible()` is right to refuse a control inside a
collapsed accordion — it is not on the page, and cataloguing it would be a
capture-says-covered / replay-cannot-bind claim. The consequence was that a
question the application asks was never recorded by any crawl of it: the
catalogue was silently a catalogue of the *open* parts of an application.

Every assertion here runs against a REAL crawl of fixture 22 through the
production `PlaywrightBrowserPort` and the production `Crawler`, and reads the
`manifest.jsonl` the production emitter wrote. The fixture's own `expected.json`
pins the OTHER half — that those same fields are absent from the raw capture —
so the pair measures the difference the pass makes rather than the state of a
page.

What is deliberately NOT asserted: that any particular control was clicked. The
laws are about the CATALOGUE and the EVIDENCE, so an implementation that reaches
the same result another way passes.
"""
from __future__ import annotations

import json
import shutil
from typing import Any

import pytest

import _harness as H

pytestmark = [pytest.mark.browser, pytest.mark.playwright]

FIXTURE = "22-collapsed-disclosure"
CRAWL_OUT = H.HERE / "_crawl_out"

#: Behind a collapsed accordion and a closed <details>. The milestone's
#: acceptance criterion is that these are catalogued.
BEHIND_A_SHUT_DOOR = {
    "Beneficiary full name",
    "Beneficiary share percent",
    "Existing conditions",
}

#: On the page from the start. If these ever go missing the pass has taken
#: something away, which is strictly worse than never having run.
ALWAYS_ON_THE_PAGE = {"Full name", "Contact email", "Target cash value"}


def _crawl(pw, url: str) -> tuple[list[dict[str, Any]], Any]:
    """A real crawl of the fixture; returns (manifest records, the crawler)."""
    from app.auth import AuthWindow
    from app.crawler import Budget, Crawler, GuardContext
    from app.guard import load_refuse_pack
    from app.main import EXPLORER_VERSION, PlaywrightBrowserPort

    pack = load_refuse_pack(str(H.SERVICE_ROOT / "app" / "refuse_pack.yaml"))
    guard_ctx = GuardContext(
        refuse_pack=pack,
        auth_window=AuthWindow(max_requests=200, window_ms=120_000),
        attestation=None,
        submit_flow_approved=False,      # nothing here may cross a boundary
        idp_domains=frozenset(),
    )
    budget = Budget.from_dict({
        "max_states": 8, "max_actions": 60, "max_requests": 400,
        "max_duration_ms": 240_000,
    })
    work_dir = CRAWL_OUT / "expansion"
    shutil.rmtree(work_dir, ignore_errors=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    crawl_id = "cap03-expansion"
    crawler = Crawler(
        PlaywrightBrowserPort(pw.page, pw.context),
        crawl_id=crawl_id, tenant_id="cap03", target_url=url,
        work_dir=str(work_dir), refuse_pack=pack, budget=budget,
        explorer_version=EXPLORER_VERSION, guard_version=EXPLORER_VERSION,
        refuse_pack_version=pack.version, config_fingerprint="cap03",
        guard_context=guard_ctx, identity_seed="qec-cap03",
        observe_only=True,
    )
    pw.run(crawler.run())
    manifest = work_dir / crawl_id / "manifest.jsonl"
    assert manifest.exists(), f"the crawl wrote no manifest at {manifest}"
    records = [json.loads(line) for line in
               manifest.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert records, "the manifest is empty"
    return records, crawler


@pytest.fixture(scope="module")
def crawled(pw, fixture_server) -> tuple[list[dict[str, Any]], Any]:
    return _crawl(pw, fixture_server.url(FIXTURE))


def _states(records) -> list[dict[str, Any]]:
    return [r for r in records if r.get("type") == "page_state"]


def _signals(state) -> set[str]:
    return set((state.get("form_snapshot_signals") or {}))


def _clicked(state) -> list[str]:
    return [str(a.get("target_label") or "") for a in (state.get("actions") or [])
            if a.get("verb") == "click"]


# ── The acceptance criterion ────────────────────────────────────────────────

def test_a_field_behind_a_collapsed_accordion_is_catalogued(crawled) -> None:
    """THE MILESTONE'S ACCEPTANCE TEST, stated as the catalogue and not as a
    click: after the crawl, the questions the application asks inside a shut
    section are in it."""
    records, crawler = crawled
    catalogued: set[str] = set()
    for state in _states(records):
        catalogued |= _signals(state)
    missing = BEHIND_A_SHUT_DOOR - catalogued
    assert not missing, (
        f"the crawl catalogued {sorted(catalogued)} and left {sorted(missing)} "
        f"behind a shut door. A catalogue of the open parts of an application "
        f"is not a catalogue of the application.")
    assert crawler._expansions_opened >= 2, (
        f"expansions_opened={crawler._expansions_opened} — the fields arrived "
        f"without the pass opening anything, so this test is measuring "
        f"something other than what it claims to")


def test_the_expansion_never_costs_the_page_what_it_already_had(crawled) -> None:
    """Strictly additive, or the pass is a regression dressed as a feature."""
    records, _ = crawled
    best = max((_signals(s) for s in _states(records)), key=len, default=set())
    assert ALWAYS_ON_THE_PAGE <= best, (
        f"the richest catalogued state is missing "
        f"{sorted(ALWAYS_ON_THE_PAGE - best)} — opening a section took "
        f"something off the page")


def test_the_opens_are_recorded_where_a_replay_can_find_them(crawled) -> None:
    """A field that only exists once a section is open is UNBINDABLE unless the
    run that binds it opens the section first. Recording the field without
    recording the open is the exact capture-says-covered / replay-cannot-bind
    shape this harness exists to catch — so the opens must be actions on the
    same state, ahead of anything that touches the revealed fields."""
    records, _ = crawled
    state = max(_states(records), key=lambda s: len(_signals(s)))
    clicks = _clicked(state)
    assert "Beneficiary details" in clicks, (
        f"the state catalogues fields that only exist once 'Beneficiary "
        f"details' is open, and does not record opening it. Clicks on it: "
        f"{clicks}")
    assert "Medical history" in clicks, (
        f"same for the native <details>. Clicks: {clicks}")
    assert clicks.index("Beneficiary details") == 0, (
        f"the opens must lead the state's action list, so a generated flow "
        f"opens before it fills; got {clicks}")


# ── What the pass must refuse ───────────────────────────────────────────────

def test_the_expansion_pass_never_submits_an_application(crawled) -> None:
    """`#submit-app` declares aria-expanded="false" exactly like a real
    accordion header. The only thing between an expansion pass and a submitted
    application is the commit-word veto, so it is asserted rather than
    assumed."""
    records, _ = crawled
    for state in _states(records):
        clicks = _clicked(state)
        if "Submit application" not in clicks:
            continue
        # Navigation discovery may still probe it on a non-form state; what may
        # never happen is the EXPANSION pass reaching for it, and the expansion
        # actions are the ones that lead the list.
        assert clicks.index("Submit application") > 0, (
            "the expansion pass clicked a commit-labelled control")


def test_a_menu_opener_is_not_folded_into_the_form(crawled) -> None:
    """`#nav-more` is collapsed too, and its content is site navigation. Folding
    it in would put a nav fly-out into the catalogue of an application form."""
    records, _ = crawled
    for state in _states(records):
        assert "Help centre" not in _signals(state), (
            "a nav fly-out's contents were catalogued as controls of this page")


def test_an_already_open_section_is_left_open(crawled) -> None:
    """`#acc-contact` is aria-expanded="true". A pass that toggles every
    disclosure it can see would CLOSE it and catalogue fewer fields than doing
    nothing at all."""
    records, _ = crawled
    best = max(_states(records), key=lambda s: len(_signals(s)))
    assert "Contact email" in _signals(best), (
        "the field inside the already-open section is gone — the pass closed it")


# ── Tabs: a different page, recorded as one ─────────────────────────────────

def test_two_tab_panels_are_never_merged_into_one_state(crawled) -> None:
    """The panels are never on screen together, so a state holding both is a
    page nobody has ever seen and a script bound to it cannot run."""
    records, _ = crawled
    for state in _states(records):
        sig = _signals(state)
        assert not ({"Target cash value", "Term length years"} <= sig), (
            f"one state catalogues both tab panels: {sorted(sig)}")


def test_the_unselected_tab_panel_gets_a_state_of_its_own(crawled) -> None:
    """Refusing to merge is only honest if the panel is recorded SOMEWHERE —
    otherwise the fields behind the second tab are simply lost, which is the
    gap this milestone exists to close."""
    records, crawler = crawled
    assert any("Term length years" in _signals(s) for s in _states(records)), (
        f"no state catalogues the second tab's field; "
        f"tab_views_recorded={crawler._tab_views_recorded}")
    assert crawler._tab_views_recorded >= 1


def test_the_tab_state_records_the_click_that_reached_it(crawled) -> None:
    """A state nothing can navigate to is evidence of nothing."""
    records, _ = crawled
    state = next(s for s in _states(records)
                 if "Term length years" in _signals(s))
    assert "Term life" in _clicked(state), (
        f"the tab view does not record the click that opened it: "
        f"{_clicked(state)}")


def test_an_edge_leads_to_the_tab_state(crawled) -> None:
    records, _ = crawled
    state = next(s for s in _states(records)
                 if "Term length years" in _signals(s))
    edges = [r for r in records if r.get("type") == "edge"
             and r.get("to_state") == state.get("state_id")]
    assert edges, "the tab view is recorded with no edge reaching it"


# ── Cost ────────────────────────────────────────────────────────────────────

def test_a_page_with_nothing_collapsed_pays_nothing(pw, fixture_server) -> None:
    """The pass decides from captured evidence, so a page that declares no
    disclosure must not cost a single browser round trip. Asserted on a fixture
    that has none rather than by reading the code."""
    records, crawler = _crawl(pw, fixture_server.url("14-capture-attributes"))
    assert _states(records), "the control crawl recorded nothing"
    assert crawler._expansions_opened == 0
    assert crawler._expansions_skipped == 0
    assert crawler._tab_views_recorded == 0
