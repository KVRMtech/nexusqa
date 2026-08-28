"""THREE DOCUMENTS, THREE SIGNATURES, ONE LABEL.

`boundary_key` is `(url_template, normalised label)` and its docstring explains
why it is NOT the state fingerprint: a fingerprint moves when a banner appears
or a follow-up block is revealed, so keying exactly-once on it would authorise a
SECOND irreversible submit of the same application. That reasoning is right and
is preserved here.

WHAT THE LABEL PROXY CANNOT EXPRESS. It assumes one button per page per action.
An application that renders the SAME action once per object — sign each of three
documents, void each of four invoices — presents N genuinely different controls
under one label, and the ledger sees one boundary.

MEASURED (LifeOps, 2026-08-27). Three `Sign` buttons, each with its own stable
test id:

    action-startSignDocument-electronic-consent
    action-startSignDocument-application-ack
    action-startSignDocument-illustration-receipt

With `Sign` granted for THREE crossings, two landed and the third was refused
`already_crossed`. `qec.wizard.gate_step` recorded `fp_moved=False` after the
second: signing a document changes the page's TEXT, not its shape, so the
fingerprint stopped moving and the third crossing collided with the second's
exactly-once key. The application's own gate needs all three, so it never
opened.

THE SPLIT THIS PINS, and why it does not widen anything:

  * the BUDGET key stays `(url, label)`. `max_crossings` therefore still caps
    the TOTAL — three signatures authorised means three, not three per button;
  * the EXACTLY-ONCE key gains the control's own stable identity, so the SAME
    button still cannot be crossed twice while three DIFFERENT buttons are
    three different slots.

STABLE IDENTIFIERS ONLY. A positional locator ("the 3rd button") is not
identity: the same button acquires a new one when a row above it disappears,
and two keys for one button is exactly the double-submit this ledger exists to
prevent. Only an app-authored id counts; anything else falls back to today's
behaviour byte for byte.
"""
from __future__ import annotations

import pytest

from app.boundary import CrossingLedger, stable_control_ref


def _ctl(ref: str, strategy: str = "testid") -> dict:
    return {"name": "Sign", "kind": "button",
            "locator": {"strategy": strategy, "value": ref, "verified": True}}


URL = "https://app.test/documents"
A, B, C = _ctl("sign-consent"), _ctl("sign-ack"), _ctl("sign-receipt")


def _reserve(ledger, control, fp="fp-docs", seq=1):
    return ledger.reserve(control_name="Sign", url=URL, state_fingerprint=fp,
                          control_ref=stable_control_ref(control),
                          approval_id="apr-1", sequence_index=seq)


def _exceeds(ledger, control, fp="fp-docs", budget=3):
    return ledger.would_exceed(control_name="Sign", url=URL, state_fingerprint=fp,
                               control_ref=stable_control_ref(control),
                               max_crossings=budget)


def test_a_second_document_is_a_second_boundary_not_a_repeat():
    """THE DEFECT. Same label, same page, same fingerprint — a DIFFERENT
    button, and one the operator authorised."""
    led = CrossingLedger()
    _reserve(led, A)
    assert not _exceeds(led, B), "signing the second document was refused as a repeat"
    _reserve(led, B, seq=2)
    assert not _exceeds(led, C), "signing the third document was refused as a repeat"


def test_the_same_button_still_cannot_be_crossed_twice():
    """THE CONTROL THAT MATTERS MOST. Exactly-once per control is the whole
    point of this ledger and is unchanged."""
    led = CrossingLedger()
    _reserve(led, A)
    assert _exceeds(led, A), "the SAME button was offered a second crossing"


def test_the_grant_budget_still_caps_the_total():
    """THE OTHER CONTROL. Making the exactly-once key finer must not turn a
    three-crossing grant into three crossings PER BUTTON."""
    led = CrossingLedger()
    for i, c in enumerate((A, B, C), start=1):
        assert not _exceeds(led, c, budget=3)
        _reserve(led, c, seq=i)
    assert _exceeds(led, _ctl("sign-fourth"), budget=3), (
        "a fourth crossing was allowed against a budget of three")


@pytest.mark.parametrize("strategy", ["css", "nth", "role", "text"])
def test_a_positional_locator_is_not_identity(strategy):
    """A locator that describes WHERE a control sits changes when the page
    above it changes. Two keys for one button is the double-submit this ledger
    exists to prevent, so anything but an app-authored id is ignored."""
    assert stable_control_ref(_ctl("button:nth-of-type(3)", strategy)) == ""


def test_a_control_with_no_identifier_behaves_exactly_as_before():
    """No id, no change: the key is what it has always been."""
    led = CrossingLedger()
    bare = {"name": "Sign", "kind": "button"}
    assert stable_control_ref(bare) == ""
    _reserve(led, bare)
    assert _exceeds(led, bare)
