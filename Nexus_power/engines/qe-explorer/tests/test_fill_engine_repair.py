"""T-FE-01 / T-FE-02 — THE ARROW BACK, AND WHOSE FAILURE IT IS.

Two defects, one shape.  Validity was a property of the PAGE, so a cookie banner
failed every fill on it; and there was no way back from a rejection, so a value
the application refused ended the field, the field ended the page, and the crawl
reported the number of fields it had ATTEMPTED.

The tests below drive the real :func:`app.fill_engine.repair.repair_loop`
against a fake application that behaves the way real ones do: it rejects values
with messages, publishes those messages through the accessibility channels form
libraries actually use, and keeps a consent banner up the whole time.
"""
from __future__ import annotations

import asyncio
from typing import Mapping

import pytest

from app.fill_engine import constraints as C
from app.fill_engine.repair import (STOP_ACCEPTED, STOP_BUDGET,
                                    STOP_NOT_ACTIONABLE, STOP_NO_BETTER_VALUE,
                                    STOP_NO_SIGNAL, FillVerdict, RepairBudget,
                                    repair_loop, tighten)
from app.fill_engine.validation import (PageAlertFilter, interpret,
                                        is_cookie_banner, is_informational,
                                        signals_for_control)

AGE = {"name": "Age", "kind": "text", "input_type": "number", "id": "age"}


# ── T-FE-02 · a page alert is not a verdict on a field ───────────────────────

COOKIE = "We use cookies to improve your experience. Accept all or manage preferences."


def test_a_consent_banner_is_never_a_verdict_on_a_form_field():
    """They are marked ``role=alert`` so screen readers announce them, which is
    exactly why the page-wide read failed every fill on every page."""
    assert is_cookie_banner(COOKIE)
    assert not PageAlertFilter().fresh([COOKIE])


def test_an_alert_already_on_the_page_is_stale_for_every_fill_that_follows():
    """The whole mechanism: snapshot once, before anything is typed."""
    alerts = PageAlertFilter(["Your session will expire in 5 minutes",
                             "Postcode is invalid"])
    assert alerts.fresh(["Your session will expire in 5 minutes",
                         "Postcode is invalid"]) == []
    assert alerts.suppressed == 2


def test_a_genuinely_new_rejection_is_not_suppressed():
    alerts = PageAlertFilter([COOKIE])
    assert alerts.fresh([COOKIE, "Age must be at least 18"]) == ["Age must be at least 18"]


def test_an_informational_region_is_not_a_rejection():
    assert is_informational("Your changes were saved")
    assert not is_informational("Your changes were saved but the postcode is invalid")


def test_an_unanchored_page_alert_fails_no_field():
    """THE HEADLINE FIX. An error raised by field 3 stays in the DOM while
    fields 4 through 12 are filled; without anchoring, one real failure was
    reported as ten."""
    signals = signals_for_control(
        AGE, fresh_alerts=["Postcode is invalid"], after_controls=[])
    assert signals == []


def test_an_alert_that_names_the_control_does_belong_to_it():
    signals = signals_for_control(
        AGE, fresh_alerts=["Age must be at least 18"], after_controls=[])
    assert [s.code for s in signals] == [C.CODE_MIN]
    assert signals[0].source == "message_names_control"


def test_an_aria_errormessage_is_the_strongest_anchor():
    control = dict(AGE, aria_errormessage="age-err")
    signals = signals_for_control(
        control, fresh_alerts=[{"id": "age-err", "text": "Age must be at least 18"}],
        after_controls=[{"id": "age-err", "text": "Age must be at least 18"}])
    assert signals[0].source == "aria_errormessage"
    assert signals[0].code == C.CODE_MIN and signals[0].detail == "18"


def test_the_form_librarys_id_convention_anchors_too():
    """Every mainstream library emits ``<id>-error`` when it does not wire
    ``aria-describedby``."""
    signals = signals_for_control(
        AGE, after_controls=[{"id": "age-error", "text": "Age must be at least 18"}])
    assert signals and signals[0].source == "id_convention"


