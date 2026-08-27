"""A REFUSAL IN A POLITE LIVE REGION IS STILL A REFUSAL.

WHAT WAS WRONG.  ``RawObservation.confirmation_detail`` is documented as "a
grounded, POSITIVE success/confirmation live-region text", and
:func:`classify_submit_after` credits its mere PRESENCE as
``OUTCOME_CONFIRMATION``.  Nothing ever checked that the text was positive.

The producer is :meth:`PlaywrightBrowserPort.status_texts`, which reads
``[role=status]`` and ``[aria-live=polite]`` wholesale.  Its docstring states
the assumption the whole design rests on -- that ``role=alert`` carries errors
and ``role=status`` carries successes -- and names the consequence of the
assumption failing: "an error read as a confirmation is the one
misclassification that turns a failed submit into a green journey."

That assumption is false, and not rarely.  ``role=status`` is an ARIA live
region with implicit ``aria-live=polite``; it is the CORRECT and considerate
markup for a non-interrupting form-validation message, and applications use it
for exactly that.  Measured on a third-party carrier platform (LifeOps,
2026-08-27), a failed sign-in renders

    <div role="status" class="message error">Invalid member ID or PIN.</div>

``error_texts`` -- which reads only ``[role=alert]`` and
``[aria-live=assertive]`` -- returned ``[]``, so ``error_detail`` was empty and
the guard clause that exists to stop precisely this ("error is checked before
confirmation so a rejected submit is never green-washed") never fired.  The
same sentence arrived as ``confirmation_detail`` and the classifier returned
``confirmation``.  A refused login was scored as a completed submit.

THE FIX IS POLARITY, NOT PLUMBING.  Reading more selectors would not settle it:
the two roles do not partition the world into errors and successes, so the
polarity has to come from the text the application actually wrote.  A
rejection-shaped live region is an ERROR whichever role carries it.

WHAT IS ASSERTED HERE.  Both directions, because a rule that only ever refuses
is as broken as one that only ever confirms:

  * a rejection in a polite region is classified ``error`` (the defect); and
  * a genuine same-page success in the SAME region is still ``confirmation``
    (the control -- proof the fix discriminates rather than blanket-refuses).
"""
from __future__ import annotations

import pytest

from app.browser import (OUTCOME_CONFIRMATION, OUTCOME_ERROR, RawObservation,
                         classify_submit_after)

#: Verbatim from the live application, not paraphrased.
LIFEOPS_LOGIN_REFUSAL = "Invalid member ID or PIN."
LIFEOPS_MFA_REFUSAL = "Invalid verification code."
LIFEOPS_FIELD_REFUSAL = "Correct the highlighted fields before continuing."


def _same_page(**kw) -> RawObservation:
    """An observation with NO url change, so the classifier reaches the
    error/confirmation branches rather than short-circuiting on navigation."""
    return RawObservation(url_before="https://app.test/x",
                          url_after="https://app.test/x", **kw)


@pytest.mark.parametrize("refusal", [LIFEOPS_LOGIN_REFUSAL,
                                     LIFEOPS_MFA_REFUSAL,
                                     LIFEOPS_FIELD_REFUSAL])
def test_a_rejection_in_a_polite_region_is_an_error_not_a_confirmation(refusal):
    """THE DEFECT.  Text the application wrote to REFUSE the submit arrives as
    ``confirmation_detail`` because it sat in ``role=status``.  It must not be
    credited as a terminal success."""
    outcome = classify_submit_after(_same_page(confirmation_detail=refusal))
    assert outcome.outcome == OUTCOME_ERROR, (
        f"a refusal was scored {outcome.outcome!r}: {refusal!r}")
    assert not outcome.navigated


@pytest.mark.parametrize("success", [
    "Application submitted. Reference APP-2026-00174.",
    "Your payment was received.",
    "Coverage step ready.",
    "Policy issued successfully.",
])
def test_a_genuine_same_page_success_is_still_a_confirmation(success):
    """THE CONTROL.  The fix must discriminate on polarity, not simply stop
    trusting ``role=status``.  If this goes red the cure is worse than the
    disease: every single-page application that confirms in place -- the
    majority -- loses its only route to a completed journey."""
    outcome = classify_submit_after(_same_page(confirmation_detail=success))
    assert outcome.outcome == OUTCOME_CONFIRMATION, (
        f"a real confirmation was downgraded to {outcome.outcome!r}: {success!r}")


def test_an_explicit_error_region_still_outranks_a_status_region():
    """Unchanged precedence: when the application marks an error properly, that
    reading wins regardless of what any polite region says."""
    outcome = classify_submit_after(_same_page(
        error_detail="Payment declined.",
        confirmation_detail="Application submitted."))
    assert outcome.outcome == OUTCOME_ERROR
    assert outcome.detail == "Payment declined."
