"""R0 Intent Contracts — every actuation declares + verifies its intent.

Tests the pure ``verify_intent`` function (browser.py), the intent-gated
``_fill_one`` path (forms.py), and the ``intent_unmet`` counter surfacing
in ``FormFillResult`` and ``flow_ledger.summarize``.
"""
from __future__ import annotations

from app.browser import RawObservation, verify_intent
from app.forms import (
    AnswerKey,
    FormFillResult,
    PROV_INTENT_UNMET,
    fill_form_phase_a,
)
from app import emit, flow_ledger


# ─── Pure verify_intent tests ────────────────────────────────────────────────


class TestVerifyIntentFill:
    def test_exact_match_true(self):
        assert verify_intent("fill", intended_value="John",
                             committed_value="John") is True

    def test_case_insensitive_match(self):
        assert verify_intent("fill", intended_value="john",
                             committed_value="JOHN") is True

    def test_whitespace_normalized(self):
        assert verify_intent("fill", intended_value=" John ",
                             committed_value="John") is True

    def test_empty_committed_non_empty_intended(self):
        assert verify_intent("fill", intended_value="John",
                             committed_value="") is False

    def test_no_readback_is_none(self):
        assert verify_intent("fill", intended_value="John",
                             committed_value=None) is None

    def test_reformatted_value_is_none(self):
        assert verify_intent("fill", intended_value="1234567890",
                             committed_value="(123) 456-7890") is None

    def test_error_always_false(self):
        assert verify_intent("fill", intended_value="John",
                             committed_value="John",
                             error_detail="validation error") is False


class TestVerifyIntentSelect:
    def test_exact_match(self):
        assert verify_intent("select", intended_value="California",
                             committed_value="California") is True

    def test_empty_committed(self):
        assert verify_intent("select", intended_value="California",
                             committed_value="") is False

    def test_no_readback(self):
        assert verify_intent("select", intended_value="California",
                             committed_value=None) is None

    def test_error(self):
        assert verify_intent("select", intended_value="California",
                             committed_value="California",
                             error_detail="action_error: timeout") is False


class TestVerifyIntentChecked:
    def test_checked_true_matches(self):
        assert verify_intent("checked", intended_checked=True,
                             committed_value="true") is True

    def test_checked_false_matches(self):
        assert verify_intent("checked", intended_checked=False,
                             committed_value="false") is True

    def test_checked_mismatch(self):
        assert verify_intent("checked", intended_checked=True,
                             committed_value="false") is False

    def test_unchecked_mismatch(self):
        assert verify_intent("checked", intended_checked=False,
                             committed_value="true") is False

    def test_no_readback(self):
        assert verify_intent("checked", intended_checked=True,
                             committed_value=None) is None

    def test_error(self):
        assert verify_intent("checked", intended_checked=True,
                             committed_value="true",
                             error_detail="set_checked failed") is False

    def test_truthy_variants(self):
        for val in ("true", "1", "on", "yes", "checked"):
            assert verify_intent("checked", intended_checked=True,
                                 committed_value=val) is True
        for val in ("false", "0", "off", "no", "unchecked", ""):
            assert verify_intent("checked", intended_checked=False,
                                 committed_value=val) is True


class TestVerifyIntentClick:
    def test_url_changed(self):
        assert verify_intent("click", url_before="http://a.com/page1",
                             url_after="http://a.com/page2") is True

    def test_dom_changed(self):
        assert verify_intent("click", url_before="http://a.com",
                             url_after="http://a.com",
                             dom_changed=True) is True

    def test_dialog_opened(self):
        assert verify_intent("click", url_before="http://a.com",
                             url_after="http://a.com",
                             dialog_opened=True) is True

    def test_no_effect_is_none(self):
        assert verify_intent("click", url_before="http://a.com",
                             url_after="http://a.com") is None

    def test_error(self):
        assert verify_intent("click", error_detail="element detached") is False


class TestVerifyIntentHover:
    def test_hover_always_none(self):
        assert verify_intent("hover") is None

    def test_hover_with_error_is_false(self):
        assert verify_intent("hover", error_detail="hover failed") is False


