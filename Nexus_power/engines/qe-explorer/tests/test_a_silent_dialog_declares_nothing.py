"""A MODAL THAT SAYS NOTHING HAS DECLARED NOTHING.

`DECLARED_CONFIRMATION_RUNGS` is the set of rungs on which "the APPLICATION
ITSELF declared success", and `RUNG_NAVIGATION` is excluded from it with an
argument this module agrees with entirely: a URL change proves a click landed
somewhere, not that the somewhere is a confirmation.

WHAT WAS STILL MISSING. The same test was never applied to the DETAIL. A
transition could satisfy every conjunct — positive outcome, a declared rung, the
page moved — while carrying an EMPTY detail, i.e. while the application had said
nothing at all. The rung was doing the declaring, not the app.

MEASURED (OWASP Juice Shop, 2026-08-27, first crawl of it we have ever run):

    qec.wizard.confirmation url=http://127.0.0.1:3010/#/ rung=dialog detail=''
      - the application DECLARED this journey complete; the walk stops here

Nothing was declared. That modal is Juice Shop's WELCOME/cookie overlay, which
is on screen before any user has done anything. The walk stopped on it and the
whole crawl ended at 3 pages of a shop with a catalogue, a basket, a login and a
registration form — none of which were ever reached.

WHY THE EARLIER FIX DID NOT CATCH IT. d232c52 taught the platform that a modal
which ASKS something (a field, a re-offered commit verb) is a challenge, not a
receipt. A welcome overlay asks nothing and commits nothing — it is not a
challenge, so that guard passed it straight through. "No text, no declaration"
is the stronger rule and subsumes both: a receipt SAYS something.

WHAT IS ASSERTED HERE. The defect and the control, because a rule that rejects
every dialog would delete the completion path for every application that
confirms in a modal.
"""
from __future__ import annotations

import pytest

from app.boundary import (RUNG_ARIA_STATUS, RUNG_DIALOG, RUNG_TRANSITION_TEXT,
                          is_confirmation_landing)


@pytest.mark.parametrize("rung", [RUNG_DIALOG, RUNG_ARIA_STATUS,
                                  RUNG_TRANSITION_TEXT])
def test_a_declared_rung_with_no_text_is_not_a_declaration(rung):
    """THE DEFECT, across every rung that claims the app declared something.
    An empty detail means the application said nothing; the rung alone cannot
    do the declaring."""
    assert not is_confirmation_landing(
        outcome="confirmation", rung=rung, changed=True, detail=""), (
        f"an empty {rung} was treated as the application declaring success")
    assert not is_confirmation_landing(
        outcome="confirmation", rung=rung, changed=True, detail="   ")


@pytest.mark.parametrize("rung,detail", [
    (RUNG_DIALOG, "Order placed. Confirmation #AL-4471."),
    (RUNG_ARIA_STATUS, "Electronic Delivery Consent signed and retained in the vault."),
    (RUNG_TRANSITION_TEXT, "Your application was submitted successfully."),
])
def test_a_declared_rung_that_carries_the_apps_words_still_confirms(rung, detail):
    """THE CONTROL. Applications that confirm in a modal, a status region or in
    place must keep their completion path. If this goes red the cure removed the
    only route to a completed journey for most single-page applications."""
    assert is_confirmation_landing(outcome="confirmation", rung=rung,
                                   changed=True, detail=detail)


def test_the_other_conjuncts_are_unchanged():
    """The page must still have moved, and the outcome must still be positive —
    an empty detail is an ADDITIONAL requirement, never a replacement."""
    d = "Application submitted."
    assert not is_confirmation_landing(outcome="confirmation", rung=RUNG_DIALOG,
                                       changed=False, detail=d)
    assert not is_confirmation_landing(outcome="dom_changed", rung=RUNG_DIALOG,
                                       changed=True, detail=d)
    assert not is_confirmation_landing(outcome="confirmation", rung="navigation",
                                       changed=True, detail=d)
