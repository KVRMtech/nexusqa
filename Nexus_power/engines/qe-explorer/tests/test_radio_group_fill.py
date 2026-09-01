"""A RADIO GROUP IS ONE QUESTION WITH ONE ANSWER.

Two defects, both fleet-wide, both found by walking a five-step application
wizard that stopped two steps short of the end.

1. AGENT MODE COULD NOT ANSWER A RADIO GROUP AT ALL.
   GROUP_ASSEMBLE writes a question's answers to ``group_options`` and
   deliberately NOT to ``options``, so a radio's field signature does not shift
   when a sibling appears. The value generator read only ``options``, so a
   grouped radio looked like a control offering nothing and resolved to
   ``needs_input`` — in the one mode whose entire purpose is to answer a
   semantic choice, for the most common semantic choice there is.

   Live consequence: the Coverage step filled 3 of 5 fields, the application's
   own validation kept Continue disabled, and the walk stopped at step 3 of 5.

2. EVERY MEMBER RESOLVED TO THE SAME ANSWER.
   Filling each in turn checks them one after another and the browser unchecks
   the previous, so the LAST member wins regardless of which option was chosen.
   The form ends validly answered and the ledger records the option we picked
   rather than the one now selected — a recorded choice that contradicts the
   DOM. That is the failure this product exists to prevent, committed by the
   product.
"""
from __future__ import annotations

import asyncio

from app.browser import RawObservation
from app.config import Settings
from app.emit import MonotonicClock
from app.field_values import DATA_MODE_AGENT, DATA_MODE_USER
from app.forms import PROV_GROUP_SIBLING, AnswerKey, fill_form_phase_a
from app.guard import load_refuse_pack
from app.identity_pack import derive
from app.inventory import build_inventory

_REFUSE = load_refuse_pack(Settings().refuse_pack_path)
_ID = derive("radio-seed")


class _Port:
    """Records which controls were actually SELECTED."""

    def __init__(self) -> None:
        self.checked: list[str] = []

    async def set_checked(self, control, value):
        if value:
            self.checked.append(str(control.get("name") or ""))
        return RawObservation(url_before="/a", url_after="/a",
                              committed_value="true" if value else "false")

    async def fill(self, control, value):
        return RawObservation(url_before="/a", url_after="/a", committed_value=value)

    async def select_option(self, control, value):
        return RawObservation(url_before="/a", url_after="/a", committed_value=value)

    async def click(self, control):
        return RawObservation(url_before="/a", url_after="/a")


def _radio(name: str, group: str = "tobaccoUse") -> dict:
    return {
        "role": "radio", "name": name, "name_source": "content",
        "best_effort": False, "kind": "radio", "tag": "input",
        "input_type": "radio", "options": [], "required": False,
        "disabled": False, "frame_selector": "", "testid": "", "css_hint": "",
        "value_committed": "false", "group_key": group,
        "landmark": {"role": "", "name": ""},
    }


def _fill(labels, *, mode=DATA_MODE_AGENT, group="tobaccoUse"):
    built = build_inventory([_radio(n, group) for n in labels], _REFUSE,
                            url="https://app.example/a")
    port = _Port()
    result = asyncio.run(fill_form_phase_a(
        port, built, AnswerKey.from_payload(None), MonotonicClock(),
        state_id="fp", identity=_ID, data_mode=mode))
    return port, result


# ── 1. the group is answerable at all ──────────────────────────────────────

def test_agent_mode_answers_a_radio_group():
    """THE WIZARD BLOCKER. Before this, a grouped radio resolved to needs_input
    and the application's validation kept the funnel shut."""
    port, result = _fill(["No", "Yes"])
    assert result.filled == 1
    assert len(port.checked) == 1
    assert port.checked[0] in ("No", "Yes")


def test_the_enumeration_is_read_from_group_options():
    """``options`` is empty on a grouped radio by design; the answers live in
    ``group_options``. Reading only the former is what made the group look like
    a control offering nothing."""
    built = build_inventory([_radio("No"), _radio("Yes")], _REFUSE,
                            url="https://app.example/a")
    assert built[0]["options"] == []
    assert built[0]["group_options"] == ["No", "Yes"]


def test_a_multi_option_group_is_answered_once():
    port, result = _fill(["Monthly", "Quarterly", "Annual"], group="premiumMode")
    assert result.filled == 1
    assert len(port.checked) == 1
    assert port.checked[0] in ("Monthly", "Quarterly", "Annual")


# ── 2. one answer, and the ledger matches the DOM ──────────────────────────

def test_only_the_chosen_member_is_selected():
    """Every member resolving to the same answer meant each was checked in turn
    and the browser unchecked the previous — the LAST won, whichever was
    chosen."""
    port, _ = _fill(["No", "Yes"])
    assert len(port.checked) == 1, (
        f"more than one member was selected: {port.checked}")


def test_the_ledger_records_the_option_that_is_actually_selected():
    """A recorded choice that contradicts the DOM is the failure this product
    exists to prevent."""
    port, result = _fill(["No", "Yes"])
    chosen = port.checked[0]
    answered = [e for e in result.field_ledger
                if e["provenance"] not in (PROV_GROUP_SIBLING,)
                and e.get("filled")]
    assert len(answered) == 1
    assert answered[0]["name"] == chosen


def test_a_sibling_is_never_asked_for_as_residue():
    """Its question WAS answered. Listing it would ask the client to supply a
    value we already chose."""
    _, result = _fill(["No", "Yes"])
    assert result.unfilled_fields == []
    siblings = [e for e in result.field_ledger
                if e["provenance"] == PROV_GROUP_SIBLING]
    assert len(siblings) == 1


# ── 3. what must not change ────────────────────────────────────────────────

def test_user_mode_still_leaves_the_choice_to_the_client():
    """A radio group is a semantic decision; in user mode it stays the client's
    to make, and both members are honestly reported as unanswered."""
    port, result = _fill(["No", "Yes"], mode=DATA_MODE_USER)
    assert result.filled == 0
    assert port.checked == []
    assert sorted(result.unfilled_fields) == ["No", "Yes"]


def test_an_ungrouped_radio_is_unaffected():
    """Without a group there is no sibling relationship to reason about."""
    built = build_inventory([_radio("Standalone", group="")], _REFUSE,
                            url="https://app.example/a")
    port = _Port()
    result = asyncio.run(fill_form_phase_a(
        port, built, AnswerKey.from_payload(None), MonotonicClock(),
        state_id="fp", identity=_ID, data_mode=DATA_MODE_AGENT))
    assert result.filled + len(result.unfilled_fields) == 1
