"""B2 — the closed loop: a NAMED refusal drives one repair and one retry.

WHAT WAS MISSING, MEASURED ON SUMMIT-LIFE-CARRIER.  B1/B1-S taught the crawl
to NAME the field a silent commit refusal was about, in the application's own
words — and then the funnel still ended there.  The rejection reader walked
back to ``Face Amount ($)``, read "Minimum face amount is $10,000", recorded
it faithfully, and the crawl reported a crossing with no confirmation.  The
one thing the operator wants — the journey completed, with the value the
application itself dictated — needed a human to re-run the crawl with a seed.

The repair machinery already exists (:mod:`app.fill_engine.repair` — interpret
the message, tighten the constraints, regenerate) and already runs DURING a
fill.  A schema refusal happens at COMMIT, after every fill is done, so nothing
was left standing to act on what the reader had just learned.  This module is
the missing licence: it decides whether a refused commit may be repaired and
retried, exactly once.

THIS MODULE DECIDES; IT DOES NOT DRIVE.  Pure, like :mod:`app.step_back` and
for the same reason: no Playwright, no I/O, no clock.  The walker re-fills and
re-advances; the submit mixin owns the retry click through the SAME
``_execute_approved_submit`` path as any crossing (guard, journal, milestone —
none of it bypassed); :meth:`app.boundary.CrossingLedger.refund_app_refused`
owns the exactly-once arithmetic.

WHY A RETRY IS EVER SAFE, stated as invariants rather than hopes.  The
exactly-once ledger exists so an irreversible act is never performed twice.
A retry is permitted ONLY when the evidence shows the first act was never
performed at all — the application refused it before anything irreversible
could happen:

1. **The boundary is spent and the crossing is on the record.**  A retry is a
   second attempt at a recorded crossing, never a way around the reservation.
2. **The application refused it, and said so.**  At least one validation
   rejection was NAMED for this very commit (``rejected_on`` matches).  A
   commit that produced silence stays silent — retrying against no evidence is
   a search, and a search that succeeds by accident produces a green result
   nobody can explain.
3. **The commit did not navigate.**  Same-document only (fragment- and
   slash-insensitive, via :func:`app.step_back.same_document`): a commit whose
   far side is a new URL landed somewhere, and that landing is the record.
4. **No mutating request was allowed through during the crossing.**  A POST
   the guard allowed may have reached the application; whether it took effect
   is unknowable from here, so the retry is refused.  Zero allowed mutations
   plus a schema rejection rendered in the DOM is the summit shape: the
   handler was never reached.
5. **The repaired page is standing at the commit again.**  The walker re-fills
   the named fields where they live and walks forward; only when the commit
   control is visible and enabled again is there anything to retry.
6. **Once.**  ``max_retries`` (default 1, ``QEC_REFUSAL_RETRY_MAX``; 0
   disables) bounds this decision, and the ledger's refund — one per boundary,
   ever — bounds it again underneath.  Two independent brakes, because the
   failure mode of a loose retry loop is a form-submission spree against a
   client's application.

WHAT IT MAY CLAIM.  Nothing.  The retry's outcome is observed and recorded by
exactly the machinery that recorded the first attempt.  A retry that is also
refused mints its milestone with ``verified=False`` like any other, and there
is no third attempt.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .step_back import same_document

__all__ = [
    "DEFAULT_MAX_RETRIES",
    "RetryVerdict",
    "max_retries_configured",
    "may_repair_retry",
    "mutations_allowed_in",
    "repairable_rejections",
]

#: One retry per refused crossing.  One is the whole design: the repair either
#: satisfied the application's stated rules or it did not, and a second guess
#: would not be evidence-driven.  0 disables the mechanism entirely.
DEFAULT_MAX_RETRIES = 1


def max_retries_configured() -> int:
    """The operator's dial, read from the environment.

    Read here rather than from :class:`app.config.Settings` because that file
    is under concurrent edit by the fleet workstream this week; the semantics
    are identical (int, default :data:`DEFAULT_MAX_RETRIES`, 0 disables) and a
    malformed value fails CLOSED to 0 — a broken dial must not enable retries.
    """
    raw = str(os.environ.get("QEC_REFUSAL_RETRY_MAX",
                             str(DEFAULT_MAX_RETRIES))).strip()
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


@dataclass(frozen=True)
class RetryVerdict:
    """Whether one repair-and-retry may run, and WHY — never a bare bool.

    ``reason`` is logged on both polarities, the same doctrine as
    :class:`app.step_back.StepBackVerdict`: a mechanism that declines silently
    is indistinguishable from one that never ran.
    """

    permitted: bool
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {"permitted": self.permitted, "reason": self.reason}


def mutations_allowed_in(events: Sequence[Mapping[str, Any]]) -> int:
    """How many MUTATING requests the guard let through, in a drained window.

    Counts POST/PUT/PATCH/DELETE events.  Deliberately does NOT try to decide
    which of them "mattered": invariant 4 is fail-closed, so any allowed
    mutation in the crossing window refuses the retry.  Blocked requests are
    not in the drained stream (the guard aborted them before the wire), and a
    ``buffer_truncated`` marker counts as a mutation — a window we did not see
    all of is a window we cannot certify as mutation-free.
    """
    count = 0
    for event in events or ():
        if not isinstance(event, Mapping):
            continue
        if str(event.get("event") or "") == "buffer_truncated":
            count += 1
            continue
        if str(event.get("method") or "").strip().upper() in (
                "POST", "PUT", "PATCH", "DELETE"):
            count += 1
    return count


def repairable_rejections(
    records: Sequence[Mapping[str, Any]], *, trigger: str,
) -> list[Mapping[str, Any]]:
    """The named rejections THIS commit produced that a re-fill can act on.

    A record qualifies when it was rejected on exactly this trigger AND names a
    field.  A page-level rule with no field ("allocations must total 100%") is
    real evidence and drives nothing here — there is no single control a
    re-fill could honestly apply it to, and guessing one is the invention rung
    5 of the attribution ladder exists to prevent.

    The RULE text travels with each record; whether it is actionable (whether
    :func:`app.fill_engine.validation.interpret` extracts a constraint) is the
    driver's question, asked per field at re-fill time — this filter only
    decides which records are even candidates.
    """
    want = str(trigger or "").strip()
    if not want:
        return []
    out: list[Mapping[str, Any]] = []
    for record in records or ():
        if not isinstance(record, Mapping):
            continue
        if str(record.get("rejected_on") or "").strip() != want:
            continue
        if not str(record.get("field") or "").strip():
            continue
        out.append(record)
    return out


def may_repair_retry(
    *,
    crossing_spent: bool,
    confirmation_rung: str,
    url_before: str,
    url_after: str,
    named_for_trigger: int,
    mutations_allowed: int,
    repair_ready: bool,
    retries_taken: int,
    max_retries: int | None = None,
) -> RetryVerdict:
    """Gate the one repair-and-retry.  PURE.  Fail-closed on every axis.

    Every argument is something the caller OBSERVED, never something it
    intends: ``crossing_spent`` is the ledger's fact, ``confirmation_rung`` is
    the milestone's, ``named_for_trigger`` is what the rejection reader put on
    the record for THIS commit, ``mutations_allowed`` is counted off the
    drained network stream for the crossing window, ``repair_ready`` is the
    walker reporting that the re-filled wizard is standing at the commit
    control again.
    """
    budget = (max_retries_configured() if max_retries is None
              else int(max_retries))
    if budget <= 0:
        return RetryVerdict(False, "retries_disabled")
    if int(retries_taken) >= budget:
        return RetryVerdict(False, "retry_budget_spent")
    if not crossing_spent:
        # Invariant 1.  A retry is a second attempt at a RECORDED crossing.
        # Reaching here without a spent boundary means the caller is confused,
        # and a confused caller must not click anything.
        return RetryVerdict(False, "crossing_not_spent")
    if str(confirmation_rung or "").strip():
        # Invariant: the journey completed.  There is nothing to repair.
        return RetryVerdict(False, "confirmed")
    if not same_document(url_before, url_after):
        # Invariant 3.  The commit landed somewhere.  That landing is the
        # record; a retry would abandon it.
        return RetryVerdict(False, "navigated")
    if int(named_for_trigger) <= 0:
        # Invariant 2.  No named rejection, no retry — a retry must be CAUSED
        # by an observed rejection, the same rule the fill-time repair loop
        # holds (:mod:`app.fill_engine.repair`, rule 1).
        return RetryVerdict(False, "nothing_named")
    if int(mutations_allowed) > 0:
        # Invariant 4.  A mutating request was allowed through during the
        # crossing.  Whether the application acted on it is unknowable from
        # here, so the safe reading is that it may have — and a second click
        # could then be a second submission.
        return RetryVerdict(False, "mutation_observed")
    if not repair_ready:
        # Invariant 5.  Nothing was repaired, or the walk could not stand the
        # wizard back at its commit.  Clicking anyway would re-submit the very
        # values the application just refused.
        return RetryVerdict(False, "repair_not_ready")
    return RetryVerdict(True, "named_refusal_repaired")
