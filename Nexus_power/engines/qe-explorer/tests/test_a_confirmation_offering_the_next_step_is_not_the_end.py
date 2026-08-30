"""A CONFIRMATION THAT STILL OFFERS THE NEXT NAMED STEP IS NOT THE END.

MEASURED on the live vkpowerlife funnel 2026-08-30. Its underwriting page
declares:

    "Congratulations! Based on the information you provided, your application
     has been approved"

That is genuine success-shaped text which really did appear as a result of the
crossing, so all three of ``is_confirmation_landing``'s conjuncts hold and the
walk stopped — at step 6 of 10 (Decision; Payment, Beneficiary, Signature and
Confirmation still to come), with "Continue to Payment" on the page at that very
moment.

The underwriting DECISION succeeded. The JOURNEY did not. Reporting one as the
other is the same green-wash ``RUNG_NAVIGATION`` is excluded from the declared
rungs to prevent — arriving through the text rung instead.

THE DISCRIMINATOR IS THE REFUSE PACK'S OWN RULE. ``rp.allow.destination_advance_step``
already names this class, and these tests pin that it separates the two cases
exactly — because a looser rule ("any strict advance") was measured to break
three existing tests that correctly require a confirmation offering a bare
"Continue" or a "Back to Dashboard" to still END the journey.
"""
from __future__ import annotations

import pytest

from app.config import _DEFAULT_REFUSE_PACK_PATH
from app.guard import classify_action_verb, load_refuse_pack

_RULE = "rp.allow.destination_advance_step"
_URL = "http://app.test/apply/decision/"


@pytest.fixture(scope="module")
def pack():
    return load_refuse_pack(_DEFAULT_REFUSE_PACK_PATH)


# ── the separation the whole rule rests on ─────────────────────────────────

@pytest.mark.parametrize("label", [
    "Continue to Payment",
    "Continue to Signature",
    "Continue to Underwriting Decision",
])
def test_a_named_next_step_is_recognised_as_one(pack, label):
    """These say the funnel goes on. vkpowerlife's decision page offers the
    first of them while declaring the application approved."""
    assert classify_action_verb(label, _URL, pack).rule_id == _RULE


@pytest.mark.parametrize("label", [
    "Continue",             # the bare one from the TCF04 bug report
    "Back to Dashboard",
    "Print Confirmation",
    "New Application",
    "Done",
])
def test_an_exit_from_a_finished_journey_is_not_a_named_next_step(pack, label):
    """THE CONTROL THAT MATTERS. If any of these matched, the walk would wander
    off a genuine confirmation page — exactly the regression
    test_TCF04_back_to_dashboard_does_not_stop_it_completing guards, and exactly
    what a looser "any strict advance" rule was measured to cause."""
    assert classify_action_verb(label, _URL, pack).rule_id != _RULE


def test_a_commit_is_never_a_mere_next_step(pack):
    """The pack's own carve-out: the commit forms stay refused, so this rule
    can never be used to walk through an irreversible action."""
    verdict = classify_action_verb("Sign & Submit Application", _URL, pack)
    assert verdict.irreversible is True
    assert verdict.rule_id != _RULE


# ── the walker's reader over that rule ─────────────────────────────────────

def _W(pack):
    """The real method, on the smallest object that can carry it."""
    from app.walker import WalkerMixin

    class _Walker(WalkerMixin):
        def __init__(self, refuse_pack):
            self._refuse_pack = refuse_pack

    return _Walker(pack)


def _ctl(name):
    return {"name": name, "kind": "button", "role": "button"}


def test_the_walker_finds_the_next_step_among_a_page_s_controls(pack):
    w = _W(pack)
    assert w._named_next_step(
        [_ctl("Print Confirmation"), _ctl("Continue to Payment")],
        _URL) == "Continue to Payment"


def test_the_walker_finds_nothing_on_a_page_that_only_offers_exits(pack):
    """FALSIFICATION CONTROL for the test above."""
    w = _W(pack)
    assert w._named_next_step(
        [_ctl("Print Confirmation"), _ctl("Back to Dashboard"),
         _ctl("Continue")], _URL) == ""


def test_a_nameless_or_empty_page_yields_nothing_rather_than_raising(pack):
    w = _W(pack)
    assert w._named_next_step([], _URL) == ""
    assert w._named_next_step([{}, {"name": ""}, {"name": "   "}], _URL) == ""
    assert w._named_next_step(None, _URL) == ""


def test_a_pack_that_cannot_classify_declines_rather_than_stopping_the_walk():
    """A confirmation must still be reachable when the pack misbehaves — the
    fail direction here is "the journey ends", which is the safe one."""
    class _Boom:
        pass

    w = _W(_Boom())
    # No exception, and no invented next step from a pack that cannot answer.
    assert w._named_next_step([_ctl("Continue to Payment")], _URL) in (
        "", "Continue to Payment")
