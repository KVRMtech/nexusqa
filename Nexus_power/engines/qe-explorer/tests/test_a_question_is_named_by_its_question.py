"""A HEALTH PAGE'S 25 QUESTIONS MUST NOT BE RECORDED AS TWO FIELDS.

MEASURED (health-declaration fixture, 2026-08-29). A page carrying 25 Yes/No
questions -- the shape every life-insurance funnel is built from -- produced this
form_snapshot in its evidence bundle:

    {"No": "false", "Yes": "false"}

Two entries. The crawl's own inventory had it right in the same run --
`radio_groups=25 radio_grouped=50` -- so all 25 questions and all 50 options were
seen. The snapshot then keyed each control on its ACCESSIBLE NAME, which for a
radio is the OPTION ("Yes"), not the QUESTION, and 25 questions collapsed onto
two keys.

WHY THIS ONE MATTERS MORE THAN ITS SIZE. The client asks "prove every question on
the page was captured". The bundle is the proof, and the bundle said two fields
called Yes and No. Coverage of a questionnaire cannot be demonstrated from it,
and worse, the field ledger and the seed-request list inherit the same two keys.

THE WORDING IS ALREADY THERE. inventory_js captures `question_label` -- "the
application's OWN wording for that question, from a declared accessible-name rung
only (aria-labelledby -> aria-label -> <legend> -> heading inside the container)"
-- and "" when the page declared none, never inferred from layout. The snapshot
simply did not read it.

So: key by the question when the application declared one; keep the control's own
name when it did not. Two radios of one question share a key deliberately -- one
question is one row, and its value is whichever option is committed.
"""
from __future__ import annotations

from app.state_identity import _form_snapshot


def _radio(name, question, committed="", label_source="legend"):
    return {"name": name, "kind": "radio", "role": "radio",
            "question_label": question, "question_label_source": label_source,
            "group_options": ["Yes", "No"], "group_size": 2,
            "value_committed": committed, "input_type": "radio"}


def _text(name, committed="", question=""):
    return {"name": name, "kind": "text", "role": "textbox",
            "question_label": question, "question_label_source": "",
            "value_committed": committed, "input_type": "text"}


# ── the measured regression ────────────────────────────────────────────────

def test_two_questions_are_two_rows_not_one_yes_and_one_no():
    """THE BUG, at its smallest: 2 questions x Yes/No must not become {Yes,No}."""
    q1 = "Have you smoked tobacco in the last 12 months?"
    q2 = "Have you ever been diagnosed with heart disease?"
    snap, sig = _form_snapshot([
        _radio("Yes", q1), _radio("No", q1),
        _radio("Yes", q2), _radio("No", q2)])
    assert set(snap) == {q1, q2}, snap
    assert set(sig) == {q1, q2}


def test_twenty_five_questions_produce_twenty_five_rows():
    """The fixture's real shape."""
    controls = []
    for i in range(25):
        q = f"{i+1}. health question"
        controls += [_radio("Yes", q), _radio("No", q)]
    snap, _ = _form_snapshot(controls)
    assert len(snap) == 25, f"expected 25 questions, got {len(snap)}"


def test_the_committed_option_is_what_the_question_holds():
    q = "Do you have diabetes?"
    snap, _ = _form_snapshot([_radio("Yes", q, committed="Yes"), _radio("No", q)])
    assert snap[q] == "Yes"


# ── controls: what must NOT change ─────────────────────────────────────────

def test_a_control_with_no_declared_question_keeps_its_own_name():
    """THE CONTROL. Most fields are plain inputs and must be unaffected."""
    snap, _ = _form_snapshot([_text("Email", committed="a@b.c")])
    assert snap == {"Email": "a@b.c"}


def test_an_undeclared_question_is_never_invented():
    """question_label is "" when the page declared none -- never inferred."""
    snap, _ = _form_snapshot([_radio("Yes", ""), _radio("No", "")])
    assert set(snap) == {"Yes", "No"}


def test_a_named_text_field_inside_a_question_keeps_the_question():
    q = "Which condition were you diagnosed with?"
    snap, _ = _form_snapshot([_text("condition", committed="angina", question=q)])
    assert snap == {q: "angina"}