# ─── Integration: _fill_one intent gate ──────────────────────────────────────


class IntentAwarePort:
    """A fake port that lets tests control intent_met on the observation."""

    def __init__(self, intent_met=None, committed_value=None, error=""):
        self._intent_met = intent_met
        self._committed = committed_value
        self._error = error

    async def fill(self, control, value):
        return RawObservation(
            url_before="u", url_after="u",
            committed_value=self._committed if self._committed is not None else value,
            intended_value=value,
            intent_met=self._intent_met,
            error_detail=self._error,
        )

    async def select_option(self, control, value):
        return RawObservation(
            url_before="u", url_after="u",
            committed_value=self._committed if self._committed is not None else value,
            intended_value=value,
            intent_met=self._intent_met,
            error_detail=self._error,
        )

    async def set_checked(self, control, checked):
        return RawObservation(
            url_before="u", url_after="u",
            committed_value="true" if checked else "false",
            intended_value="true" if checked else "false",
            intent_met=self._intent_met,
            error_detail=self._error,
        )


def _ctrl(kind, name="field1", **over):
    base = {"role": kind, "name": name, "name_source": "label-for",
            "best_effort": False, "kind": kind, "tag": "input",
            "input_type": "", "options": [], "required": False,
            "disabled": False, "frame_selector": "", "testid": "",
            "css_hint": "", "value_committed": "",
            "landmark": {"role": "", "name": ""}}
    base.update(over)
    return base


async def test_fill_intent_met_true_produces_action():
    port = IntentAwarePort(intent_met=True)
    result = await fill_form_phase_a(
        port, [_ctrl("text")],
        AnswerKey.from_payload({"exact": {"field1": "hello"}}),
        emit.MonotonicClock(), phase="explore", state_id="s1")
    assert result.filled == 1
    assert result.intent_unmet == 0


async def test_fill_intent_met_false_becomes_residue():
    port = IntentAwarePort(intent_met=False, committed_value="")
    result = await fill_form_phase_a(
        port, [_ctrl("text")],
        AnswerKey.from_payload({"exact": {"field1": "hello"}}),
        emit.MonotonicClock(), phase="explore", state_id="s1")
    assert result.filled == 0
    assert result.intent_unmet == 1
    assert len(result.unfilled_fields) == 1
    assert result.field_ledger[0]["provenance"] == PROV_INTENT_UNMET


async def test_fill_intent_met_none_preserves_action():
    port = IntentAwarePort(intent_met=None)
    result = await fill_form_phase_a(
        port, [_ctrl("text")],
        AnswerKey.from_payload({"exact": {"field1": "hello"}}),
        emit.MonotonicClock(), phase="explore", state_id="s1")
    assert result.filled == 1
    assert result.intent_unmet == 0


async def test_select_intent_unmet_becomes_residue():
    port = IntentAwarePort(intent_met=False, committed_value="")
    ctrl = _ctrl("select", options=["A", "B", "C"])
    result = await fill_form_phase_a(
        port, [ctrl],
        AnswerKey.from_payload({"exact": {"field1": "A"}}),
        emit.MonotonicClock(), phase="explore", state_id="s1")
    assert result.filled == 0
    assert result.intent_unmet == 1


async def test_toggle_intent_unmet_becomes_residue():
    port = IntentAwarePort(intent_met=False, committed_value="false")
    ctrl = _ctrl("radio", tag="div")
    result = await fill_form_phase_a(
        port, [ctrl],
        AnswerKey.from_payload({"exact": {"field1": "yes"}}),
        emit.MonotonicClock(), phase="explore", state_id="s1")
    assert result.filled == 0
    assert result.intent_unmet == 1


# ─── RawObservation carries intent fields ────────────────────────────────────


def test_raw_observation_intent_fields_default():
    obs = RawObservation()
    assert obs.intended_value == ""
    assert obs.intent_met is None


