"""A page the server said was broken is not a state of the application.

MEASURED on parabank.parasoft.com, 2026-09-02. A 20-minute crawl spent NINE of
its nineteen states inside /parabank/services/* — SOAP and WSDL endpoints that
answer 500 and 404:

    services/store-01        HTTP 500
    services/bank            HTTP 404
    services/LoanProcessor   HTTP 500

Each was navigated to, inventoried, recorded as a page_state and EXPANDED, so
its links were followed too. transfer.htm and billpay.htm — the two pages that
would have produced a real transaction — were never reached, and the crawl ended
with crossings=0.

The status was never the problem. ``NavResult.status`` has carried it all along
and NOTHING read it: a 500 was treated exactly like a 200. The budget was not
short, it was spent on pages the server had already declared broken.

WHY 401/403 ARE DELIBERATELY EXCLUDED. They are the auth wall — a meaningful
state this crawler exists to answer, with a whole module devoted to crossing it.
Skipping them would silently stop every gated application from being crawled,
which is a far worse defect than the one this closes. The parametrised control
below fails if anyone ever "tidies" them into the same branch.
"""

from __future__ import annotations

import pytest


#: The rule under test, kept as a pure predicate so it can be asserted without a
#: browser. It mirrors app/discovery.py exactly; the test below pins that.
def _skips(status: int) -> bool:
    return status >= 500 or status == 404


@pytest.mark.parametrize("status", [500, 502, 503, 504, 404])
def test_a_broken_page_is_skipped(status):
    assert _skips(status), (
        "HTTP %d means the server itself said this is not a usable page; "
        "inventorying and expanding it spends budget the application needs"
        % status
    )


@pytest.mark.parametrize("status", [401, 403])
def test_an_auth_wall_is_never_skipped(status):
    """CONTROL — the case that must NOT be swept into the same branch.

    401/403 is "you need to log in", which is exactly the state app/auth_flow.py
    exists to answer. Skipping it would make every gated application
    uncrawlable while looking like a tidy-up.
    """
    assert not _skips(status), (
        "HTTP %d is an auth wall, not a broken page — skipping it would stop "
        "the crawler entering any application that requires a login" % status
    )


@pytest.mark.parametrize("status", [0, 200, 201, 204, 301, 302, 304])
def test_a_working_page_is_never_skipped(status):
    """0 is 'no response object' (same-document navigation), not an error."""
    assert not _skips(status)


def test_the_predicate_matches_the_crawler():
    """The rule here must be the rule that actually runs.

    A local mirror that drifts from discovery.py would keep passing while the
    crawler did something else — the shape of blind verifier this repository
    keeps finding. So the source is read and the condition asserted present.
    """
    from pathlib import Path
    src = Path(__file__).resolve().parents[1] / "app" / "discovery.py"
    text = src.read_text(encoding="utf-8")
    assert "_status >= 500 or _status == 404" in text, (
        "discovery.py no longer carries the condition this file pins; either "
        "the rule moved (update this test with it) or it was removed"
    )
    assert "http_error_not_a_state" in text, (
        "the skip must stay announced — a silently dropped page reads later as "
        "'the application has nothing there'"
    )
