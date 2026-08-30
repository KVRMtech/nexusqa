"""THE BOUNDED VALIDATION-REPAIR LOOP.

    generate → fill → read the verdict → interpret it → generate BETTER → retry

The engine this replaces had no arrow back.  A value the application rejected
ended the field, the field ended the page, and the page ended the walk — and the
crawl reported the number of fields it had ATTEMPTED, which is the metric that
made all of this look like it was working.

TWO RULES GOVERN EVERY RETRY, and they are what separate a repair loop from a
retry loop:

  1. **A retry must be caused by an observed rejection.**  No signal, no retry.
     A field that filled cleanly is done; a field that failed for a reason we
     could not read is reported as failed, because trying a different value
     against no evidence is a search, and a search that succeeds by accident
     produces a green result nobody can explain.

  2. **A retry must change something the rejection named.**  Every attempt
     records the signal that caused it and the constraint it therefore tightened
     (:class:`RepairAttempt.reason`).  If interpreting the message yields nothing
     actionable, the loop STOPS rather than trying the same class of value again
     — and it says so.

CONVERGENCE IS STRUCTURAL, not a hope about the budget.  Each attempt folds the
interpreted hint into the constraint set, so the candidate space only ever
shrinks; and a value the loop has already tried is never tried again.  With a
default budget of three attempts a form that declares its rules honestly is
satisfied on the first or second, and one that does not is abandoned with a
recorded explanation instead of a loop.

THE DRIVER IS INJECTED.  :class:`FillDriver` is the whole browser surface this
loop needs — one method to commit a value and one to read back the verdict — so
the entire architecture is testable against a fake, and the loop itself contains
no Playwright, no selectors and no I/O.
"""
from __future__ import annotations

import inspect

import logging
from dataclasses import dataclass, field, replace
from typing import Any, Awaitable, Callable, Mapping, Optional, Protocol, Sequence

from . import constraints as C
from .validation import ConstraintHint, ValidationSignal, interpret

logger = logging.getLogger(__name__)

__all__ = [
    "RepairAttempt", "RepairOutcome", "RepairBudget", "FillDriver",
    "FillVerdict", "repair_loop", "tighten",
    "STOP_ACCEPTED", "STOP_BUDGET", "STOP_NO_SIGNAL", "STOP_NOT_ACTIONABLE",
    "STOP_NO_BETTER_VALUE", "STOP_REPEATED_VALUE", "STOP_NOT_REPAIRABLE",
    "STOP_TRANSIENT_BUDGET", "RetryPolicy", "FAILURE_TRANSIENT",
    "FAILURE_PERMANENT", "classify_failure",
]

STOP_ACCEPTED = "accepted"
STOP_BUDGET = "retry_budget_exhausted"
STOP_NO_SIGNAL = "rejected_without_a_readable_reason"
STOP_NOT_ACTIONABLE = "rejection_named_nothing_to_change"
STOP_NO_BETTER_VALUE = "no_value_satisfies_the_tightened_constraints"
STOP_REPEATED_VALUE = "the_only_remaining_value_was_already_rejected"
#: The application rejected the value and this field's value may NOT be
#: regenerated, because it did not come from the generator — it came from a
#: persona, recalled data, a credential or an MFA code.  Distinct from
#: ``STOP_NO_BETTER_VALUE``, which means the generator was ASKED and had nothing.
#: Conflating them was a real defect: a rejected Security PIN was reported as
#: "no value satisfies the tightened constraints" when no value had been sought.
STOP_NOT_REPAIRABLE = "value_is_provenance_locked_and_must_not_be_regenerated"
#: A transient, page-race failure kept recurring until the transient budget ran
#: out.  The value was never the problem and was never changed.
STOP_TRANSIENT_BUDGET = "transient_failure_persisted_across_every_retry"

#: How a mechanical failure is CLASSIFIED, which decides whether retrying the
#: identical value can possibly help.
FAILURE_TRANSIENT = "transient"
FAILURE_PERMANENT = "permanent"

