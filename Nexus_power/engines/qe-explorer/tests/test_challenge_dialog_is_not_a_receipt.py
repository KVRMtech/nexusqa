"""A MODAL THAT ASKS FOR MORE INPUT HAS NOT CONFIRMED ANYTHING.

WHAT WAS WRONG.  ``classify_submit_after`` credits ``dialog_opened`` -- a bare
boolean -- as ``OUTCOME_CONFIRMATION``, and both rung assignments
(``forms.py``, ``walker.py``) then stamp ``RUNG_DIALOG``:

    confirm_rung = RUNG_DIALOG if observation.dialog_opened else ""

The only question asked is "did a modal appear".  ``RUNG_DIALOG`` is documented
as "a non-error dialog/modal opened -- a receipt or confirmation overlay", and
nothing distinguishes a receipt from a challenge.

Measured on a third-party carrier platform (LifeOps, 2026-08-27).  Clicking the
granted commit control ``Sign`` produced a real ``role=dialog`` containing

    "Sign Electronic Delivery Consent -- PIN confirmation is required to
     create an auditable electronic signature event."

with an input (``#sign-pin``) and the buttons ["Sign document", "Cancel"].  The
document's status was BYTE-IDENTICAL before and after the click: nothing was
signed.  The platform recorded outcome=confirmation, confirmed=true,
verified=True and logged "JOURNEY COMPLETED", with ``confirmation_detail``
empty -- it never read the sentence that says the action is still pending.

THE ARGUMENT ALREADY EXISTS IN THIS CODEBASE, one rung over.
``DECLARED_CONFIRMATION_RUNGS`` excludes ``RUNG_NAVIGATION`` and explains why:

    "A URL change proves that a click LANDED somewhere; it says nothing about
     whether the somewhere is a confirmation."

That reasoning transfers verbatim to modals -- a dialog opening proves a modal
appeared, not that it is a receipt -- and was never applied to them.

THE DISCRIMINATOR IS CHEAP AND STRUCTURAL.  A receipt tells you something; a
challenge asks you something.  A modal carrying an interactive field, or
re-offering the commit verb, has not completed the commit.

WHAT IS ASSERTED HERE.  Both directions, because a rule that refuses every
dialog is as broken as one that trusts every dialog:

  * a challenge modal is NOT a confirmation (the defect); and
  * a genuine receipt modal still IS one (the control).
"""
from __future__ import annotations

import pytest

from app.browser import (OUTCOME_CONFIRMATION, RawObservation,
                         classify_submit_after, is_challenge_dialog)


def _same_page(**kw) -> RawObservation:
    return RawObservation(url_before="https://app.test/x",
                          url_after="https://app.test/x", **kw)


# ── the pure discriminator ──────────────────────────────────────────────────

def test_a_modal_that_asks_for_a_field_is_a_challenge():
    """THE MEASURED CASE: LifeOps' e-signature PIN modal."""
    assert is_challenge_dialog(field_count=1,
                               button_labels=["Sign document", "Cancel"])


def test_a_modal_that_re_offers_the_commit_verb_is_a_challenge():
    """No field, but it still asks you to commit -- so the commit has not
    happened yet.  An "are you sure?" interstitial is not a receipt."""
    assert is_challenge_dialog(field_count=0,
                               button_labels=["Confirm payment", "Cancel"])


def test_a_receipt_modal_is_not_a_challenge():
    """THE CONTROL.  Nothing to fill, nothing to commit -- only an
    acknowledgement.  This must stay a confirmation or every application that
    confirms in a modal loses its completed journeys."""
    assert not is_challenge_dialog(field_count=0,
                                   button_labels=["Close", "Print receipt"])
    assert not is_challenge_dialog(field_count=0, button_labels=["Done"])
    assert not is_challenge_dialog(field_count=0, button_labels=[])


# ── the classifier ──────────────────────────────────────────────────────────

def test_a_challenge_dialog_is_not_a_terminal_confirmation():
    """THE DEFECT.  The commit is still pending; the crawl must not report a
    completed journey off the back of it."""
    outcome = classify_submit_after(_same_page(
        dialog_opened=True, dialog_is_challenge=True,
        dialog_detail="Sign Electronic Delivery Consent"))
    assert outcome.outcome != OUTCOME_CONFIRMATION, (
        "a modal demanding a PIN was scored as a completed commit")


def test_a_receipt_dialog_is_still_a_terminal_confirmation():
    """THE CONTROL for the classifier."""
    outcome = classify_submit_after(_same_page(
        dialog_opened=True, dialog_is_challenge=False,
        dialog_detail="Policy issued. Reference APP-2026-00174."))
    assert outcome.outcome == OUTCOME_CONFIRMATION
