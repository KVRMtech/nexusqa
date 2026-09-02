"""The information-gain planner is inert on an app mounted under one prefix.

MEASURED on parabank.parasoft.com, 2026-09-02 — the first third-party
application put through this crawler.

``Frontier`` ranks by novelty so that "the FIRST item of every section is
visited before any section's second item", which under a finite state budget is
supposed to spend it on breadth-of-app-regions. That is a good rule and it did
nothing at all here.

``_section_signature`` takes the first two path segments of a ``url_template``,
and a template carries no scheme — so the HOST is the first segment and the rule
is really "host + first path segment". ParaBank mounts its whole application
under ``/parabank/``, so the marketing pages, the Swagger docs and the banking
transactions ALL signed as ``parabank.parasoft.com/parabank``. One section.
Novelty rank then merely increments, ordering degenerates to FIFO, and the docs
— which multiply with every click — took 86 of 101 states. The two real submits
were refused afterwards with "SUBMIT window closed, exceeded the request/time
budget": the traps did not merely waste effort, they SPENT the budget the
application itself needed.

``/app/``, ``/portal/`` and ``/web/`` are the same shape. Summit and vkpower are
not, which is exactly why this went unnoticed — the planner works on
applications whose structure happens to suit it and fails silently on the rest.

WHY THE DEFAULT STILL DOES NOTHING. frontier.py states its own constraint:
"ORDERING IS BEHAVIOUR ... any change to it changes every crawl's manifest."
So ``mount`` defaults to "" and is then byte-for-byte the original function.
``test_the_default_is_a_no_op`` is the control that keeps that promise honest —
without it this file would happily pass while every golden in the browser lane
quietly moved.
"""

from __future__ import annotations

import pytest

from app.frontier import Frontier, FrontierItem, _section_signature

# ── the measured shapes ──────────────────────────────────────────────────────

PARABANK_MOUNT = "parabank.parasoft.com/parabank"
PARABANK = [
    "parabank.parasoft.com/parabank/index.htm",
    "parabank.parasoft.com/parabank/about.htm",
    "parabank.parasoft.com/parabank/api-docs/index.html",
    "parabank.parasoft.com/parabank/overview.htm",
    "parabank.parasoft.com/parabank/transfer.htm",
    "parabank.parasoft.com/parabank/billpay.htm",
]
#: Summit's top-level paths differ, so its sections were always real.
SUMMIT = [
    "summitlife-admin.136-85-106-73.sslip.io/underwriting/new-business/new-application",
    "summitlife-admin.136-85-106-73.sslip.io/claims/reported/new-fnol",
    "summitlife-admin.136-85-106-73.sslip.io/dashboard/overview",
]


def test_a_single_mount_app_collapses_to_one_section_today():
    """The defect itself, pinned. If this ever returns >1 the fix has landed
    somewhere else and this file should be re-read, not deleted."""
    sections = {_section_signature(u) for u in PARABANK}
    assert sections == {PARABANK_MOUNT}, (
        "expected every ParaBank url to collapse into one section (the defect); "
        "got %r" % sorted(sections)
    )


def test_the_mount_makes_the_regions_distinguishable():
    sections = {_section_signature(u, PARABANK_MOUNT) for u in PARABANK}
    assert len(sections) >= 4, (
        "the mount-relative signature must separate the app's regions so the "
        "novelty planner can interleave them; got %r" % sorted(sections)
    )
    # The specific separation the crawl needed: docs must not share a section
    # with the banking pages, or they cannot be interleaved against each other.
    docs = _section_signature(
        "parabank.parasoft.com/parabank/api-docs/index.html", PARABANK_MOUNT)
    transfer = _section_signature(
        "parabank.parasoft.com/parabank/transfer.htm", PARABANK_MOUNT)
    assert docs != transfer


@pytest.mark.parametrize("url", PARABANK + SUMMIT)
def test_the_default_is_a_no_op(url):
    """CONTROL — the promise frontier.py makes about itself.

    ORDERING IS BEHAVIOUR: with no mount the signature must be byte-for-byte
    what it always was, or every golden in the browser lane moves silently.
    Re-derived here rather than imported so a change to the implementation
    cannot quietly redefine what "unchanged" means.
    """
    from urllib.parse import urlsplit

    def original(url_template: str) -> str:
        path = urlsplit(url_template or "").path or ""
        return "/".join([s for s in path.split("/") if s][:2])

    assert _section_signature(url) == original(url)
    assert _section_signature(url, "") == original(url)


def test_summit_is_unaffected_because_its_sections_already_worked():
    """The fix must not be sold as general when the failure was specific."""
    assert len({_section_signature(u) for u in SUMMIT}) == 3


def test_a_frontier_defaults_to_the_unchanged_signature():
    """A Frontier built the way every caller builds one today must not move."""
    assert Frontier()._section_mount == ""
    plain, mounted = Frontier(), Frontier(section_mount=PARABANK_MOUNT)
    for url in PARABANK:
        plain.push(FrontierItem(url=url, depth=0), key=url)
        mounted.push(FrontierItem(url=url, depth=0), key=url)
    # One saturated section vs several fresh ones — the ordering difference the
    # planner exists to produce.
    assert len(plain._section_counts) == 1
    assert len(mounted._section_counts) >= 4


def test_a_mount_that_does_not_match_changes_nothing():
    """A wrong or stale mount must degrade to today's behaviour, never to junk."""
    for url in SUMMIT:
        assert _section_signature(url, "some.other.host/app") == _section_signature(url)