#: Substrings that identify a failure as a RACE WITH THE PAGE rather than a fact
#: about the control.  Deliberately a small, closed list of things a browser
#: automation layer says when the DOM moved under it: each one describes a state
#: that a later moment can differ from, which is precisely what makes re-issuing
#: the SAME value a sane act rather than a hopeful one.
#:
#: Everything not on this list is PERMANENT.  Fail-closed on purpose — an
#: unrecognised failure retried three times is three times the wall clock for
#: the same answer, and on a crawl that is a budget the rest of the funnel
#: needed.
_TRANSIENT_MARKERS: tuple[str, ...] = (
    "not attached", "detached", "stale", "element is not attached",
    "timeout", "timed out", "intercept", "navigating", "navigation",
    "context was destroyed", "execution context", "target closed",
    "element is outside of the viewport", "not stable",
)


def classify_failure(mechanical: str) -> str:
    """Is this failure worth trying again with the SAME value?

    The question is never "did it fail" but "was the failure ABOUT the value".
    A detached element, an in-flight navigation and a settle timeout are all the
    page moving while we typed; the value was never examined.  A control that
    took the keystrokes and held something else is a fact about the control, and
    a second identical attempt reproduces it exactly.
    """
    text = str(mechanical or "").strip().lower()
    if not text:
        return FAILURE_PERMANENT
    return (FAILURE_TRANSIENT
            if any(marker in text for marker in _TRANSIENT_MARKERS)
            else FAILURE_PERMANENT)


@dataclass(frozen=True)
class RepairBudget:
    """How hard the loop is allowed to try.

    ``attempts`` counts TOTAL commits including the first, so ``3`` means one
    generation and at most two repairs.  Bounded on purpose: an unbounded loop
    against an application that rejects everything is a denial of service we
    aim at our own crawl."""

    attempts: int = 3

    def __post_init__(self) -> None:
        if self.attempts < 1:
            object.__setattr__(self, "attempts", 1)


@dataclass(frozen=True)
class RetryPolicy:
    """How a TRANSIENT failure is retried — a separate budget from the repair one.

    THE TWO BUDGETS ANSWER DIFFERENT QUESTIONS and must not share a number.
    ``RepairBudget.attempts`` bounds how many DIFFERENT VALUES the loop may try
    against an application that keeps rejecting them; this bounds how many times
    the SAME value is re-issued after the page moved under it.  A field can
    legitimately consume one of each: a detached-element retry that then commits
    a value the app rejects has used one transient retry and one repair attempt.

    BACKOFF IS EXPONENTIAL AND WITHOUT JITTER.  Jitter is the usual right answer
    and is wrong here: this crawl's evidence is replayed and compared against
    goldens, so identical inputs must produce an identical timing sequence.  The
    thing backoff protects against — a re-render storm we are racing — is
    unaffected by whether the wait is randomised.
    """

    #: TOTAL commits of the same value, including the first.  ``1`` disables
    #: transient retry entirely and is the pre-Gate-1 behaviour exactly.
    transient_attempts: int = 3
    #: Wait before the FIRST retry, in milliseconds.
    backoff_ms: int = 50
    #: Multiplier applied to the wait after each retry.
    backoff_factor: float = 2.0
    #: Ceiling, so a long budget cannot turn into a long sleep.
    max_backoff_ms: int = 400

    def __post_init__(self) -> None:
        if self.transient_attempts < 1:
            object.__setattr__(self, "transient_attempts", 1)
        if self.backoff_ms < 0:
            object.__setattr__(self, "backoff_ms", 0)
        if self.backoff_factor < 1.0:
            object.__setattr__(self, "backoff_factor", 1.0)
        if self.max_backoff_ms < self.backoff_ms:
            object.__setattr__(self, "max_backoff_ms", self.backoff_ms)

    def delay_ms(self, retry_index: int) -> int:
        """Wait before retry ``retry_index`` (1-based).  Deterministic."""
        if retry_index < 1:
            return 0
        delay = float(self.backoff_ms) * (self.backoff_factor ** (retry_index - 1))
        return int(min(delay, float(self.max_backoff_ms)))


