"""FILLING A CHOICE WHOSE OPTIONS DO NOT EXIST UNTIL IT IS OPENED.

THE LIVE FAILURE, traced end to end and confirmed in three independent places:

    manifest:  Gender → {type: select, options: [], value: ""}, and absent from
               the six labels actually typed on that page
    app source: canAdvance() requires fields.gender for step 0 (page.tsx:91)
                and Continue renders disabled={!canAdvance()} (page.tsx:396)
    crawl:     advances_by_tier: {} — one step deep, no journey

A shadcn/Radix ``<Select>`` renders as a ``<button role="combobox">`` whose
options live in a portal that does not exist until it is opened. The inventory
captured an empty enumeration; the resolver — correctly refusing to invent a
value for options it could not read — left the field empty; the application's own
validation then disabled Continue; and the wizard walk, equally correctly,
skipped a disabled control.

EVERY LAYER WAS HONEST. Nobody had opened the widget. That is the whole defect,
and it is not specific to this app: portal-rendered listboxes are the default in
every modern component library, so the same gap exists on most React apps built
in the last three years.

The fix defers the CHOICE to fill time rather than inventing one — the answer
comes from the application's own options, which is the opposite of guessing. What
must not be traded away is the read-back: a fill that cannot be verified is
recorded as unmet, because a field the crawl claims to have filled and did not is
a lie that fails later, somewhere else, wearing the application's face.
"""
from __future__ import annotations

import asyncio

from app.browser import RawObservation
from app.emit import MonotonicClock
from app.field_values import DATA_MODE_AGENT, DATA_MODE_USER
from app.forms import (
    CHOICE_OPEN_AND_PICK,
    AnswerKey,
    _is_open_choice,
    fill_form_phase_a,
    resolve_field,
)
from app.identity_pack import derive as derive_identity

_ID = derive_identity("open-choice")

#: The live control, as the inventory actually captured it.
GENDER = {
    "name": "Gender", "kind": "select", "tag": "button", "role": "combobox",
    "options": [], "value_committed": "", "required": False, "disabled": False,
}


def _option(name: str) -> dict:
    return {"name": name, "role": "option", "kind": "option", "tag": "div",
            "options": [], "disabled": False}


class RadixSelectPort:
    """A portal-rendered select: options exist only while it is open, and the
    trigger's accessible name becomes the selection once one is picked.

    ``settle_reads`` models the LIVE behaviour that broke the first version: the
    listbox animates closed and React re-renders asynchronously, so the first N
    reads after the click still show the popup open.

    ``combined_label`` models the other real variant — some libraries render the
    trigger as "Claim Type Death Claim" rather than replacing the label outright.
    """

    def __init__(self, *, options=("Male", "Female", "Other"),
                 commits: bool = True, opens: bool = True,
                 settle_reads: int = 0, combined_label: bool = False) -> None:
        self._options = list(options)
        self.commits = commits
        self.opens = opens
        self.settle_reads = settle_reads
        self.combined_label = combined_label
        self.open = False
        self.selected = ""
        self.escapes = 0
        self.reads_after_pick = 0

    async def collect_controls(self):
        trigger = dict(GENDER)
        if self.selected:
            shown = (f"Gender {self.selected}" if self.combined_label
                     else self.selected)
            trigger = {**trigger, "name": shown,
                       "value_committed": "" if self.combined_label
                       else self.selected}
        if self.selected:
            self.reads_after_pick += 1
            # The popup is still closing for the first ``settle_reads`` reads.
            if self.reads_after_pick <= self.settle_reads:
                return [trigger] + [_option(o) for o in self._options]
        if self.open:
            return [trigger] + [_option(o) for o in self._options]
        return [trigger]

    async def click(self, control):
        role = str(control.get("role") or "")
        if role == "option":
            if self.commits:
                self.selected = str(control.get("name") or "")
            self.open = False
            return RawObservation(url_before="/a", url_after="/a",
                                  committed_value=self.selected)
        self.open = bool(self.opens)
        return RawObservation(url_before="/a", url_after="/a")

    async def press_key(self, key):
        if key == "Escape":
            self.escapes += 1
            self.open = False

    async def fill(self, control, value):
        return RawObservation(url_before="/a", url_after="/a", committed_value=value)

    async def select_option(self, control, value):
        # The browser primitive on a non-<select> — this is what used to be tried.
        return RawObservation(url_before="/a", url_after="/a", intent_met=False)

    async def set_checked(self, control, checked):
        return RawObservation(url_before="/a", url_after="/a")


def _fill(port, control=GENDER, data_mode=DATA_MODE_AGENT):
    return asyncio.run(fill_form_phase_a(
        port, [control], AnswerKey.from_payload(None), MonotonicClock(),
        state_id="fp1", identity=_ID, data_mode=data_mode))


# ── the defect, closed ─────────────────────────────────────────────────────

def test_the_gender_select_that_blocked_the_whole_wizard_is_now_filled():
    """THE ONE THAT MATTERS. Six of seven step-1 fields filled and this one did
    not, so the app disabled Continue and the funnel was one step deep."""
    port = RadixSelectPort()
    result = _fill(port)

    assert result.filled == 1, "the widget was still not filled"
    assert result.unfilled_fields == []
    assert port.selected in ("Male", "Female", "Other")

    entry = result.field_ledger[0]
    assert entry["filled"] is True
    assert entry["provenance"] == "synthesized"