def test_raw_observation_intent_fields_set():
    obs = RawObservation(intended_value="hello", intent_met=True)
    assert obs.intended_value == "hello"
    assert obs.intent_met is True


def test_raw_observation_intent_false_on_error():
    obs = RawObservation(error_detail="locator_unresolved",
                         intended_value="hello", intent_met=False)
    assert obs.intent_met is False


# ─── Flow ledger summary carries intent_unmet ─────────────────────────────────


def test_flow_summary_intent_unmet():
    flows = [
        {"completed": True, "fully_answered": True, "step_count": 2,
         "steps": [
             {"fingerprint": "a", "url": "u", "title": "t",
              "fields_filled": 2, "fields_unfilled": 0},
             {"fingerprint": "b", "url": "u2", "title": "t2",
              "fields_filled": 1, "fields_unfilled": 1, "intent_unmet": 1},
         ]},
    ]
    s = flow_ledger.summarize(flows)
    assert s["intent_unmet"] == 1


def test_flow_summary_no_intent_unmet():
    flows = [
        {"completed": True, "fully_answered": True, "step_count": 1,
         "steps": [
             {"fingerprint": "a", "url": "u", "title": "t",
              "fields_filled": 3, "fields_unfilled": 0},
         ]},
    ]
    s = flow_ledger.summarize(flows)
    assert s["intent_unmet"] == 0


# ── R0 read-back: a radio commits its CHECKEDNESS, not its value attribute ────

class _FakeRadioLocator:
    """An <input type=radio value="term-life"> that IS checked.

    The point: ``input_value()`` does NOT raise on a radio — it returns the
    value attribute — so a reader that tries it first never falls through to
    ``is_checked()``."""

    def __init__(self, checked=True, value="term-life"):
        self._checked, self._value = checked, value

    async def input_value(self):
        return self._value

    async def is_checked(self):
        return self._checked

    async def inner_text(self):
        return ""


def test_checkable_read_back_reports_checkedness_not_the_value_attribute():
    """Regression: a genuinely selected product card read back as "term-life",
    never equalled the intended "true", and was recorded intent_unmet — so the
    branch was never marked walked and the crawl re-planned a choice it had
    already made, while the funnel had in fact opened."""
    import asyncio

    from app.main import PlaywrightBrowserPort

    port = PlaywrightBrowserPort.__new__(PlaywrightBrowserPort)   # no browser needed
    loc = _FakeRadioLocator()

    checkable = asyncio.run(port._read_value(loc, kind="checked"))
    assert checkable == "true", "a checkbox/radio commits its checked state"

    # Unchecked must be distinguishable, not just truthy.
    assert asyncio.run(
        port._read_value(_FakeRadioLocator(checked=False), kind="checked")
    ) == "false"

    # Non-checkable controls keep reading their value (selects, text inputs).
    assert asyncio.run(port._read_value(loc, kind="fill")) == "term-life"


class _FakeSelectLocator:
    """A <select> whose chosen option is labelled "$50,000" but whose value
    attribute is the code "50000" — the shape of virtually every enterprise
    dropdown (amounts, state codes, ids)."""

    async def input_value(self):
        return "50000"

    async def evaluate(self, _js):
        return "$50,000"

    async def is_checked(self):
        raise RuntimeError("not a checkbox")

    async def inner_text(self):
        return ""


def test_select_read_back_reports_the_option_LABEL_not_its_value_code():
    """We select BY LABEL, so the read-back must speak labels too.

    Regression: Coverage Amount and Term Length were both selected correctly and
    both recorded intent_unmet, because the read returned the value code. The
    only select that passed was the one whose label happened to equal its value
    — which is exactly why this hid for so long."""
    import asyncio

    from app.main import PlaywrightBrowserPort

    port = PlaywrightBrowserPort.__new__(PlaywrightBrowserPort)
    assert asyncio.run(
        port._read_value(_FakeSelectLocator(), kind="select")) == "$50,000"
    # A non-select control is unaffected and still reads its value.
    assert asyncio.run(
        port._read_value(_FakeSelectLocator(), kind="fill")) == "50000"
