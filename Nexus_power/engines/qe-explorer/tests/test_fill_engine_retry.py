"""Gate 1 / T-RE-01 + T-RE-02 — THE ENGINE STOPPED AFTER ONE ATTEMPT.

THE REPORTED SYMPTOM, and what was actually wrong with it.

A live crawl of VKPower Life reached the Security PIN step of the login and gave
up on the field with ``attempts = 1``.  Read from the outside that looks like a
missing retry budget, and it is not: :class:`RepairBudget` has defaulted to three
attempts since T-FE-01, and the loop honours it.  Two DIFFERENT one-attempt exits
were being read as that number.

**1 — the provenance gate (T-RE-02).**  ``forms.py`` only permits regeneration
for values the generator itself produced (``PROV_SYNTHESIZED``).  A Security PIN
comes from MFA config, a member number from the operator's data, a date of birth
from the persona — regenerating any of those fabricates the very data the crawl
is supposed to be carrying, so the refusal is CORRECT and stays.  What was wrong
was the record: the callback returned ``None`` and the loop reported
``no_value_satisfies_the_tightened_constraints`` — a sentence about a search that
never ran, sending an operator to look at constraint inference for a field whose
value was never in question.

**2 — the transient gate (T-RE-01).**  ANY mechanical failure returned
immediately.  A control detached by a re-render mid-fill, a commit racing an
in-flight navigation and a settle timeout were all reported as though the widget
had refused the value.  It had not refused anything; it had not been asked.  The
fix retries the SAME value — which is what makes it safe to do for a field the
repair path is forbidden to touch, because re-typing the operator's own PIN after
the form re-rendered is not a regeneration of it.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

import pytest

from app.fill_engine import constraints as C
from app.fill_engine.validation import SOURCE_ARIA_ERRORMESSAGE
from app.fill_engine.repair import (
    FAILURE_PERMANENT,
    FAILURE_TRANSIENT,
    STOP_ACCEPTED,
    STOP_NOT_REPAIRABLE,
    STOP_NO_BETTER_VALUE,
    STOP_TRANSIENT_BUDGET,
    FillVerdict,
    RepairBudget,
    RetryPolicy,
    ValidationSignal,
    classify_failure,
    repair_loop,
)


CONTROL: Mapping[str, Any] = {"name": "Security PIN", "kind": "text"}


class _Driver:
    """Answers with a scripted sequence of verdicts and records every commit."""

    def __init__(self, verdicts: list[FillVerdict]) -> None:
        self._verdicts = list(verdicts)
        self.commits: list[str] = []

    async def commit(self, control, value):
        self.commits.append(value)
        return (self._verdicts.pop(0) if self._verdicts
                else FillVerdict(accepted=True, committed=value))


class _Clock:
    """A sleep that records instead of waiting, so timing is asserted rather
    than endured — and the suite stays instant."""

    def __init__(self) -> None:
        self.slept_ms: list[int] = []

    async def __call__(self, seconds: float) -> None:
        self.slept_ms.append(int(round(seconds * 1000)))


def _never_regenerates(_cons, _refused) -> Optional[str]:
    return None


async def _run(driver, *, repairable=True, retry=None, budget=None,
               regenerate=_never_regenerates, sleep=None, value="1234"):
    return await repair_loop(
        driver, CONTROL, first_value=value, cons=C.Constraints(),
        regenerate=regenerate, budget=budget or RepairBudget(),
        retry=retry or RetryPolicy(), repairable=repairable,
        sleep=sleep or _Clock())


# ─── classification ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("failure", [
    "element is not attached to the DOM",
    "Element is detached from document",
    "Timeout 30000ms exceeded",
    "element click intercepted by another element",
    "Execution context was destroyed, most likely because of a navigation",
    "Target closed",
    "element is not stable",
])
def test_a_page_race_is_transient(failure):
    """Each of these describes a STATE A LATER MOMENT CAN DIFFER FROM.  That is
    the whole criterion: the value was never examined, so asking again is a sane
    act rather than a hopeful one."""
    assert classify_failure(failure) == FAILURE_TRANSIENT


@pytest.mark.parametrize("failure", [
    "intent_unmet", "readonly", "the option does not exist",
    "widget refused the verb", "",
])
def test_anything_unrecognised_is_permanent(failure):
    """FAIL-CLOSED.  An unrecognised failure retried three times is three times
    the wall clock for the same answer, and on a crawl that is budget the rest of
    the funnel needed."""
    assert classify_failure(failure) == FAILURE_PERMANENT


# ─── the transient retry ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_detached_element_is_retried_and_then_succeeds():
    """THE DEFECT, INVERTED.  Before this, one detached element ended the field."""
    driver = _Driver([
        FillVerdict(accepted=False, mechanical_failure="element is not attached"),
        FillVerdict(accepted=True, committed="1234"),
    ])
    outcome = await _run(driver)

    assert outcome.accepted
    assert outcome.stop_reason == STOP_ACCEPTED
    assert driver.commits == ["1234", "1234"], "the SAME value, re-issued"
    assert outcome.transient_retries == 1


@pytest.mark.asyncio
async def test_the_retry_never_invents_a_different_value():
    """What makes this safe for a provenance-locked field.  Nothing is
    generated, nothing is tightened, and the act is idempotent."""
    driver = _Driver([
        FillVerdict(accepted=False, mechanical_failure="timeout"),
        FillVerdict(accepted=False, mechanical_failure="timeout"),
        FillVerdict(accepted=True, committed="1234"),
    ])
    outcome = await _run(driver, repairable=False)

    assert outcome.accepted
    assert set(driver.commits) == {"1234"}
    assert len(driver.commits) == 3


@pytest.mark.asyncio
async def test_a_permanent_failure_is_not_retried():
    driver = _Driver([
        FillVerdict(accepted=False, mechanical_failure="intent_unmet"),
    ])
    outcome = await _run(driver)

    assert not outcome.accepted
    assert driver.commits == ["1234"], "one attempt, correctly"
    assert outcome.stop_reason.startswith("widget_refused:")
    assert outcome.transient_retries == 0


@pytest.mark.asyncio
async def test_a_transient_failure_that_never_clears_says_so():
    """Distinct from ``widget_refused``: one is a failure we retried and could
    not outlast, the other was never worth retrying.  They mean opposite things
    to whoever reads the ledger."""
    driver = _Driver([FillVerdict(accepted=False, mechanical_failure="timeout")
                      for _ in range(6)])
    outcome = await _run(driver)

    assert outcome.stop_reason == STOP_TRANSIENT_BUDGET
    assert outcome.transient_retries == 2, "budget of 3 total commits"
    assert len(driver.commits) == 3


# ─── backoff ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_backoff_is_exponential_and_deterministic():
    """No jitter, on purpose.  This crawl's evidence is replayed and compared
    against goldens, so identical inputs must produce an identical sequence —
    and the re-render storm backoff protects against does not care whether the
    wait was randomised."""
    clock = _Clock()
    driver = _Driver([FillVerdict(accepted=False, mechanical_failure="timeout")
                      for _ in range(9)])
    await _run(driver,
               retry=RetryPolicy(transient_attempts=4, backoff_ms=50,
                                 backoff_factor=2.0, max_backoff_ms=1000),
               sleep=clock)

    assert clock.slept_ms == [50, 100, 200]


def test_the_backoff_is_capped():
    policy = RetryPolicy(backoff_ms=100, backoff_factor=10.0, max_backoff_ms=250)
    assert [policy.delay_ms(i) for i in (1, 2, 3)] == [100, 250, 250]


def test_a_policy_of_one_attempt_disables_retry_entirely():
    """The pre-Gate-1 behaviour exactly, available as configuration — so a lane
    that wants the old semantics can have them without a code change."""
    assert RetryPolicy(transient_attempts=1).transient_attempts == 1
    assert RetryPolicy(transient_attempts=0).transient_attempts == 1


def test_a_degenerate_policy_is_repaired_not_obeyed():
    policy = RetryPolicy(transient_attempts=-5, backoff_ms=-1,
                         backoff_factor=0.1, max_backoff_ms=0)
    assert policy.transient_attempts == 1
    assert policy.backoff_ms == 0
    assert policy.backoff_factor == 1.0


@pytest.mark.asyncio
async def test_no_sleep_happens_when_nothing_fails():
    clock = _Clock()
    await _run(_Driver([FillVerdict(accepted=True, committed="1234")]),
               sleep=clock)
    assert clock.slept_ms == []


# ─── the provenance gate, reported honestly ─────────────────────────────────

def _rejection() -> FillVerdict:
    """A rejection the interpreter can ACT on.

    RULE 2 of the loop is "the retry must change something the rejection named",
    so an uninterpretable message stops at ``rejection_named_nothing_to_change``
    one branch before the gates this file is about — which would make these
    tests pass for the wrong reason.
    """
    return FillVerdict(
        accepted=False, committed="1234",
        signals=(ValidationSignal(
            code="", source=SOURCE_ARIA_ERRORMESSAGE,
            message="Value must be greater than or equal to 18."),))


@pytest.mark.asyncio
async def test_a_provenance_locked_field_says_which_gate_stopped_it():
    """THE MISLEADING RECORD, FIXED.  The app rejected a Security PIN; the loop
    is forbidden to regenerate it, and must say THAT rather than report a
    constraint search it never ran."""
    driver = _Driver([_rejection()])
    outcome = await _run(driver, repairable=False)

    assert outcome.stop_reason == STOP_NOT_REPAIRABLE
    assert not outcome.accepted


@pytest.mark.asyncio
async def test_a_repairable_field_that_runs_out_of_values_keeps_its_own_reason():
    """The other half of the distinction: here the generator WAS asked and had
    nothing, which is a different finding with a different remediation."""
    driver = _Driver([_rejection()])
    outcome = await _run(driver, repairable=True)

    assert outcome.stop_reason == STOP_NO_BETTER_VALUE


@pytest.mark.asyncio
async def test_a_provenance_locked_field_is_committed_exactly_once():
    """The refusal must not be turned into a spree by the new retry: a value the
    app REJECTED is not a page race, and re-typing it would be a loop."""
    driver = _Driver([_rejection(), _rejection(), _rejection()])
    await _run(driver, repairable=False)
    assert driver.commits == ["1234"]


# ─── the two budgets are independent ────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_field_may_spend_one_of_each_budget():
    """A detached-element retry that then commits a value the app rejects has
    used one transient retry and one repair attempt.  Sharing a number would
    make either one steal from the other."""
    values = iter(["5678", "9012"])
    driver = _Driver([
        FillVerdict(accepted=False, mechanical_failure="detached"),
        _rejection(),
        FillVerdict(accepted=True, committed="5678"),
    ])
    outcome = await _run(driver, repairable=True,
                         regenerate=lambda _c, _r: next(values, None))

    assert outcome.accepted
    assert outcome.transient_retries == 1
    assert len(outcome.attempts) == 2, "one rejected value, then an accepted one"
    assert driver.commits == ["1234", "1234", "5678"]


@pytest.mark.asyncio
async def test_transient_retries_are_not_counted_as_repairs():
    """``repaired`` is the numerator of the repair-success rate.  Counting page
    flakiness in it would make that metric a measurement of the fixture."""
    driver = _Driver([
        FillVerdict(accepted=False, mechanical_failure="timeout"),
        FillVerdict(accepted=True, committed="1234"),
    ])
    outcome = await _run(driver)

    assert outcome.first_pass, "accepted on its first VALUE"
    assert not outcome.repaired
    assert outcome.transient_retries == 1


@pytest.mark.asyncio
async def test_the_audit_history_carries_the_retry_count():
    driver = _Driver([
        FillVerdict(accepted=False, mechanical_failure="timeout"),
        _rejection(),
    ])
    outcome = await _run(driver, repairable=False)
    assert outcome.as_dict()["transient_retries"] == 1
    assert outcome.as_dict()["stop_reason"] == STOP_NOT_REPAIRABLE
