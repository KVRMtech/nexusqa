"""The step-back rejection reader - the POLICY half of B1-S.

WHAT WAS MISSING, MEASURED ON SUMMIT-LIFE-CARRIER.  The crawl crosses
``Submit Application`` under a named boundary grant, the crossing is spent, the
milestone is minted - and the milestone carries no confirmation rung, the
outcome is ``none``, zero ``/api/v1/`` calls fire, and the rejection reader,
standing on the page it crossed from, finds **nothing at all**.

That silence was verified in the application's own source and it is TRUE.  The
wizard is five steps in one ``<form>``; ``handleSubmit`` runs a zod resolver
over the whole schema before the submit handler is reached; a refusal populates
``formState.errors``; and each error renders through a ``<FormMessage/>`` that
lives inside its own field's ``<FormItem>``.  The review step where
``Submit Application`` lives declares no field and therefore renders no message
node.  Every refusal belongs to a field on an EARLIER step, and those steps are
unmounted::

    step 0  Applicant              <FormMessage/> x 7   <- the refusals live here
    step 1  Address & Employment   <FormMessage/> x 7
    step 2  Coverage               <FormMessage/> x 4
    step 3  Health                 <FormMessage/> x 4
    step 4  Review & Submit        (none)               <- the reader stood here

So the reader was not broken and the app was not silent.  **The message lives
where the field lives, and the reader has to go there.**

THIS MODULE DECIDES; IT DOES NOT DRIVE.  Everything here is pure: no Playwright
import, no I/O, no clock, no randomness.  It takes what was OBSERVED about a
crossing and about the controls on the page and returns a decision plus the
reason for it - the same split :mod:`app.page_lifecycle` and :mod:`app.browser`
already use, and for the same reason: a gate that can be unit-tested without a
browser is a gate someone can still audit in a year.

WHY THIS IS SAFE TO DO AT ALL, stated as invariants rather than hopes.  A
step-back moves the browser off the page a crossing just landed on, which is
the most evidence-sensitive moment in the whole crawl.  Five conditions gate
it, every one of them fail-closed:

1. **The boundary is already spent.**  This runs only AFTER a crossing has been
   reserved in the exactly-once ledger, journalled, and given its milestone.
   There is no path from here back to a second click on the commit.
2. **The crossing produced no confirmation.**  A journey that completed is
   never disturbed; only a crossing that landed on nothing is investigated.
3. **The anchored reader already ran and found nothing.**  Stepping back is the
   LAST resort, never the first - if the app named the field where it was
   refused, that is a better record than anything this can produce.
4. **The commit did not navigate.**  A crossing whose far side is a new URL is
   not the silent-refusal shape; stepping back from it would abandon a real
   landing page.  Same-document only.
5. **The control is a step-back and nothing else.**  Button kind, not disabled,
   not danger-flagged by the refuse pack, label a FULL-STRING match for
   :data:`app.vocab.BACK_RE`, and carrying no commit word and no advance word.
   "Back to Dashboard" leaves the funnel; "Roll Back Payment" is a mutation
   wearing a navigation word; both are refused by the full-string rule, and the
   commit/advance union is the belt to that braces so the guarantee survives
   the vocabulary growing.

WHAT IT MAY CLAIM.  A message read on a stepped-back page is attributed through
the SAME accessibility ladder as any other rejection
(:mod:`app.fill_engine.validation` - ``aria-errormessage`` / ``aria-invalid`` +
``validationMessage`` / error-node ids), so the app's own annotation is what
anchors it.  A transition claim needs a before-snapshot, and the reader was not
standing on this step when the commit was refused — but the WALK was, on its
way forward, and where it recorded the step's texts then
(``walker._note_step_texts``) that snapshot licenses B1's plain-text rung here
too: text present at the read that was absent when the walk left the step
appeared as a result of the refused commit.  With no snapshot the rung stays
withheld — anchored or nothing.  Records are labelled with ``steps_back``
either way, so a reader can weigh the claim.  Saying which of these you have
is the point.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

from .vocab import ADVANCE_RE, BACK_RE, COMMIT_RE

__all__ = [
    "DEFAULT_MAX_STEPS_BACK",
    "StepBackVerdict",
    "is_step_back_control",
    "may_step_back",
    "pick_step_back_control",
    "same_document",
]

#: How many times the reader may step back from one crossing.  Four is the
#: measured depth of the summit wizard's field steps (0-3) below its review
#: step, and a wizard deeper than its own funnel is not a shape this reader is
#: trying to serve.  Bounded because an unbounded loop over a "Back" that does
#: not move is a hang, and because every click is an action the crawl must own.
DEFAULT_MAX_STEPS_BACK = 4


def same_document(url_before: str, url_after: str) -> bool:
    """Did the commit leave the document it was clicked on?

    Compared WITHOUT the fragment, because a client-side wizard that writes
    ``#step-4`` into the location bar has not navigated in any sense that
    matters here, and WITHOUT a trailing slash, because ``/apply`` and
    ``/apply/`` are the same document served by every router in use.

    An empty ``url_after`` is treated as "did not move": the crossing recorded
    no new location, which is exactly the silent shape.  Guessing the other way
    would make the reader decline on the one case it exists for.
    """
    def _key(u: str) -> str:
        s = str(u or "").strip()
        s = s.split("#", 1)[0]
        return s.rstrip("/")

    after = _key(url_after)
    if not after:
        return True
    return _key(url_before) == after


@dataclass(frozen=True)
class StepBackVerdict:
    """Whether the reader may step back, and WHY - never a bare bool.

    ``reason`` is logged on both polarities.  A mechanism that declines
    silently is indistinguishable from a mechanism that never ran, and telling
    those two apart has already cost this project a diagnosis round more than
    once.
    """

    permitted: bool
    reason: str
    max_steps: int = DEFAULT_MAX_STEPS_BACK

    def as_dict(self) -> dict[str, Any]:
        return {"permitted": self.permitted, "reason": self.reason,
                "max_steps": self.max_steps}


def may_step_back(
    *,
    confirmation_rung: str,
    named_on_landing: int,
    url_before: str,
    url_after: str,
    crossing_spent: bool,
    max_steps: int = DEFAULT_MAX_STEPS_BACK,
) -> StepBackVerdict:
    """Gate the step-back read.  PURE.  Fail-closed on every axis.

    Every argument is something the caller OBSERVED, never something it
    intends: ``crossing_spent`` is the ledger's fact, ``confirmation_rung`` is
    the milestone's, ``named_on_landing`` is what the anchored reader returned
    standing on the crossing's own page.
    """
    if max_steps <= 0:
        return StepBackVerdict(False, "budget_zero", 0)
    if not crossing_spent:
        # Invariant 1.  Nothing may move the page before the boundary is
        # durably spent - a step-back that raced the ledger write could leave
        # a resumed run believing the commit never happened.
        return StepBackVerdict(False, "crossing_not_spent", max_steps)
    if str(confirmation_rung or "").strip():
        # Invariant 2.  The journey completed.  Leave it alone.
        return StepBackVerdict(False, "confirmed", max_steps)
    if named_on_landing > 0:
        # Invariant 3.  The app named the field where it was refused.  That
        # record is strictly better than anything a step-back can produce.
        return StepBackVerdict(False, "already_named", max_steps)
    if not same_document(url_before, url_after):
        # Invariant 4.  The commit went somewhere.  Whatever that page is, it
        # is the crossing's landing and the reader does not get to abandon it.
        return StepBackVerdict(False, "navigated", max_steps)
    return StepBackVerdict(True, "silent_same_document_refusal", max_steps)


def is_step_back_control(name: str) -> bool:
    """True for a label that says step-back and NOTHING else.

    The commit and advance unions are checked even though a full-string
    :data:`BACK_RE` match cannot contain either today.  They are here so that
    widening the back vocabulary later - "Back a step", "Previous page" - can
    only ever fail CLOSED, rather than quietly admitting a label that grew a
    commit word.
    """
    n = str(name or "").strip()
    if not n or not BACK_RE.match(n):
        return False
    return not (COMMIT_RE.search(n) or ADVANCE_RE.search(n))


def pick_step_back_control(
    controls: Sequence[Mapping[str, Any]],
) -> tuple[Optional[Mapping[str, Any]], list[str]]:
    """Choose the control that steps one page back, plus a verdict per reject.

    Returns ``(pick, verdicts)``.  ``verdicts`` is the audit trail: one short
    ``label:reason`` for every control passed over, so a decline can be read
    from the log instead of reproduced in a debugger.  This is the same shape
    ``_commit_subform_to_unblock`` adopted after a silent decline there looked
    exactly like a mechanism that had never run.
    """
    verdicts: list[str] = []
    for control in controls or ():
        name = str(control.get("name") or "").strip()
        if control.get("kind") != "button":
            if name:
                verdicts.append(f"{name[:24]}:not-button")
            continue
        if not name:
            verdicts.append("(nameless)")
            continue
        if control.get("disabled"):
            verdicts.append(f"{name[:24]}:disabled")
            continue
        if control.get("danger"):
            # The refuse pack's verdict is final here.  A danger-flagged
            # control never becomes clickable because its label happens to
            # read "Back".
            verdicts.append(f"{name[:24]}:danger")
            continue
        if not BACK_RE.match(name):
            verdicts.append(f"{name[:24]}:not-back")
            continue
        if COMMIT_RE.search(name) or ADVANCE_RE.search(name):
            verdicts.append(f"{name[:24]}:commit-or-advance")
            continue
        return control, verdicts
    return None, verdicts
