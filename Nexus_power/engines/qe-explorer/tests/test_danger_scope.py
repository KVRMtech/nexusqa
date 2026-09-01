"""A URL-SCOPED REFUSE RULE MATCHES WHERE A CONTROL GOES, NOT WHERE IT SITS.

THE LIVE FAILURE, reproduced deterministically before anything was changed. The
PAGE url was passed as the control's danger url, so a rule declaring
``applies_to: [url_path]`` fired for EVERY actuator rendered on a matching page:

    rp.verb.underwrite  matches  \\bunderwriting\\b
    page /underwriting/new-business/new-application
    -> Back, the user avatar, the notification bell labelled "3", the wizard's
       own step tabs, and Continue itself all came back danger=critical
       (20 of 35 controls on one page)

That is not a safety property, it is a blind spot, and it produced BOTH of the
symptoms that were being chased separately:

  * every advance tier skips danger controls, so the funnel was unwalkable and
    the wizard never left step 1;
  * ``_tier3_candidates`` excludes danger controls too, so the candidate set was
    empty and the agent oracle recorded ZERO consultations fleet-wide — read as
    "the oracle is not wired up" when it was simply never given anything to
    judge.

One over-broad rule, two investigations.

WHAT IS NOT WEAKENED, and these tests exist mostly to hold that line: a control
whose LABEL names the irreversible act is still refused (that is what
``button_name`` is for), a link whose DESTINATION is a dangerous route is still
refused, nameless actuators were already unclassifiable, the EXPLORE-phase
network guard still blocks every mutation, and the submit tier still requires an
attestation plus an approval.
"""
from __future__ import annotations

import pytest

from app.config import Settings
from app.guard import load_refuse_pack
from app.inventory import build_inventory

_REFUSE = load_refuse_pack(Settings().refuse_pack_path)

_WIZARD = "https://admin.example/underwriting/new-business/new-application"
_HUB = "https://admin.example/underwriting/new-business"
_SAFE = "https://admin.example/dashboard/overview"


def _raw(role: str, name: str, **over):
    base = {
        "role": role, "name": name, "name_source": "content", "best_effort": False,
        "kind": role, "tag": over.pop("tag", "button"), "input_type": "",
        "options": [], "required": False, "disabled": False,
        "frame_selector": "", "testid": "", "css_hint": "", "value_committed": "",
        "landmark": {"role": "", "name": ""},
    }
    base.update(over)
    return base


def _danger(name: str, *, page: str, tag: str = "button", href: str = "") -> bool:
    role = "link" if tag == "a" else "button"
    ctrl = _raw(role, name, tag=tag)
    if href:
        ctrl["href"] = href
    return build_inventory([ctrl], _REFUSE, url=page)[0]["danger"]


# ── the contamination, gone ────────────────────────────────────────────────

@pytest.mark.parametrize("label", [
    "Continue", "Back", "Applicant", "Address & Employment",
    "Coverage", "Health", "Review & Submit",
    "3",                      # the notification bell
    "MC Margaret Chen",       # the user avatar
])
def test_ordinary_controls_are_not_dangerous_because_of_the_page_they_sit_on(label):
    """None of these DOES anything irreversible. Standing on a page whose path
    contains a dangerous word is not an act."""
    assert _danger(label, page=_WIZARD) is False
    assert _danger(label, page=_HUB) is False


def test_the_whole_page_is_no_longer_flagged():
    """20 of 35 controls came back critical-danger on the live wizard page."""
    page = [
        _raw("button", "Continue"), _raw("button", "Back"),
        _raw("button", "Applicant"), _raw("button", "3"),
        _raw("button", "MC Margaret Chen"),
    ]
    built = build_inventory(page, _REFUSE, url=_WIZARD)
    assert [c["danger"] for c in built] == [False] * 5


# ── what MUST still be refused ─────────────────────────────────────────────

@pytest.mark.parametrize("label", [
    "Submit to Underwriting", "Delete account", "Pay now",
    "Surrender policy", "Bind coverage",
])
def test_a_label_that_names_the_irreversible_act_is_still_refused(label):
    """button_name is the signal that survives, and it is the RIGHT one: the
    control says on its face what it does."""
    assert _danger(label, page=_SAFE) is True


def test_a_link_to_a_dangerous_ROUTE_is_still_refused_by_its_destination():
    """A control's own href IS a destination, so url-scoped rules apply to it —
    that is the reading the refuse pack intended."""
    assert _danger("Manage", page=_SAFE, tag="a", href="/account/delete") is True
    assert _danger("Settings", page=_SAFE, tag="a", href="/account/profile") is False


def test_a_dangerous_page_does_not_launder_a_dangerous_link():
    """Both halves at once: the page no longer contaminates, and the link's own
    destination still decides."""
    built = build_inventory([
        _raw("link", "Profile", tag="a", href="/account/profile"),
        _raw("link", "Close", tag="a", href="/account/delete"),
    ], _REFUSE, url=_WIZARD)
    assert [c["danger"] for c in built] == [False, True]


@pytest.mark.parametrize("href", ["javascript:void(0)", "#top", "mailto:a@b.c",
                                  "tel:+15555550100"])
def test_a_non_navigating_href_is_not_treated_as_a_destination(href):
    """These go nowhere, so they carry no destination to match a rule against."""
    assert _danger("Delete", page=_SAFE, tag="a", href=href) is True   # label still wins
    assert _danger("Options", page=_SAFE, tag="a", href=href) is False


def test_a_nameless_control_is_unchanged():
    """Already unclassifiable — a nameless actuator has no verb to read, and the
    a11y-weakness signal surfaces it separately."""
    assert _danger("", page=_WIZARD) is False
