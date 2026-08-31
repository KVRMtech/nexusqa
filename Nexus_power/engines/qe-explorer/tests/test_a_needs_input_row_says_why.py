"""B4 (second half) — A NEEDS_INPUT ROW SAYS WHY, SO A SEED HAS A TARGET.

MEASURED on the live summit bundles (2026-08-31): ``data_account`` carried
``needs_input: 3`` and ``fields_needing_seed`` listed Gender, State, Face
Amount, Tobacco Use, Claim Type — with nothing on any row saying WHY.  "Supply
Health Conditions" and "supply Security PIN" look identical on that list and
need opposite responses: one is a choice the crawl declined to make on the
client's behalf, the other a secret it must never invent, and a third class —
a widget the engine has no primitive for — cannot be fixed by any seed at all.

The vocabulary is tiny, closed and mechanical (``forms._needs_input_reason``),
because a reason is only useful if the same cause always produces the same
string.  Additive and conditional: only rows already ``needs_input`` grow the
key, so every other row is byte-identical and the goldens hold.
"""
from __future__ import annotations

import asyncio

from app import field_values
from app.fill_engine import widgets as fe_widgets
from app.forms import (AnswerKey, PROV_NEEDS_INPUT, _needs_input_reason,
                       fill_form_phase_a)
from tests.characterization.harness import ScriptedBrowser, ScriptedPage, control


# ── the vocabulary, cause by cause ─────────────────────────────────────────

def _entry(**over):
    base = {"sensitive": False, "provenance": PROV_NEEDS_INPUT}
    base.update(over)
    return base


def test_an_unanswerable_widget_names_its_class():
    """The one reason a seed alone can never fix — so the widget class is
    named, making it a work item rather than a mystery."""
    widget = fe_widgets.classify_widget(
        {"kind": "slider", "role": "slider", "tag": "div", "name": "Risk"},
        kind="slider")
    assert not widget.answerable, (
        "control: this fixture must actually be unanswerable, or the test "
        "asserts a branch that cannot be reached")
    reason = _needs_input_reason(widget, _entry(), "slider", "user")
    assert reason == "widget_unhandled:%s" % widget.name


def test_a_secret_is_never_invented_and_says_so():
    widget = fe_widgets.classify_widget(
        {"kind": "text", "role": "textbox", "tag": "input", "name": "PIN"},
        kind="text")
    reason = _needs_input_reason(widget, _entry(sensitive=True), "text", "user")
    assert reason == "secret_never_invented"


def test_a_declined_choice_says_it_belongs_to_the_client():
    widget = fe_widgets.classify_widget(
        {"kind": "radio", "role": "radio", "tag": "input", "name": "Yes"},
        kind="radio")
    reason = _needs_input_reason(widget, _entry(), "radio", "user")
    assert reason == "choice_left_to_client"


def test_control_the_same_choice_in_agent_mode_is_not_that_reason():
    """The reason must track the MODE, not the kind: in agent mode a declined
    enumerable fell through every rung, which is the ordinary ask."""
    widget = fe_widgets.classify_widget(
        {"kind": "radio", "role": "radio", "tag": "input", "name": "Yes"},
        kind="radio")
    reason = _needs_input_reason(
        widget, _entry(), "radio", field_values.DATA_MODE_AGENT)
    assert reason == "no_value_rung_answered"


def test_everything_else_is_the_ordinary_ask():
    widget = fe_widgets.classify_widget(
        {"kind": "text", "role": "textbox", "tag": "input", "name": "Notes"},
        kind="text")
    assert _needs_input_reason(widget, _entry(), "text", "user") == (
        "no_value_rung_answered")


# ── through the real fill, onto the real ledger ────────────────────────────

def test_the_reason_reaches_the_ledger_row_the_seed_request_is_built_from():
    """END TO END through ``fill_form_phase_a``: a user-mode radio question the
    fill declines produces ledger rows that carry the reason — and rows that
    FILLED carry none, which is the additive-and-conditional guarantee."""
    port = ScriptedBrowser(
        {"p": ScriptedPage(url="http://x/apply", controls=[])}, "p")
    controls = [
        control("radio", "Yes", tag="input", kind="radio",
                group_id="g1", question_label="Do you use tobacco?"),
        control("radio", "No", tag="input", kind="radio",
                group_id="g1", question_label="Do you use tobacco?"),
        control("textbox", "Full Name", tag="input", kind="text"),
    ]
    result = asyncio.run(fill_form_phase_a(
        port, controls, AnswerKey(), _Clock(), state_id="s1",
        data_mode="user"))
    rows = {r.get("name"): r for r in result.field_ledger}
    asked = rows["Do you use tobacco?"]
    assert asked["provenance"] == PROV_NEEDS_INPUT
    assert asked["reason"] == "choice_left_to_client", (
        "the seed request built from this row must say the question is the "
        "client's to answer, not beg for a value")
    filled = rows["Full Name"]
    assert filled["filled"] is True
    assert "reason" not in filled, "a row that answered owes no excuse"


class _Clock:
    def __init__(self):
        self._t = 0

    def now_ms(self):
        self._t += 1
        return self._t
