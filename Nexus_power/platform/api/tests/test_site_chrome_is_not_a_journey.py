"""A sidebar is not a journey.

MEASURED on orangehrm.136-85-106-73.sslip.io, 2026-09-04. A Target-mode crawl of
the recruitment funnel — one app, one flow, the exact endpoint a client named —
completed and produced 15 scenarios. Thirteen of them were:

    Verify user can navigate from 'viewCandidates' to 'viewBuzz' via 'Buzz'
    Verify user can navigate from 'viewCandidates' to 'purgeEmployee' via 'PIM'
    ...                                                            (3 steps each)

Several named destinations OUTSIDE the crawl's own scope — defineLeavePeriod,
purgeEmployee, viewBuzz — because the sidebar links there even though the crawl
correctly never went. Exactly ONE of the fifteen was a journey a client would
recognise as worth having.

It is the same misjudgement as the boundary counts on the same application: 42 of
50 "crossings" were the sidebar and 6 were Cancel, against 2 real business
actions. The pipeline works; it had no idea navigation chrome is not behaviour.

THE SIGNAL. A control that appears on SEVERAL DIFFERENT PAGES is not part of any
one page's journey — that is what makes it chrome. Measured on that crawl the
separation is exact, and these numbers are the fixture below:

    13 sidebar links   on 2 of 2 click-bearing pages
    Add    (button)    on 1
    Save   (button)    on 1

WHY THE CONTROLS MATTER MORE THAN THE FIX. This rule DELETES tests. Tuned a
notch too aggressively it silently removes the real journeys a crawl found, and
the failure looks exactly like "the crawler didn't find anything" — which is the
shape of defect this repository keeps digging out. So the tests that must never
stop passing are the ones asserting a genuine action survives.
"""
from __future__ import annotations

import pytest

from app.services.test_factory.generator import (PageActionInput, _norm,
                                                 site_chrome_labels)


def _click(visit: str, label: str, kind: str = "link", idx: int = 0,
           dest: str = "/dest"):
    return PageActionInput(
        page_visit_id=visit, subaction_index=idx, verb="click",
        target_label=label, target_kind=kind, value=None,
        after_outcome="navigation",
        after_detail="https://app.example%s" % dest,
        navigated=True,
    )


#: The thirteen sidebar items, verbatim from the measured crawl.
SIDEBAR = ["Buzz", "Candidates", "Claim", "Dashboard", "Directory", "Leave",
           "Maintenance", "My Info", "OrangeHRM, Inc", "PIM", "Performance",
           "Time", "Vacancies"]


def _orangehrm_shape():
    """Two click-bearing pages, the sidebar on both, one real action on each."""
    actions = []
    for page in ("visit-a", "visit-b"):
        for i, label in enumerate(SIDEBAR):
            actions.append(_click(page, label, "link", i))
    actions.append(_click("visit-a", "Add", "button", 90))
    actions.append(_click("visit-b", "Save", "button", 91))
    return actions


def test_the_measured_sidebar_is_chrome():
    chrome = site_chrome_labels(_orangehrm_shape())
    missing = [s for s in SIDEBAR if _norm(s) not in chrome]
    assert not missing, (
        "these sidebar items were not recognised as chrome, so each would still "
        "become its own three-step 'test': %r" % missing
    )


@pytest.mark.parametrize("action", ["Add", "Save"])
def test_a_real_action_is_never_chrome(action):
    """CONTROL — the rule must not eat the journeys the crawl actually found.

    'Add' and 'Save' are the two controls on that crawl that a client would call
    a test. If either is classified as chrome the fix has destroyed the product's
    output while appearing to tidy it.
    """
    chrome = site_chrome_labels(_orangehrm_shape())
    assert _norm(action) not in chrome


def test_one_page_of_clicks_yields_no_chrome():
    """CONTROL — with a single page there is NO evidence of recurrence.

    Guessing here would delete the only journeys a short crawl found, and the
    result would read as 'the crawler found nothing'.
    """
    actions = [_click("only-visit", lbl, "link", i) for i, lbl in enumerate(SIDEBAR)]
    assert site_chrome_labels(actions) == set()


def test_a_repeated_action_on_a_large_crawl_is_not_chrome():
    """CONTROL — the ratio half of the rule, which the count alone cannot do.

    A genuine action appearing on 2 pages of 20 is a repeated action, not a nav
    bar. Without the ratio test this rule would quietly widen as crawls grow.
    """
    actions = []
    for n in range(20):
        actions.append(_click("visit-%d" % n, "Home", "link", 0))      # true chrome
    for n in range(2):
        actions.append(_click("visit-%d" % n, "Approve Claim", "button", 5))
    chrome = site_chrome_labels(actions)
    assert _norm("Home") in chrome, "a control on all 20 pages must be chrome"
    assert _norm("Approve Claim") not in chrome, (
        "an action on 2 of 20 pages is a repeated action, not a nav bar"
    )


def test_an_unlabelled_click_is_ignored():
    """A blank label cannot identify chrome, and must not become the key ''."""
    actions = _orangehrm_shape() + [_click("visit-a", "   ", "link", 99),
                                    _click("visit-b", "", "link", 99)]
    assert "" not in site_chrome_labels(actions)


def test_non_click_verbs_do_not_create_chrome():
    """CONTROL — a field FILLED on every page is not navigation chrome.

    Only clicks are considered. Without this, a search box present on every page
    would suppress the journeys reached from it.
    """
    fills = [PageActionInput(page_visit_id=p, subaction_index=0, verb="fill",
                             target_label="Search", target_kind="textbox",
                             value="x")
             for p in ("visit-a", "visit-b", "visit-c")]
    assert site_chrome_labels(fills) == set()


def test_the_journey_generator_drops_chrome_and_keeps_the_rest():
    """END TO END — the classifier is only worth anything if the generator uses it.

    A pure-function test would pass just as happily with the call site missing,
    which is exactly how the first attempt at the name_attr fix did nothing.
    """
    from app.services.test_factory.generator import (PageVisitInput,
                                                     generate_grounded_journeys)

    def _visit(vid, seq, path):
        return PageVisitInput(
            page_visit_id=vid, sequence_index=seq, location=path,
            url_host="app.example", url_path=path, url_query="",
            canonical_host="app.example", source="url_regex",
            form_snapshot={},
        )

    visits = [_visit("visit-a", 0, "/recruitment/viewCandidates"),
              _visit("visit-b", 1, "/recruitment/addCandidate")]

    def _nav(visit, label, kind, dest, idx):
        return _click(visit, label, kind, idx, dest)

    actions = []
    for page in ("visit-a", "visit-b"):
        for i, label in enumerate(SIDEBAR):
            actions.append(_nav(page, label, "link", "/module/%s" % label.lower()[:6], i))
    actions.append(_nav("visit-a", "Add", "button", "/recruitment/addCandidate", 90))

    cases = generate_grounded_journeys(
        artifact_id="artifact-1", page_visits=visits, page_actions=actions)

    names = [c.name for c in cases]
    sidebar_cases = [n for n in names if any(("via '%s'" % s) in n for s in SIDEBAR)]
    assert not sidebar_cases, (
        "sidebar navigations still became scenarios: %r" % sidebar_cases[:4]
    )
    assert any("Add" in n for n in names), (
        "the one REAL navigation was dropped along with the chrome — that is the "
        "over-block this rule must never commit. got %r" % names
    )
