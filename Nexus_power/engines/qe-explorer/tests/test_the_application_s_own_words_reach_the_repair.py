"""THE APPLICATION'S REJECTION IS THE RULE — CARRY IT, DON'T PARAPHRASE IT.

The repair loop already refuses to retry without an observed, control-anchored
rejection, and already narrows the constraints by what that rejection NAMED.
Two things it did not do, both visible in the loop:

  1. ``regenerate(tightened, refused)`` received the tightened CONSTRAINTS and
     nothing else. The application's own sentence — "Enter a valid weight",
     "Applicant must be 18-85" — reached ``interpret()`` and stopped there. A
     deterministic generator only needs the constraints; a model does far
     better with the words, and the LLM rung was being handed a Constraints
     repr dressed up as a rejection.

  2. When ``interpret()`` cannot parse a message into a structured hint, the
     loop stops at ``STOP_NOT_ACTIONABLE`` — no retry at all. That is right for
     a pattern table, which genuinely has nothing to change. It is wrong for a
     model, which can act on "Please enter a valid weight" perfectly well. The
     whole class of prose rejections was unreachable.

Both are fixed by carrying the message: ``Regenerate`` takes the rejection text
as a third argument, and an unparseable-but-anchored rejection now reaches the
regenerator instead of ending the repair.

WHAT MUST NOT CHANGE, pinned below: no anchored rejection still means no retry
(RULE 1 is the whole reason this is a repair loop and not a retry loop), a
regenerator that returns None still ends the attempt honestly, and a repeated
value is still refused.
"""
from __future__ import annotations

import pytest

from app.fill_engine import constraints as C
from app.fill_engine.repair import (STOP_NO_SIGNAL, STOP_NOT_ACTIONABLE,
                                    RepairBudget, repair_loop)


from app.fill_engine.validation import SOURCE_PAGE, ValidationSignal


def _Signal(message, anchored=True):
    """A real ValidationSignal — `is_anchored` is derived from `source`, so the
    fake must not fabricate it or the test proves nothing about the real type."""
    return ValidationSignal(
        code="invalid", message=message,
        source="aria_describedby" if anchored else SOURCE_PAGE)


def _Verdict(accepted, signals=()):
    from app.fill_engine.repair import FillVerdict
    return FillVerdict(accepted=accepted, committed=None,
                       signals=tuple(signals))


class _Driver:
    """Accepts only once a value the application would take is offered."""

    def __init__(self, script):
        #: [(verdict_for_this_attempt)] in order
        self.script = list(script)
        self.tried = []

    async def commit(self, control, value):
        self.tried.append(value)
        return self.script.pop(0) if self.script else _Verdict(True)

    async def read(self, control):
        return _Verdict(True)


@pytest.mark.asyncio
async def test_the_rejection_sentence_reaches_the_regenerator():
    """THE POINT: the model gets the application's words, not a repr."""
    seen = {}

    def regenerate(tightened, refused, rejection=""):
        seen["rejection"] = rejection
        return "42"

    driver = _Driver([_Verdict(False, [_Signal("Applicant must be 18-85")])])
    await repair_loop(driver, {"name": "Age"}, first_value="7",
                      cons=C.Constraints(), regenerate=regenerate,
                      budget=RepairBudget(attempts=2), repairable=True)
    assert seen["rejection"] == "Applicant must be 18-85"


@pytest.mark.asyncio
async def test_a_prose_rejection_no_pattern_can_parse_still_gets_one_retry():
    """"Enter a valid weight" carries no number, no bound, no pattern — the
    interpreter has nothing to fold in. A model does not need it folded."""
    calls = []

    def regenerate(tightened, refused, rejection=""):
        calls.append(rejection)
        return "72 kg"

    driver = _Driver([_Verdict(False, [_Signal("Please enter a valid weight")]),
                      _Verdict(True)])
    out = await repair_loop(driver, {"name": "Weight"}, first_value="178",
                            cons=C.Constraints(), regenerate=regenerate,
                            budget=RepairBudget(attempts=3), repairable=True)
    assert calls == ["Please enter a valid weight"]
    assert out.accepted is True
    assert out.value == "72 kg"


# ── the rules that must survive ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_anchored_rejection_still_means_no_retry():
    """RULE 1. An unanchored complaint is not evidence about our value."""
    def regenerate(tightened, refused, rejection=""):
        raise AssertionError("must not be consulted without an anchored signal")

    driver = _Driver([_Verdict(False, [_Signal("Something went wrong",
                                               anchored=False)])])
    out = await repair_loop(driver, {"name": "X"}, first_value="a",
                            cons=C.Constraints(), regenerate=regenerate,
                            budget=RepairBudget(attempts=3), repairable=True)
    assert out.accepted is False
    assert out.stop_reason == STOP_NO_SIGNAL


@pytest.mark.asyncio
async def test_a_regenerator_with_nothing_left_ends_the_attempt_honestly():
    def regenerate(tightened, refused, rejection=""):
        return None

    driver = _Driver([_Verdict(False, [_Signal("Please enter a valid weight")])])
    out = await repair_loop(driver, {"name": "Weight"}, first_value="178",
                            cons=C.Constraints(), regenerate=regenerate,
                            budget=RepairBudget(attempts=3), repairable=True)
    assert out.accepted is False


@pytest.mark.asyncio
async def test_an_unrepairable_value_is_still_never_regenerated():
    """A value that did not come from the generator must not be replaced by
    one that did — provenance-locked stays locked."""
    def regenerate(tightened, refused, rejection=""):
        raise AssertionError("an unrepairable field must not regenerate")

    driver = _Driver([_Verdict(False, [_Signal("Wrong PIN")])])
    out = await repair_loop(driver, {"name": "Security PIN"}, first_value="0000",
                            cons=C.Constraints(), regenerate=regenerate,
                            budget=RepairBudget(attempts=3), repairable=False)
    assert out.accepted is False


# ── the line between reading prose and searching blindly ───────────────────

def test_a_two_argument_regenerator_declares_it_cannot_read_prose():
    """THE RULE-2 GUARD. A generator that only takes constraints has nothing to
    act on when the message names nothing — bumping 1 to 2 because the app said
    "Something went wrong" is the blind search that produces a green nobody can
    explain. Its SIGNATURE is how it says so."""
    from app.fill_engine.repair import reads_prose

    def deterministic(cons, refused):
        return "2"

    def model_backed(cons, refused, rejection=""):
        return "72 kg"

    assert reads_prose(deterministic) is False
    assert reads_prose(model_backed) is True
    assert reads_prose(lambda *a: None) is True, "varargs can take the message"


@pytest.mark.asyncio
async def test_a_prose_rejection_stops_for_a_generator_that_cannot_read_it():
    """The existing contract, unchanged: same message, same anchoring — a
    deterministic regenerator still stops at NOT_ACTIONABLE rather than guess."""
    tried = []

    def deterministic(cons, refused):
        tried.append("called")
        return "2"

    driver = _Driver([_Verdict(False, [_Signal("Something went wrong")])])
    out = await repair_loop(driver, {"name": "Age"}, first_value="1",
                            cons=C.Constraints(), regenerate=deterministic,
                            budget=RepairBudget(attempts=3), repairable=True)
    assert out.stop_reason == STOP_NOT_ACTIONABLE
    assert tried == [], "it must not have been consulted at all"
    assert driver.tried == ["1"], "and no second value was committed"
