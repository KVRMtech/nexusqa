"""THE FIELD LEDGER MUST ASK THE CLIENT ABOUT THE QUESTION, NOT THE ANSWER.

MEASURED (health-declaration fixture, 2026-08-29), after form_snapshot was fixed
to name a row by its question. The bundle's SNAPSHOT then showed 25 questions --
and its LEDGER still showed this:

    field_ledger        : 2 entries
    fields_needing_seed : 2
        {"label": "Yes", "url": "http://127.0.0.1:8099/"}
        {"label": "No",  "url": "http://127.0.0.1:8099/"}

Two defects with one root cause, on two different paths out of the inventory.
`state_identity._form_snapshot` was one; `forms.py` is the other, and it is the
more consequential of the two: the ledger is what becomes the SEED REQUEST sent
to the client. A 25-question health page asked its client to supply values for
two fields named "Yes" and "No".

`question_name_of` puts the rule in ONE place so the two paths cannot drift:
the application's declared wording when it declared one, the control's own name
when it did not, and never an invention. A BUTTON is never a question and always
keeps its own name -- "Submit Declaration" is not something anyone answers.
"""
from __future__ import annotations

from app.inventory import question_name_of


def test_a_radio_answers_under_its_question():
    assert question_name_of({
        "name": "Yes", "kind": "radio",
        "question_label": "Do you have diabetes?"}) == "Do you have diabetes?"


def test_the_sibling_answers_under_the_same_question():
    q = "Do you have diabetes?"
    a = question_name_of({"name": "Yes", "kind": "radio", "question_label": q})
    b = question_name_of({"name": "No",  "kind": "radio", "question_label": q})
    assert a == b == q


def test_a_control_with_no_declared_question_keeps_its_own_name():
    """THE CONTROL. Most fields are plain inputs and must be untouched."""
    assert question_name_of({"name": "Email", "kind": "text"}) == "Email"
    assert question_name_of({"name": "Email", "kind": "text",
                             "question_label": ""}) == "Email"


def test_a_button_is_never_a_question():
    """A submit is not something a client supplies an answer for."""
    assert question_name_of({
        "name": "Submit Declaration", "kind": "button",
        "question_label": "Health Declaration"}) == "Submit Declaration"


def test_an_unnamed_control_stays_unnamed():
    assert question_name_of({"name": "", "kind": "text"}) == ""


def test_whitespace_in_a_declared_question_is_trimmed():
    assert question_name_of({"name": "Yes", "kind": "radio",
                             "question_label": "  Do you smoke?  "}) == "Do you smoke?"