def test_the_ledger_records_the_option_the_form_ACTUALLY_took():
    """The requested value is a sentinel meaning "take a real one" — recording it
    would put a value in the ledger the form never contained."""
    port = RadixSelectPort(options=("Female", "Male"))
    result = _fill(port)

    entry = result.field_ledger[0]
    assert entry["choice"] == "female"                  # the committed label
    assert CHOICE_OPEN_AND_PICK not in str(entry)
    assert result.actions[0].value == "Female"
    assert result.actions[0].verb == "select"


def test_the_enumeration_the_widget_revealed_is_captured():
    """These are exactly the questions whose answers were hardest to get; an
    empty answer set for them is the worst place to keep one."""
    port = RadixSelectPort(options=("Male", "Female"))
    entry = _fill(port).field_ledger[0]
    assert entry.get("options"), "the revealed option was not recorded"


def test_the_resolver_defers_instead_of_giving_up():
    out = resolve_field(GENDER, "select", "Gender", AnswerKey.from_payload({}),
                        _ID, data_mode=DATA_MODE_AGENT)
    assert out["value"] == CHOICE_OPEN_AND_PICK
    assert out["entry"]["provenance"] == "synthesized"


def test_an_explicit_answer_key_value_still_wins_and_is_honoured():
    """Deferring the choice must not override a client who named the answer."""
    port = RadixSelectPort(options=("Male", "Female", "Other"))
    result = asyncio.run(fill_form_phase_a(
        port, [GENDER], AnswerKey.from_payload({"exact": {"Gender": "Other"}}),
        MonotonicClock(), state_id="fp1", identity=_ID,
        data_mode=DATA_MODE_AGENT))
    assert port.selected == "Other"
    assert result.field_ledger[0]["provenance"] == "provided"


# ── the two live failure modes ─────────────────────────────────────────────

def test_a_widget_that_animates_closed_is_still_verified():
    """THE LIVE FAILURE OF THE FIRST VERSION. Six selections were correctly
    opened, read and picked — "Claim Type" → "Death Claim" — and every one was
    discarded as unverified because the read ran before the popup had finished
    closing. The selection had succeeded; the verification was simply early."""
    port = RadixSelectPort(options=("Male", "Female"), settle_reads=2)
    result = _fill(port)

    assert result.filled == 1, "a settling widget still reads back as a failure"
    assert result.field_ledger[0]["provenance"] == "synthesized"
    assert port.reads_after_pick > 1, "the read was never retried"


def test_a_trigger_that_keeps_its_label_alongside_the_value_is_verified():
    """Some libraries render "Claim Type Death Claim" rather than replacing the
    label. Equality rejects that; anchored CONTAINMENT accepts it — and stays
    safe because it is anchored to the same control, not to the whole page."""
    port = RadixSelectPort(options=("Male", "Female"), combined_label=True)
    result = _fill(port)
    assert result.filled == 1
    assert result.field_ledger[0]["choice"] == "male"


def test_containment_does_not_match_an_unrelated_control_on_the_page():
    """THE RISK CONTAINMENT INTRODUCES, closed. Another control merely containing
    the picked word must not be read as this widget's committed value — only the
    same trigger (anchored by testid/css, or by still wearing its own label)
    counts."""
    from app.forms import _reads_back_as

    control = {"name": "Gender", "testid": "", "css_hint": ""}
    others = [
        {"name": "Male patients report", "value_committed": ""},   # prose
        {"name": "Gender", "value_committed": ""},                  # unchanged
    ]
    assert _reads_back_as(others, control, "Male") is False


def test_a_popup_that_never_closes_is_still_a_failure():
    """The settle budget must not become a way to wait until an unverifiable
    result is accepted. A widget that stays open past the budget failed."""
    port = RadixSelectPort(options=("Male",), settle_reads=99)
    result = _fill(port)
    assert result.filled == 0
    assert result.field_ledger[0]["provenance"] == "intent_unmet"


# ── honesty: an unverifiable fill is not a fill ────────────────────────────

def test_a_selection_that_does_not_read_back_is_recorded_as_UNMET():
    """THE LINE THIS MUST NOT CROSS. The click happened; the widget did not take
    it. Recording success would make the ledger claim a choice the form never
    holds — and the form then fails validation while the crawl blames the app."""
    port = RadixSelectPort(commits=False)
    result = _fill(port)

    assert result.filled == 0
    assert result.unfilled_fields == ["Gender"]
    assert result.field_ledger[0]["provenance"] == "intent_unmet"


def test_a_widget_that_never_opens_is_recorded_as_UNMET_and_restored():
    port = RadixSelectPort(opens=False)
    result = _fill(port)
    assert result.filled == 0
    assert result.field_ledger[0]["provenance"] == "intent_unmet"
    assert port.escapes >= 1, "an unopenable widget was left in an unknown state"


def test_user_mode_still_leaves_the_semantic_choice_to_the_client():
    """A radio group is the client's decision to make in user mode, and that
    contract is unchanged — deferral is an AGENT-mode behaviour."""
    out = resolve_field(GENDER, "select", "Gender", AnswerKey.from_payload({}),
                        _ID, data_mode=DATA_MODE_USER)
    assert out["value"] is None
    assert out["entry"]["provenance"] == "needs_input"


# ── routing: only a DECLARED custom widget is opened ───────────────────────

def test_a_native_select_keeps_the_browser_primitive():
    assert _is_open_choice({"tag": "select"}) is False


def test_an_unknown_tag_is_not_assumed_custom():
    """REGRESSION GUARD. Treating unknown as custom routed ordinary selects into
    the open-pick path and broke fills that had always worked."""
    assert _is_open_choice({}) is False
    assert _is_open_choice({"tag": ""}) is False


def test_a_declared_custom_trigger_is_opened():
    assert _is_open_choice({"tag": "button"}) is True
    assert _is_open_choice({"tag": "div"}) is True