def test_a_control_declaring_itself_invalid_with_no_message_still_counts():
    signals = signals_for_control(AGE, after_controls=[dict(AGE, aria_invalid="true")])
    assert signals and signals[0].source == "aria_invalid"


def test_the_native_validation_message_is_read_when_the_browser_gives_one():
    signals = signals_for_control(
        AGE, native_message="Value must be greater than or equal to 18.")
    assert signals[0].source == "native_validation_message"
    assert signals[0].code == C.CODE_MIN


# ── interpreting a rejection into something actionable ───────────────────────

@pytest.mark.parametrize("message,code,attribute,expected", [
    ("This field is required", C.CODE_REQUIRED, None, None),
    ("Age must be at least 18", C.CODE_MIN, "minimum", 18.0),
    ("Must be no more than 65", C.CODE_MAX, "maximum", 65.0),
    ("Must be between 18 and 65", C.CODE_MIN, "maximum", 65.0),
    ("Must be at least 8 characters", C.CODE_MINLENGTH, "minlength", 8),
    ("Must be exactly 5 digits", C.CODE_MINLENGTH, "exact_length", 5),
    ("No more than 10 characters", C.CODE_MAXLENGTH, "maxlength", 10),
])
def test_a_rejection_message_names_what_to_change(message, code, attribute, expected):
    hint = interpret(message)
    assert hint.code == code
    assert hint.actionable
    if attribute:
        assert getattr(hint, attribute) == expected


def test_a_message_that_names_nothing_actionable_says_so():
    """A retry that cannot say what it is changing is a blind retry, and blind
    retries are prohibited."""
    assert not interpret("Something went wrong").actionable
    assert not interpret("").actionable


def test_tightening_only_ever_narrows():
    """Monotonic, which is what makes the loop converge rather than oscillate
    between two rules the application stated in two places."""
    base = C.Constraints(minimum=18.0, maximum=65.0, declared=True)
    assert tighten(base, interpret("Must be at least 21")).minimum == 21.0
    assert tighten(base, interpret("Must be at least 10")).minimum == 18.0
    assert tighten(base, interpret("Must be no more than 90")).maximum == 65.0


# ── T-FE-01 · the loop itself ────────────────────────────────────────────────

class FakeApp:
    """An application that rejects until its rule is met, and SAYS the rule.

    Publishes through ``aria-errormessage`` — the strongest anchoring rung and
    the one a real form library uses — so the loop's inference is exercised
    end to end rather than short-circuited."""

    def __init__(self, *, minimum: int = 18, message: str = "",
                 silent: bool = False, vague: bool = False,
                 never: bool = False):
        self.minimum = minimum
        #: An application that raises its own bar every time — the pathological
        #: case the budget exists for.
        self.never = never
        self.message = message or f"Age must be at least {minimum}"
        self.silent = silent
        self.vague = vague
        self.commits: list[str] = []

    async def commit(self, control, value):
        self.commits.append(value)
        if self.never:
            self.minimum += 1000
            self.message = f"Age must be at least {self.minimum}"
            accepted = False
        else:
            try:
                accepted = float(value) >= self.minimum
            except ValueError:
                accepted = False
        if accepted:
            return FillVerdict(accepted=True, committed=value)
        if self.silent:
            return FillVerdict(accepted=False, committed=value)
        text = "Something went wrong" if self.vague else self.message
        return FillVerdict(accepted=False, committed=value, signals=tuple(
            signals_for_control(control, after_controls=[
                {"id": "age-err", "text": text}])))


CONTROL = dict(AGE, aria_errormessage="age-err")


def _numeric_regenerate(app_min_seen=None):
    """A generator that produces the smallest value the tightened constraints
    allow — the same discipline the real generator uses."""
    def regenerate(cons: C.Constraints, refused):
        candidate = int(cons.minimum) if cons.minimum is not None else 1
        while str(candidate) in refused:
            candidate += 1
        return str(candidate)
    return regenerate


