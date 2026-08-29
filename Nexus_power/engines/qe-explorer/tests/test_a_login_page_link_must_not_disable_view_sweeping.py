"""A LOGIN PAGE'S LINKS MUST NOT DISABLE VIEW-SWEEPING FOR THE APP BEHIND IT.

MEASURED (Odoo 17, 2026-08-28). A crawl of a full ERP — sales, invoicing,
inventory, contacts — ended with TWO states and ``stop_reason=completed``. Not
blocked, not out of budget (it used 2 of 150), and not refused: discovery simply
had nothing left to follow.

Odoo's authenticated UI reports ``anchored=0`` on every page; it navigates by
clicking menu entries that swap a client-side view. ``_sweep_view_navigation``
exists for exactly that shape and would have walked it. It never ran.

The gate required that NO link href had ever been enqueued:

    if not self._view_sweep_done and not self._link_hrefs_enqueued:

Odoo's LOGIN page — server-rendered, in front of the SPA — carries four:

    /web/signup   /web/reset_password   /web/database/manager   odoo.com

One of those was enough to set the counter, and the counter then disabled view
discovery for everything behind the login. That is the ordinary shape of
enterprise software, not an exotic one: a conventional login page fronting a
single-page application.

The rule is now ONCE PER CRAWL. Cost stays bounded where it always was — the
sweep observes the page and returns 0 unless it finds enough view-switching
controls, so a routed application pays a single observation, which is what its
docstring already promised.
"""
from __future__ import annotations

from app.discovery import DiscoveryMixin


class _Stub:
    """Carries only the attributes the predicate is allowed to consult."""
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _warranted(**kw) -> bool:
    return DiscoveryMixin._view_sweep_is_warranted(_Stub(**kw))


# ── the measured regression ────────────────────────────────────────────────

def test_the_odoo_shape_four_login_links_still_permits_the_sweep():
    """THE BUG. Four hrefs from the login page must not silence the ERP."""
    assert _warranted(_view_sweep_done=False, _link_hrefs_enqueued=4) is True


def test_one_href_is_enough_to_have_triggered_the_old_gate():
    assert _warranted(_view_sweep_done=False, _link_hrefs_enqueued=1) is True


# ── the bound that must survive ────────────────────────────────────────────

def test_the_sweep_runs_at_most_once_per_crawl():
    """THE CONTROL. Without this the empty frontier would sweep forever."""
    assert _warranted(_view_sweep_done=True, _link_hrefs_enqueued=0) is False
    assert _warranted(_view_sweep_done=True, _link_hrefs_enqueued=9) is False


def test_an_application_with_no_links_at_all_is_unchanged():
    """The LifeOps shape that motivated the sweep keeps working."""
    assert _warranted(_view_sweep_done=False, _link_hrefs_enqueued=0) is True


def test_the_predicate_does_not_consult_href_history():
    """The rule must be decidable without the counter existing at all."""
    assert _warranted(_view_sweep_done=False) is True
