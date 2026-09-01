"""R1 Interaction Ladder — per-archetype mechanic rungs tried until R0 verifies.

Tests the pure ladder definitions (interaction_ladder.py), the ladder execution
via ``_act_with_ladder`` wiring in forms (integration), and the governance rule
that danger/submit controls are NEVER laddered.
"""
from __future__ import annotations

from app.browser import RawObservation, verify_intent
from app.interaction_ladder import (
    CHECKBOX_LADDER,
    DATE_LADDER,
    RADIO_LADDER,
    Rung,
    SELECT_LADDER,
    SLIDER_LADDER,
    TEXT_LADDER,
    archetype_has_ladder,
    ladder_for,
)
from app.forms import (
    AnswerKey,
    PROV_INTENT_UNMET,
    fill_form_phase_a,
)
from app import emit


# ─── Pure ladder definitions ─────────────────────────────────────────────────


class TestLadderDefinitions:
    def test_radio_has_ladder(self):
        assert archetype_has_ladder("radio")
        assert len(RADIO_LADDER) >= 2

    def test_checkbox_has_ladder(self):
        assert archetype_has_ladder("checkbox")

    def test_toggle_shares_checkbox_ladder(self):
        assert ladder_for("toggle") == CHECKBOX_LADDER

    def test_select_has_ladder(self):
        assert archetype_has_ladder("select")
        assert len(SELECT_LADDER) >= 2

    def test_slider_has_ladder(self):
        assert archetype_has_ladder("slider")

    def test_date_has_ladder(self):
        assert archetype_has_ladder("date")

    def test_text_has_ladder(self):
        assert archetype_has_ladder("text")

    def test_button_has_no_ladder(self):
        assert not archetype_has_ladder("button")

    def test_native_rung_is_always_first(self):
        assert RADIO_LADDER[0].kind == "checked"
        assert SELECT_LADDER[0].kind == "select"
        assert SLIDER_LADDER[0].kind == "fill"
        assert DATE_LADDER[0].kind == "fill"
        assert TEXT_LADDER[0].kind == "fill"

    def test_rung_variants_are_descriptive(self):
        for ladder in (RADIO_LADDER, SELECT_LADDER, SLIDER_LADDER,
                       DATE_LADDER, TEXT_LADDER, CHECKBOX_LADDER):
            for rung in ladder:
                assert rung.variant, f"empty variant on {rung}"
                assert "_" in rung.variant or len(rung.variant) > 3

    def test_unknown_kind_returns_empty(self):
        assert ladder_for("unknown") == ()
        assert ladder_for("button") == ()

    def test_case_insensitive(self):
        assert ladder_for("Radio") == RADIO_LADDER
        assert ladder_for("SELECT") == SELECT_LADDER


# ─── Integration: ladder retry via forms ─────────────────────────────────────


class FallbackPort:
    """A fake port where the NATIVE mechanic fails but a FALLBACK succeeds.

    Simulates the exact scenario from the client demo: set_checked fails on
    a custom card, but click succeeds.
    """

    def __init__(self, *, fail_native=True, fallback_value="true"):
        self._fail_native = fail_native
        self._fallback_value = fallback_value
        self.attempts: list[str] = []

    async def fill(self, control, value):
        self.attempts.append("fill")
        if self._fail_native:
            return RawObservation(
                url_before="u", url_after="u", committed_value="",
                intended_value=value, intent_met=False,
                error_detail="action_error: fill failed")
        return RawObservation(
            url_before="u", url_after="u", committed_value=value,
            intended_value=value, intent_met=True)

    async def select_option(self, control, value):
        self.attempts.append("select")
        if self._fail_native:
            return RawObservation(
                url_before="u", url_after="u", committed_value="",
                intended_value=value, intent_met=False,
                error_detail="action_error: select failed")
        return RawObservation(
            url_before="u", url_after="u", committed_value=value,
            intended_value=value, intent_met=True)

    async def set_checked(self, control, checked):
        self.attempts.append("set_checked")
        cv = "true" if checked else "false"
        if self._fail_native:
            return RawObservation(
                url_before="u", url_after="u", committed_value="",
                intended_value=cv, intent_met=False,
                error_detail="action_error: Timeout 5000ms")
        return RawObservation(
            url_before="u", url_after="u", committed_value=cv,
            intended_value=cv, intent_met=True,
            mechanic_used="native_set_checked")


def _ctrl(kind, name="field1", **over):
    base = {"role": kind, "name": name, "name_source": "label-for",
            "best_effort": False, "kind": kind, "tag": "input",
            "input_type": "", "options": [], "required": False,
            "disabled": False, "frame_selector": "", "testid": "",
            "css_hint": "", "value_committed": "",
            "landmark": {"role": "", "name": ""}}
    base.update(over)
    return base


async def test_native_success_no_ladder_needed():
    """When the native mechanic succeeds, no fallback is tried."""
    port = FallbackPort(fail_native=False)
    result = await fill_form_phase_a(
        port, [_ctrl("text")],
        AnswerKey.from_payload({"exact": {"field1": "hello"}}),
        emit.MonotonicClock(), phase="explore", state_id="s1")
    assert result.filled == 1
    assert result.intent_unmet == 0


async def test_native_fail_intent_unmet_recorded():
    """When ALL ladder rungs fail (simulated by port returning intent_met=False
    for every call), the fill becomes honest residue."""
    port = FallbackPort(fail_native=True)
    result = await fill_form_phase_a(
        port, [_ctrl("text")],
        AnswerKey.from_payload({"exact": {"field1": "hello"}}),
        emit.MonotonicClock(), phase="explore", state_id="s1")
    assert result.filled == 0
    assert result.intent_unmet == 1
    assert result.field_ledger[0]["provenance"] == PROV_INTENT_UNMET


# ─── mechanic_used field ─────────────────────────────────────────────────────


def test_mechanic_used_default_empty():
    obs = RawObservation()
    assert obs.mechanic_used == ""


def test_mechanic_used_set():
    obs = RawObservation(mechanic_used="click_element")
    assert obs.mechanic_used == "click_element"


# ─── Governance: danger controls never laddered ──────────────────────────────


def test_button_kind_has_no_ladder():
    """Buttons (submit/danger) must never be retried via a ladder — the submit
    boundary is upstream and unchanged."""
    assert ladder_for("button") == ()


def test_link_kind_has_no_ladder():
    assert ladder_for("link") == ()