def _run(app, first="1", budget=RepairBudget(), regenerate=None):
    return asyncio.run(repair_loop(
        app, CONTROL, first_value=first,
        cons=C.Constraints(input_type="number", declared=True),
        regenerate=regenerate or _numeric_regenerate(), budget=budget))


def test_a_rejected_value_is_repaired_and_accepted():
    """The arrow that did not exist."""
    app = FakeApp(minimum=18)
    outcome = _run(app)
    assert outcome.accepted and outcome.value == "18"
    assert app.commits == ["1", "18"]
    assert outcome.repaired and not outcome.first_pass
    assert outcome.stop_reason == STOP_ACCEPTED


def test_every_retry_explains_why_that_value_was_chosen():
    """Blind retries are prohibited; the explanation is how that is enforced and
    how a reader checks the inference rather than trusting it."""
    outcome = _run(FakeApp(minimum=21))
    reason = outcome.attempts[1].reason
    assert "Age must be at least 21" in reason
    assert "aria_errormessage" in reason
    assert "min None->21.0" in reason.replace("→", "->")


def test_a_first_pass_success_costs_no_repair():
    outcome = _run(FakeApp(minimum=18), first="30")
    assert outcome.first_pass and len(outcome.attempts) == 1


def test_repair_converges_within_the_bounded_budget():
    for minimum in range(1, 40):
        outcome = _run(FakeApp(minimum=minimum))
        assert outcome.accepted, minimum
        assert len(outcome.attempts) <= RepairBudget().attempts


def test_a_rejection_with_no_readable_reason_stops_rather_than_guessing():
    """No observed signal, no retry.  A search that succeeds by accident
    produces a green result nobody can explain."""
    app = FakeApp(minimum=18, silent=True)
    outcome = _run(app)
    assert not outcome.accepted
    assert outcome.stop_reason == STOP_NO_SIGNAL
    assert app.commits == ["1"], "it must not have tried a second value"


def test_a_rejection_that_names_nothing_to_change_stops_too():
    app = FakeApp(minimum=18, vague=True)
    outcome = _run(app)
    assert outcome.stop_reason == STOP_NOT_ACTIONABLE
    assert app.commits == ["1"]


def test_the_budget_is_a_hard_ceiling():
    """An unbounded loop against an application that rejects everything is a
    denial of service aimed at our own crawl."""
    app = FakeApp(minimum=1_000, never=True)
    outcome = _run(app, budget=RepairBudget(attempts=3))
    assert not outcome.accepted and outcome.stop_reason == STOP_BUDGET
    assert len(app.commits) == 3


def test_a_generator_with_nothing_better_stops_honestly():
    outcome = _run(FakeApp(minimum=18), regenerate=lambda cons, refused: None)
    assert outcome.stop_reason == STOP_NO_BETTER_VALUE
    assert not outcome.accepted


def test_a_value_already_refused_is_never_offered_again():
    outcome = _run(FakeApp(minimum=18), regenerate=lambda cons, refused: "1")
    assert not outcome.accepted
    assert outcome.stop_reason in ("the_only_remaining_value_was_already_rejected",
                                   STOP_NO_BETTER_VALUE)


def test_a_widget_that_refuses_the_verb_is_not_a_value_problem():
    """Repairing the VALUE cannot help, and pretending otherwise burns the
    budget on the wrong problem."""
    class Broken:
        async def commit(self, control, value):
            return FillVerdict(accepted=False, mechanical_failure="intent_unmet")

    outcome = _run(Broken())
    assert outcome.stop_reason.startswith("widget_refused")
    assert len(outcome.attempts) == 1