@dataclass(frozen=True)
class FillVerdict:
    """What the application said about one committed value.

    ``committed`` is what the control ACTUALLY HOLDS after the fill, read back
    from the live control — never what we asked it to hold.  ``signals`` are the
    control-scoped validity signals from :mod:`app.fill_engine.validation`;
    an empty list with ``accepted=True`` is a clean fill."""

    accepted: bool
    committed: Optional[str] = None
    signals: tuple[ValidationSignal, ...] = ()
    #: Set when the fill itself could not be performed (the widget refused the
    #: verb, the element vanished).  Distinct from a value the app rejected: one
    #: is our problem and the other is the application's answer.
    mechanical_failure: str = ""


@dataclass(frozen=True)
class RepairAttempt:
    """One commit, and WHY this value rather than the last one.

    ``reason`` is the sentence that makes the retry legible.  It quotes the
    application's own message and names the constraint the loop tightened as a
    result, so a reader can check the inference rather than trust it."""

    attempt: int
    value: Optional[str]
    accepted: bool
    reason: str
    signals: tuple[ValidationSignal, ...] = ()
    committed: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "attempt": self.attempt,
            "accepted": self.accepted,
            "reason": self.reason[:300],
            "signals": [s.as_dict() for s in self.signals],
        }


@dataclass(frozen=True)
class RepairOutcome:
    """The end of one field's story."""

    accepted: bool
    value: Optional[str]
    attempts: tuple[RepairAttempt, ...] = ()
    stop_reason: str = STOP_ACCEPTED
    committed: Optional[str] = None
    #: Commits spent re-issuing the SAME value after a transient page race.
    #: Reported separately from ``attempts`` because they are not repairs: no
    #: value changed, so counting them as repair attempts would make the
    #: repair-success rate a measurement of page flakiness.
    transient_retries: int = 0

    @property
    def repaired(self) -> bool:
        """True when the field was accepted, but not on the first attempt — the
        numerator of the repair-success metric."""
        return self.accepted and len(self.attempts) > 1

    @property
    def first_pass(self) -> bool:
        return self.accepted and len(self.attempts) == 1

    def explanation(self) -> str:
        """The whole field, in one readable paragraph."""
        lines = [f"attempt {a.attempt}: {a.reason}" for a in self.attempts]
        lines.append(f"outcome: {self.stop_reason}")
        return "; ".join(lines)

    def as_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "first_pass": self.first_pass,
            "repaired": self.repaired,
            "attempt_count": len(self.attempts),
            "transient_retries": self.transient_retries,
            "stop_reason": self.stop_reason,
            "attempts": [a.as_dict() for a in self.attempts],
        }


class FillDriver(Protocol):
    """The whole browser surface the loop needs.

    Two methods, deliberately: one that commits a value and one that reads the
    verdict.  Everything else — locators, ladders, settle budgets — belongs to
    the layer that implements this, which is what keeps the loop itself pure."""

    async def commit(self, control: Mapping[str, Any], value: str) -> FillVerdict:
        """Type/select/check ``value`` and report what the control now holds."""
        ...


#: A generator callable: given the (possibly tightened) constraints and the set
#: of values already refused, produce the next value to try — or ``None`` when
#: nothing satisfies them.  Injected so the loop never has to know about
#: personas or semantics.
Regenerate = Callable[..., Optional[str]]
#: ``regenerate(tightened, refused, rejection="")`` — the third argument is the
#: application's OWN rejection sentence, verbatim. A deterministic generator
#: needs only the tightened constraints and may ignore it; a model does far
#: better with the words than with a Constraints repr, and passing the repr as
#: if it were the message is how the LLM rung was first wired. Optional-keyword
#: so every existing two-argument regenerator keeps working unchanged.


