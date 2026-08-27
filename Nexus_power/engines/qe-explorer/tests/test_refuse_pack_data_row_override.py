"""A CUSTOMER RECORD IS NOT A CONTROL THAT ACTS ON IT.

`allow_overrides` is the refuse pack's only escape hatch, and its header says
adding a row is "an auditable, human-reviewed decision". This module is the
audit for the second row. As with the first, it asserts what the row PERMITS
and — at greater length, because this is the half that matters — what it must
still REFUSE.

WHY THIS ROW EXISTS. `rp.verb.*` rules match a regex against a control's
accessible NAME. In a carrier back-office the accessible name of a table ROW is
the customer record it displays, and those records routinely contain the
industry's irreversible vocabulary as ordinary data. Measured on
summit-life-carrier (2026-08-27, from 7d7408b), this was flagged
`rp.verb.underwrite`, severity CRITICAL:

    "Michael Thornberry UW-2026-00142 $2,000,000.00 underwriting"

It is a row in a case list. It underwrites nothing; the word is the case's
STATUS. 15 distinct label+rule over-blocks were measured across four
applications that day, and the same class was independently reproduced on a
third-party platform whose site navigation carries an "Underwriting" section.

WHY IT IS SAFE. The regex demands TWO independent data shapes in the same
label — a formatted currency amount AND a reference identifier — and then
refuses anyway if the label OPENS with a command verb. A control that acts is
named for the act, and it is named that FIRST: "Process Payment", "Pay invoice
INV-2026-00142 $2,000,000.00". A row is named for its subject. Scoped to
`button_name` only, so no GET can be unblocked by it.

WHAT IS DELIBERATELY NOT FIXED. The other measured shape — a bare navigation
item whose whole name is a section noun ("Underwriting" in a site nav) — is NOT
covered here. Distinguishing it from a genuine one-word command needs
structural context (the control's landmark ancestry) that
`classify_action_verb` does not receive, and inventing a name-only rule for it
would risk under-blocking a real commit. That remains open, by choice.
"""
from __future__ import annotations

import pytest

from app.config import Settings
from app.guard import classify_action_verb, load_refuse_pack

_PACK = load_refuse_pack(Settings().refuse_pack_path)
_OVERRIDE_ID = "rp.allow.customer_record_row"


def _verdict(name: str):
    return classify_action_verb(name, "https://app.example/cases", _PACK)


# ── what the row permits ────────────────────────────────────────────────────

@pytest.mark.parametrize("label", [
    # THE MEASURED LABEL, verbatim from the summit-life-carrier crawl.
    "Michael Thornberry UW-2026-00142 $2,000,000.00 underwriting",
    "Jennifer Walsh UW-2026-00145 $3,000,000.00 submitted",
    "Avery Lee POL-2026-00311 $750,000.00 lapsed",
])
def test_a_customer_record_row_is_data_not_an_irreversible_control(label):
    v = _verdict(label)
    assert not v.irreversible, f"a case-list row was refused as a commit: {label!r}"
    assert v.rule_id == _OVERRIDE_ID


# ── what it must still refuse (the half that matters) ───────────────────────

@pytest.mark.parametrize("label,why", [
    ("Process Payment", "a bare command — no record data at all"),
    ("Sign & Submit Application", "the real commit on the proving-ground funnel"),
    ("Underwrite Now", "a one-word command plus an adverb"),
    ("Delete", "the shortest destructive command there is"),
    # Both data shapes present, but the label OPENS with the act — so it names
    # what it does, and a record does not.
    ("Pay invoice INV-2026-00142 $2,000,000.00", "opens with the command verb"),
    ("Transfer $2,000,000.00 to account ACC-2026-00999", "opens with the command verb"),
    ("Delete policy POL-2026-00123 $1,500.00", "opens with the command verb"),
    ("Surrender contract UW-2026-00142 $2,000,000.00", "opens with the command verb"),
    # Only ONE data shape — not a record.
    ("Pay $2,000,000.00", "currency but no reference id"),
    ("Approve UW-2026-00142", "reference id but no amount"),
])
def test_the_override_does_not_reach_a_control_that_acts(label, why):
    v = _verdict(label)
    assert v.irreversible, f"{label!r} was allowed through — {why}"
    assert v.rule_id != _OVERRIDE_ID


def test_the_override_is_scoped_to_the_control_name_only():
    """No URL can be unblocked by this row: a GET whose path carries the
    verb must stay refused however record-shaped the query looks."""
    rule = next(r for r in _PACK.allow_overrides if r.id == _OVERRIDE_ID)
    assert tuple(rule.applies_to) == ("button_name",)


def test_the_verb_pack_does_not_govern_submit_application_and_never_did():
    """A BOUNDARY OF THIS FUNCTION, recorded so the next reader does not mistake
    it for a hole this row opened.

    "Submit Application" is NOT refused by `classify_action_verb` — the pack has
    no bare `submit` verb rule. Measured on summit-life-carrier the same day, it
    is caught one layer up as `commit_shaped_label` (severity high, rule_id "").
    Asserting it here would have tested the wrong subsystem; asserting the truth
    keeps the two layers legible.
    """
    v = _verdict("Submit Application")
    assert not v.irreversible
    assert v.rule_id != _OVERRIDE_ID   # and NOT because this row let it through
