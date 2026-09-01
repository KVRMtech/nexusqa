"""THE LEDGER FALLBACK IS KEYED BY URL, AND AN SPA SERVES MANY STATES FROM ONE.

The fallback exists to catch a field the page snapshot missed. On a five-step
wizard living at a single URL it instead attributed all five steps' fields to
every step, because ``ledger_by_url`` cannot tell the steps apart.

Live consequence on the Summit application: most catalogue questions appeared
TWICE — once from the state's own ``form_snapshot_signals`` and once from the
shared ledger — under two different ``question_id``s, because one basis is the
control SIGNATURE and the other the normalised NAME. Every fallback row also
claimed ``type: "text"``, since a ledger entry carries no control type, so a
number input and a date input both surfaced as text. A client opening that
catalogue sees each question duplicated and half of them mistyped.

Same root cause as the page-identity defect: a URL is not a state key in a
single-page application.

The rule: a state that produced signals has already described itself. Only a
state with NO signals — where the snapshot genuinely captured nothing — still
needs the ledger to speak on its behalf.
"""
from __future__ import annotations

from app.services.catalog import extract_controls


WIZARD_URL = "https://app/underwriting/new-application"


def _state(**signals):
    return {"location": WIZARD_URL, "form_snapshot_signals": signals}


#: One URL, five steps — so this ledger holds every field of the whole wizard.
WHOLE_WIZARD_LEDGER = {
    WIZARD_URL: [
        {"name": "First Name", "signature": "sig_first", "semantic_type": "given_name"},
        {"name": "Face Amount", "signature": "sig_face"},
        {"name": "Primary Physician", "signature": "sig_doc"},
    ]
}


def test_a_step_is_not_given_the_other_steps_fields():
    """Step 1 asks for a name. It does not ask for a face amount or a physician,
    and a catalogue that says it does describes a form the app never rendered."""
    got = extract_controls(
        _state(**{"First Name": {"type": "text", "required": True}}),
        WHOLE_WIZARD_LEDGER)
    assert [c["name"] for c in got] == ["First Name"]


def test_the_question_is_catalogued_once_not_twice():
    """Two ids for one question is the duplicate a client sees. The signals row
    keeps the ledger's SIGNATURE, so the surviving id is the stable one."""
    got = extract_controls(
        _state(**{"Face Amount": {"type": "text", "step": "10000"}}),
        WHOLE_WIZARD_LEDGER)
    assert len(got) == 1
    assert len({c["question_id"] for c in got}) == 1
    assert got[0]["signature"] == "sig_face"


def test_a_declared_rule_survives_the_merge():
    """The whole point of the row: step=10000 is what justifies a boundary
    scenario, and it exists only on the signals side."""
    got = extract_controls(
        _state(**{"Face Amount": {"type": "text", "step": "10000", "min": "10000"}}),
        WHOLE_WIZARD_LEDGER)
    assert got[0]["validation"] == {"min": "10000", "step": "10000"}


def test_a_state_that_captured_nothing_still_gets_its_fields():
    """The fallback's real job. A snapshot that captured no signals at all is
    exactly the case it was written for, and it must keep working."""
    got = extract_controls(
        {"location": WIZARD_URL, "form_snapshot_signals": {}},
        WHOLE_WIZARD_LEDGER)
    assert sorted(c["name"] for c in got) == [
        "Face Amount", "First Name", "Primary Physician"]


def test_a_fallback_row_still_carries_its_signature():
    got = extract_controls(
        {"location": WIZARD_URL, "form_snapshot_signals": {}},
        WHOLE_WIZARD_LEDGER)
    by_name = {c["name"]: c for c in got}
    assert by_name["First Name"]["signature"] == "sig_first"
    assert by_name["First Name"]["semantic_type"] == "given_name"


def test_a_page_with_no_ledger_and_no_signals_yields_nothing():
    assert extract_controls({"location": WIZARD_URL,
                             "form_snapshot_signals": {}}, {}) == []
