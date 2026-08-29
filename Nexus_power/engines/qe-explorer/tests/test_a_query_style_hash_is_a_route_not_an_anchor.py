"""An SPA whose routes are ``#key=value`` must not collapse to one URL.

MEASURED (Odoo 17, 2026-08-28). The crawl of a full ERP ended with TWO states:
the login page, and the authenticated home. It had a 150-state budget and used
two of it.

``_norm_url`` keeps a hash fragment only when it starts with ``/`` or ``!`` --
the Angular/Vue shape (``#/dashboard``, ``#!/dashboard``). Odoo routes look like

    /web#action=123&cids=1&menu_id=81

which starts with ``a``. The fragment was dropped, every route in the product
normalised to ``/web``, and so no menu click ever registered as a navigation.
The crawler then read those clicks as disclosures that "opened" nothing, tried
to undo them, failed, and discarded the page rather than fabricate one.

The existing docstring already names this exact failure -- "every SPA route
collapses to one URL and route-to-route navigation is invisible (the crawler
then never leaves the entry page)". The rule was right; its test for what
counts as a route was too narrow.

A fragment carrying STRUCTURED STATE (``=`` or ``&``) is a route. A bare
identifier (``#section``, ``#top``) is a scroll anchor. That distinction is
what these tests pin.
"""
from app.browser import _norm_url


def test_query_style_hash_route_is_kept():
    # The measured Odoo shape: two different views, one path.
    a = _norm_url("http://h/web#action=123&cids=1&menu_id=81")
    b = _norm_url("http://h/web#action=456&cids=1&menu_id=99")
    assert a != b, "two Odoo routes must not normalise to the same URL"
    assert a.endswith("#action=123&cids=1&menu_id=81")


def test_single_key_value_hash_is_a_route():
    assert _norm_url("http://h/web#action=123") != _norm_url("http://h/web")


def test_scroll_anchor_is_still_dropped():
    # The control. A bare identifier is cosmetic and must NOT read as a nav,
    # otherwise every in-page jump link becomes a false state.
    assert _norm_url("http://h/p#section") == _norm_url("http://h/p")
    assert _norm_url("http://h/p#top") == _norm_url("http://h/p")


def test_existing_slash_and_bang_routes_are_unchanged():
    assert _norm_url("http://h/a#/dash").endswith("#/dash")
    assert _norm_url("http://h/a#!/dash").endswith("#!/dash")


def test_trailing_slash_still_cosmetic():
    assert _norm_url("http://h/p/") == _norm_url("http://h/p")