def tighten(cons: C.Constraints, hint: ConstraintHint) -> C.Constraints:
    """Fold what the application just said into what we already knew.

    Only ever NARROWS.  A message asking for at least 18 raises a minimum; it
    never lowers a maximum the DOM declared, because the DOM's declaration and
    the message are both the application's own words and the intersection is the
    only set that satisfies both.  That monotonicity is what makes the loop
    converge rather than oscillate between two rules."""
    updates: dict[str, Any] = {}
    if hint.minimum is not None:
        updates["minimum"] = (hint.minimum if cons.minimum is None
                              else max(cons.minimum, hint.minimum))
    if hint.maximum is not None:
        updates["maximum"] = (hint.maximum if cons.maximum is None
                              else min(cons.maximum, hint.maximum))
    if hint.minlength is not None:
        updates["minlength"] = (hint.minlength if cons.minlength is None
                                else max(cons.minlength, hint.minlength))
    if hint.maxlength is not None:
        updates["maxlength"] = (hint.maxlength if cons.maxlength is None
                                else min(cons.maxlength, hint.maxlength))
    if hint.exact_length is not None:
        updates["minlength"] = hint.exact_length
        updates["maxlength"] = hint.exact_length
    if hint.code == C.CODE_REQUIRED:
        updates["required"] = True
    if hint.wants_type and not cons.input_type:
        # The message named a KIND the control never declared — "enter a valid
        # email address" on a bare text input.  Adopting it makes the generator's
        # own violation check reject the value we already know is wrong.
        if hint.wants_type in ("email", "url", "number", "date"):
            updates["input_type"] = hint.wants_type
    if not updates:
        return cons
    return replace(cons, declared=True, **updates)


def _reason_for(signal: ValidationSignal, hint: ConstraintHint,
                before: C.Constraints, after: C.Constraints) -> str:
    """The sentence that justifies the next value.

    Names the evidence (which signal, from which anchoring rung), what it was
    read to mean, and the constraint that therefore changed.  A reader who
    disagrees with the inference can see exactly where it happened."""
    changed: list[str] = []
    for attribute, label in (("minimum", "min"), ("maximum", "max"),
                             ("minlength", "minlength"), ("maxlength", "maxlength"),
                             ("input_type", "input_type")):
        was, now = getattr(before, attribute), getattr(after, attribute)
        if was != now:
            changed.append(f"{label} {was!r}→{now!r}")
    tightened = ", ".join(changed) or "no declared bound moved"
    return (f"the application rejected the previous value; it published "
            f"{signal.message[:120]!r} (anchored by {signal.source}), read as "
            f"{hint.code or 'an unspecified'} — tightened {tightened}")



def reads_prose(regenerate: Regenerate) -> bool:
    """Can this regenerator act on the application's SENTENCE, not just bounds?

    THE SIGNATURE IS THE DECLARATION. A regenerator that accepts the rejection
    text is telling us it can read it; a two-argument one is telling us it
    cannot, and for it a message naming nothing to change genuinely leaves
    nothing to do.

    This distinction is what keeps RULE 2 intact. "Something went wrong" names
    no bound, so a numeric generator bumping 1 to 2 would be a blind search —
    and a search that succeeds by accident produces a green result nobody can
    explain, which is the failure this loop exists to refuse. A model reading
    that same sentence is not searching blindly, so it alone is offered the
    attempt.
    """
    try:
        sig = inspect.signature(regenerate)
    except (TypeError, ValueError):                              # noqa: BLE001
        return False
    if any(p.kind is inspect.Parameter.VAR_POSITIONAL or
           p.kind is inspect.Parameter.VAR_KEYWORD
           for p in sig.parameters.values()):
        return True
    return len(sig.parameters) >= 3


def _ask_regenerate(regenerate: Regenerate, cons: C.Constraints,
                    refused: "frozenset[str]", rejection: str) -> Optional[str]:
    """Call ``regenerate``, passing the rejection when it accepts one.

    Every regenerator in the tree today takes two arguments; the message is a
    new, optional third. Rather than change every call site at once — and every
    test that fakes one — the arity decides, so an older regenerator is simply
    given what it always got.
    """
    if reads_prose(regenerate):
        return regenerate(cons, refused, rejection)
    return regenerate(cons, refused)