def test_nothing_generatable_is_recorded_without_a_single_commit():
    """A value invented to satisfy a loop is exactly the fabrication this
    product exists to prevent."""
    class Never:
        def __init__(self):
            self.commits = 0

        async def commit(self, control, value):
            self.commits += 1
            return FillVerdict(accepted=True)

    app = Never()
    outcome = asyncio.run(repair_loop(
        app, CONTROL, first_value=None, cons=C.Constraints(),
        regenerate=lambda c, r: None))
    assert not outcome.accepted and app.commits == 0


def test_the_outcome_reads_as_one_paragraph():
    outcome = _run(FakeApp(minimum=18))
    text = outcome.explanation()
    assert text.startswith("attempt 1:") and "outcome: accepted" in text
    assert outcome.as_dict()["attempt_count"] == 2


# ═══════════════════════════════════════════════════════════════════════════
#  THE STALE NATIVE MESSAGE — a required field rejected for being filled
#
#  An empty ``required`` input fails ``checkValidity()`` before anything is
#  typed into it, so the inventory captures it carrying the browser's
#  "Please fill out this field." That message describes the control as it was
#  BEFORE the commit, and the stage-2 read used to fall back to it whenever the
#  post-fill re-read reported a now-empty message — which is precisely what a
#  control reports once it has been filled correctly.
#
#  The result was that every required field was rejected at the instant it was
#  filled. A login form is nothing but required fields, so a real crawl of a
#  real application filled zero fields, closed its wizard gate and stopped at
#  the sign-in page. Proven against Chromium on the acme-life proving ground
#  before the fix, and again after it.
# ═══════════════════════════════════════════════════════════════════════════

class _CleanPort:
    """A page with no alerts whose control reports itself valid after the fill."""

    def __init__(self, after):
        self._after = after

    async def error_texts(self):
        return []

    async def collect_controls(self):
        return list(self._after)


def _drive(pre, after, value="Ada Lovelace"):
    from app.fill_engine.driver import ControlFillDriver

    if isinstance(after, Mapping):
        after = [after]

    async def commit(control, val):
        return val, True, ""              # read-back matched, intent met, no mechanics

    driver = ControlFillDriver(_CleanPort(after), commit, PageAlertFilter())
    return asyncio.run(driver.commit(pre, value))


#: What Chromium actually puts on an untouched ``<input required>``.
_NATIVE_EMPTY = "Please fill out this field."


def test_a_required_field_is_not_rejected_for_having_just_been_filled():
    pre = {"name": "Username", "id": "username", "kind": "text",
           "validation_message": _NATIVE_EMPTY}
    after = dict(pre, validation_message="")          # the browser cleared it
    verdict = _drive(pre, after)
    assert verdict.accepted, (
        "the field was filled, the browser declared it valid, and the fill was "
        f"still rejected: {[s.as_dict() for s in verdict.signals]}")
    assert not verdict.signals


def test_a_control_that_vanished_from_the_re_read_carries_no_verdict():
    """Submitted, navigated, re-rendered — absence is not a rejection."""
    pre = {"name": "Username", "id": "username", "kind": "text",
           "validation_message": _NATIVE_EMPTY}
    assert _drive(pre, after=[]).accepted


def test_a_genuine_post_fill_rejection_is_still_read():
    """The fix must not buy acceptance by going blind: a message the browser
    raises about the NEW value still fails the fill."""
    pre = {"name": "Age", "id": "age", "kind": "text",
           "validation_message": _NATIVE_EMPTY}
    after = dict(pre, validation_message="Value must be greater than or equal to 18.")
    verdict = _drive(pre, after, value="7")
    assert not verdict.accepted and verdict.signals
    assert "18" in verdict.signals[0].message


def test_the_stale_message_does_not_survive_onto_a_different_control():
    """The re-read is matched to THIS control; another field's message is not
    borrowed to reject it."""
    pre = {"name": "Username", "id": "username", "kind": "text",
           "validation_message": _NATIVE_EMPTY}
    other = {"name": "Password", "id": "password", "kind": "password",
             "validation_message": "Please fill out this field."}
    assert _drive(pre, after=[other, dict(pre, validation_message="")]).accepted
