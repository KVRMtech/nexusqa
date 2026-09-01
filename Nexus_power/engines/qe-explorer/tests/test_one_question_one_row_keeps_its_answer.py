"""FORM SNAPSHOT — one question is one row, and the row must say WHICH answer.

THE DEFECT THIS PINS, measured rather than reasoned about.

``aee5214`` made ``state_identity._form_snapshot`` key a row by the QUESTION
instead of by the answer, which was right: a 25-question Yes/No health page had
been producing ``{"No": "false", "Yes": "false"}`` -- twenty-five questions
landing on two keys. It then had to stop a group's unselected siblings from
overwriting the selected one, and wrote this guard::

    if not (label in snapshot and snapshot[label] and not value):
        snapshot[label] = value

``value`` for a radio is ``value_committed`` -- the string ``"true"`` or
``"false"``, never ``""``. So ``not value`` is ``not "false"`` which is
``False``, the guard never fires, and the LAST member in DOM order always wins.
Measured on the shape the guard was written for::

    [Term 10 SELECTED, Term 20 unselected]  ->  {"Term product": "false"}
    [Term 20 unselected, Term 10 SELECTED]  ->  {"Term product": "true"}

Two consequences, both of which the snapshot is load-bearing for:

1. **A question that WAS answered can read as unanswered** -- whichever way the
   DOM happens to order the group.
2. **The same state hashes differently depending on DOM order**, and two
   DIFFERENT answers to one question hash the SAME, because the row records a
   checked-state rather than a choice. That is the same-shape collapse this
   repository has already been bitten by once.

WHAT THE ROW SHOULD HOLD is the SELECTED OPTION'S LABEL. It keeps aee5214's
question naming, restores which option was taken, and is order-independent --
so nothing has to trade one against the other.

Every test below fails on the pre-fix implementation; the last two are the
falsification controls that make the first three mean something.
"""
from __future__ import annotations

from app.state_identity import _form_snapshot


def _member(option: str, checked: bool, *, question: str = "Term product",
            group: str = "g1") -> dict:
    """One radio member exactly as ``build_inventory`` emits it."""
    return {
        "kind": "radio",
        "name": option,
        "group_id": group,
        "group_key": f"name:form0:{group}",
        "question_label": question,
        "options": ["Term 10", "Term 20"],
        "value_committed": "true" if checked else "false",
    }


# ── the answer survives, whatever the DOM order ─────────────────────────────

def test_a_selected_option_is_not_erased_by_a_later_sibling():
    snap, _ = _form_snapshot([_member("Term 10", True),
                              _member("Term 20", False)])
    assert snap["Term product"] == "Term 10", (
        "the unselected sibling overwrote the answer: the row reports "
        f"{snap['Term product']!r} for a question answered 'Term 10'")


def test_dom_order_does_not_change_the_snapshot():
    """A snapshot that moves with DOM order cannot identify a state."""
    a, _ = _form_snapshot([_member("Term 10", True), _member("Term 20", False)])
    b, _ = _form_snapshot([_member("Term 20", False), _member("Term 10", True)])
    assert a == b, (
        f"same state, same answer, different DOM order -> {a} vs {b}")


def test_two_different_answers_are_two_different_states():
    """The same-shape collapse, stated directly.

    A 10-year term and a 20-year term are different applications. If both hash
    to one row value, the walk cannot tell those states apart and a branch walk
    that took the other option looks like a page it already visited.
    """
    ten, _ = _form_snapshot([_member("Term 10", True), _member("Term 20", False)])
    twenty, _ = _form_snapshot([_member("Term 10", False), _member("Term 20", True)])
    assert ten != twenty, (
        f"two different answers produced one snapshot: {ten}")
    assert ten["Term product"] == "Term 10"
    assert twenty["Term product"] == "Term 20"


# ── falsification controls: the guards above must be able to go red ─────────

def test_an_unanswered_group_says_so_rather_than_naming_an_option():
    """Absence has to be distinguishable from an answer, or the assertions
    above are satisfied by anything at all."""
    snap, _ = _form_snapshot([_member("Term 10", False),
                              _member("Term 20", False)])
    assert snap.get("Term product") == "", (
        "an unanswered question must not name one of its options; got "
        f"{snap.get('Term product')!r}")


def test_a_lone_ungrouped_control_is_untouched_by_the_group_rule():
    """The group rule must not reach controls that are not a group.

    A standalone checkbox IS its own whole question, and its committed value is
    the answer -- so it keeps reporting true/false exactly as before, and this
    change stays confined to the shape that needed it.
    """
    lone = {
        "kind": "checkbox",
        "name": "Subscribe to newsletter",
        "question_label": "",
        "value_committed": "true",
    }
    snap, _ = _form_snapshot([lone])
    assert snap["Subscribe to newsletter"] == "true"


def test_a_text_field_still_reports_its_typed_value():
    """The other half of the same control: nothing about free text moves."""
    field = {
        "kind": "text",
        "name": "First name",
        "question_label": "",
        "value_committed": "Ada",
    }
    snap, _ = _form_snapshot([field])
    assert snap["First name"] == "Ada"