async def repair_loop(
    driver: FillDriver,
    control: Mapping[str, Any],
    *,
    first_value: Optional[str],
    cons: C.Constraints,
    regenerate: Regenerate,
    budget: RepairBudget = RepairBudget(),
    first_reason: str = "",
    retry: RetryPolicy = RetryPolicy(),
    repairable: bool = True,
    sleep: Optional[Callable[[float], Awaitable[None]]] = None,
) -> RepairOutcome:
    """Fill one control, and repair it until the application accepts it.

    ``first_value`` is what :mod:`app.fill_engine.generator` produced.  Passing
    ``None`` means the generator honestly had nothing — the loop does not
    invent one, it records the refusal and stops, because a value made up to
    satisfy a loop is exactly the fabrication this product exists to prevent.

    ``regenerate`` is asked for a NEW value under the tightened constraints and
    is told which values have already been refused, so it can never hand back
    one the application has rejected.
    """
    attempts: list[RepairAttempt] = []
    refused: set[str] = set()
    value = first_value
    current = cons
    reason = first_reason or "the value the generator produced for this field"
    transient_retries = 0
    if sleep is None:
        import asyncio as _asyncio

        async def sleep(seconds: float) -> None:      # noqa: E306
            await _asyncio.sleep(seconds)

    if value is None:
        return RepairOutcome(
            accepted=False, value=None,
            attempts=(RepairAttempt(1, None, False,
                                    first_reason or "nothing honest could be "
                                    "generated for this field"),),
            stop_reason=STOP_NO_BETTER_VALUE)

    for index in range(1, budget.attempts + 1):
        # ── Gate 1 / T-RE-01 · THE SAME VALUE, WHEN THE PAGE MOVED ──────────
        # One commit used to be the whole story: any mechanical failure returned
        # immediately, so a control that was detached by a re-render mid-fill was
        # abandoned after a single attempt and reported as though the widget had
        # refused the value. It had not refused anything — it had not been asked.
        #
        # Re-issuing the SAME value is what makes this safe to do at all. No
        # value is invented, nothing is tightened, no constraint is inferred; the
        # act is idempotent, so a retry that turns out to have been unnecessary
        # costs a backoff and changes nothing. That is why this retry is allowed
        # for fields the repair path below is forbidden to touch: a Security PIN
        # may not be REGENERATED, and re-typing the operator's own PIN after the
        # form re-rendered is not a regeneration.
        verdict = await driver.commit(control, value)
        while (verdict.mechanical_failure
               and classify_failure(verdict.mechanical_failure) == FAILURE_TRANSIENT
               and transient_retries < retry.transient_attempts - 1):
            transient_retries += 1
            delay = retry.delay_ms(transient_retries)
            logger.info(
                "qec.repair.transient control=%r attempt=%d retry=%d/%d "
                "backoff_ms=%d failure=%r - the page moved under the fill; "
                "re-issuing the same value",
                str(control.get("name") or "")[:40], index, transient_retries,
                retry.transient_attempts - 1, delay,
                verdict.mechanical_failure[:80])
            if delay:
                await sleep(delay / 1000.0)
            verdict = await driver.commit(control, value)

        attempts.append(RepairAttempt(
            attempt=index, value=value, accepted=verdict.accepted, reason=reason,
            signals=tuple(verdict.signals), committed=verdict.committed))

        if verdict.accepted:
            return RepairOutcome(accepted=True, value=value,
                                 attempts=tuple(attempts),
                                 stop_reason=STOP_ACCEPTED,
                                 committed=verdict.committed,
                                 transient_retries=transient_retries)

        refused.add(value)

        if verdict.mechanical_failure:
            # The widget would not take the value at all.  Repairing the VALUE
            # cannot help, and pretending otherwise burns the budget on the wrong
            # problem.  Two endings, kept apart because they mean opposite
            # things to whoever reads the ledger: a failure we retried and could
            # not outlast, and one that was never worth retrying.
            exhausted = (classify_failure(verdict.mechanical_failure)
                         == FAILURE_TRANSIENT)
            return RepairOutcome(
                accepted=False, value=None, attempts=tuple(attempts),
                transient_retries=transient_retries,
                stop_reason=(STOP_TRANSIENT_BUDGET if exhausted
                             else f"widget_refused:{verdict.mechanical_failure[:60]}"))

        if not repairable:
            # ── Gate 1 / T-RE-02 · SAY WHICH GATE STOPPED THIS ──────────────
            # The application rejected a value this loop is FORBIDDEN to change,
            # because it did not come from the generator: a persona's real date
            # of birth, a member number, a credential, an MFA code. Regenerating
            # any of those would fabricate the very data the crawl is supposed to
            # be carrying, so the refusal is correct and stays.
            #
            # What was NOT correct is what the ledger said about it. The
            # regenerate callback returned None and the loop reported
            # "no value satisfies the tightened constraints" — a sentence about a
            # search that never ran, which sent operators looking at constraint
            # inference for a field whose value was never in question.
            return RepairOutcome(accepted=False, value=None,
                                 attempts=tuple(attempts),
                                 transient_retries=transient_retries,
                                 stop_reason=STOP_NOT_REPAIRABLE)

        if index >= budget.attempts:
            return RepairOutcome(accepted=False, value=None,
                                 attempts=tuple(attempts),
                                 transient_retries=transient_retries,
                                 stop_reason=STOP_BUDGET)

        # RULE 1 — no observed rejection, no retry.
        anchored = [s for s in verdict.signals if s.is_anchored]
        if not anchored:
            return RepairOutcome(accepted=False, value=None,
                                 attempts=tuple(attempts),
                                 transient_retries=transient_retries,
                                 stop_reason=STOP_NO_SIGNAL)

        # RULE 2 — the retry must change something the rejection named.
        signal = anchored[0]
        hint = interpret(signal.message)
        if not hint.actionable:
            # A PROSE REJECTION IS STILL A REJECTION.
            #
            # "Please enter a valid weight" names no bound, no pattern and no
            # number, so `interpret` has nothing to fold into the constraints
            # and `tighten` would be a no-op. That is a fact about the PATTERN
            # TABLE, not about the message: a model reads that sentence and
            # produces a weight. Stopping here made every prose rejection
            # unreachable — measured live: the LifeOps wizard stalled on
            # "Enter a valid weight" with the repair loop never consulted.
            #
            # The rejection still has to be ANCHORED to reach this line, so
            # RULE 1 is untouched; and a regenerator with nothing to offer
            # still ends the attempt one line below.
            candidate = (_ask_regenerate(regenerate, current, frozenset(refused),
                                         signal.message)
                         if reads_prose(regenerate) else None)
            if candidate is None or candidate in refused:
                return RepairOutcome(accepted=False, value=None,
                                     attempts=tuple(attempts),
                                     transient_retries=transient_retries,
                                     stop_reason=STOP_NOT_ACTIONABLE)
            reason = (f"the application said {signal.message[:120]!r} "
                      f"(anchored by {signal.source}); no constraint could be "
                      f"derived from it, so the value was regenerated on the "
                      f"message itself")
            value = candidate
            refused.add(candidate)
            continue

        tightened = tighten(current, hint)
        candidate = _ask_regenerate(regenerate, tightened, frozenset(refused),
                                    signal.message)
        if candidate is None:
            return RepairOutcome(accepted=False, value=None,
                                 attempts=tuple(attempts),
                                 transient_retries=transient_retries,
                                 stop_reason=STOP_NO_BETTER_VALUE)
        if candidate in refused:
            return RepairOutcome(accepted=False, value=None,
                                 attempts=tuple(attempts),
                                 transient_retries=transient_retries,
                                 stop_reason=STOP_REPEATED_VALUE)

        reason = _reason_for(signal, hint, current, tightened)
        current, value = tightened, candidate

    return RepairOutcome(accepted=False, value=None, attempts=tuple(attempts),
                         transient_retries=transient_retries,
                         stop_reason=STOP_BUDGET)
