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
    "STOP_NO_BETTER_VALUE", "STOP_REPEATED_VALUE",
]

STOP_ACCEPTED = "accepted"
STOP_BUDGET = "retry_budget_exhausted"
STOP_NO_SIGNAL = "rejected_without_a_readable_reason"
STOP_NOT_ACTIONABLE = "rejection_named_nothing_to_change"
STOP_NO_BETTER_VALUE = "no_value_satisfies_the_tightened_constraints"
STOP_REPEATED_VALUE = "the_only_remaining_value_was_already_rejected"


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
Regenerate = Callable[[C.Constraints, "frozenset[str]"], Optional[str]]


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


async def repair_loop(
    driver: FillDriver,
    control: Mapping[str, Any],
    *,
    first_value: Optional[str],
    cons: C.Constraints,
    regenerate: Regenerate,
    budget: RepairBudget = RepairBudget(),
    first_reason: str = "",
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

    if value is None:
        return RepairOutcome(
            accepted=False, value=None,
            attempts=(RepairAttempt(1, None, False,
                                    first_reason or "nothing honest could be "
                                    "generated for this field"),),
            stop_reason=STOP_NO_BETTER_VALUE)

    for index in range(1, budget.attempts + 1):
        verdict = await driver.commit(control, value)
        attempts.append(RepairAttempt(
            attempt=index, value=value, accepted=verdict.accepted, reason=reason,
            signals=tuple(verdict.signals), committed=verdict.committed))

        if verdict.accepted:
            return RepairOutcome(accepted=True, value=value,
                                 attempts=tuple(attempts),
                                 stop_reason=STOP_ACCEPTED,
                                 committed=verdict.committed)

        refused.add(value)

        if verdict.mechanical_failure:
            # The widget would not take the value at all.  Repairing the VALUE
            # cannot help, and pretending otherwise burns the budget on the wrong
            # problem.
            return RepairOutcome(
                accepted=False, value=None, attempts=tuple(attempts),
                stop_reason=f"widget_refused:{verdict.mechanical_failure[:60]}")

        if index >= budget.attempts:
            return RepairOutcome(accepted=False, value=None,
                                 attempts=tuple(attempts),
                                 stop_reason=STOP_BUDGET)

        # RULE 1 — no observed rejection, no retry.
        anchored = [s for s in verdict.signals if s.is_anchored]
        if not anchored:
            return RepairOutcome(accepted=False, value=None,
                                 attempts=tuple(attempts),
                                 stop_reason=STOP_NO_SIGNAL)

        # RULE 2 — the retry must change something the rejection named.
        signal = anchored[0]
        hint = interpret(signal.message)
        if not hint.actionable:
            return RepairOutcome(accepted=False, value=None,
                                 attempts=tuple(attempts),
                                 stop_reason=STOP_NOT_ACTIONABLE)

        tightened = tighten(current, hint)
        candidate = regenerate(tightened, frozenset(refused))
        if candidate is None:
            return RepairOutcome(accepted=False, value=None,
                                 attempts=tuple(attempts),
                                 stop_reason=STOP_NO_BETTER_VALUE)
        if candidate in refused:
            return RepairOutcome(accepted=False, value=None,
                                 attempts=tuple(attempts),
                                 stop_reason=STOP_REPEATED_VALUE)

        reason = _reason_for(signal, hint, current, tightened)
        current, value = tightened, candidate

    return RepairOutcome(accepted=False, value=None, attempts=tuple(attempts),
                         stop_reason=STOP_BUDGET)
