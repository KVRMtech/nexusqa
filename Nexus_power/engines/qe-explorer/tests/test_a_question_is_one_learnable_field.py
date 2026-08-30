"""A QUESTION IS ONE LEARNABLE FIELD; ITS ANSWERS ARE NOT SEPARATE FIELDS.

MEASURED (underwriting fixture, 2026-08-29). A page with 63 questions, 9 of them
successfully answered, produced a field ledger with THREE entries:

    Q1. Has a doctor advised you about a respiratory illness?   filled=False
    Q1. Has a doctor advised you about a respiratory illness?   filled=False
    Q4. Have you ever been treated for kidney disease?          filled=True

Sixty-three questions, three rows -- and Q1 twice.

WHY. The ledger dedups on `field_signature.compute`, whose first input is
`control["name"]`. For a radio that is the OPTION ("Yes"), not the question, so
all 126 radio controls on the page produced TWO signatures and the selects a
third. Everything else was discarded as a duplicate of something it had nothing
to do with.

This is the fifth path out of the inventory to carry the answer where the
question belonged -- after the form snapshot, the signal's options, the seed
request and the fill ledger's display name. Each was fixed where it was found;
this one sits underneath them, which is why the ledger stayed nearly empty even
after the rows above it were named correctly.

WHAT A SIGNATURE IS FOR. Its docstring: "the same field must resolve to the same
signature wherever it appears, or a value learned on one page would have to be
re-learned on the next." A question IS that field. "Do you smoke?" is one thing
to learn an answer for; "Yes" is not a field at all.
"""
from __future__ import annotations

from app import field_signature


def _radio(option, question):
    return {"name": option, "kind": "radio", "question_label": question,
            "group_options": ["Yes", "No"], "input_type": "radio"}


def _sig(control):
    return field_signature.compute(control, kind=control.get("kind", ""))["signature"]


# ── the measured regression ────────────────────────────────────────────────

def test_two_different_questions_are_two_different_fields():
    """THE BUG: both collapsed onto the signature for the word "Yes"."""
    a = _sig(_radio("Yes", "Do you smoke?"))
    b = _sig(_radio("Yes", "Do you have diabetes?"))
    assert a != b


def test_both_answers_of_one_question_are_the_SAME_field():
    """One question is one row, whichever answer is showing."""
    q = "Do you smoke?"
    assert _sig(_radio("Yes", q)) == _sig(_radio("No", q))


def test_sixty_questions_are_sixty_fields():
    sigs = {_sig(_radio(opt, f"Q{i}. Something?"))
            for i in range(60) for opt in ("Yes", "No")}
    assert len(sigs) == 60


# ── controls: what must NOT change ─────────────────────────────────────────

def test_a_plain_text_field_is_unchanged_by_this():
    """THE CONTROL. Most fields declare no question and must keep their identity."""
    before = _sig({"name": "Email", "kind": "text", "input_type": "email"})
    after = _sig({"name": "Email", "kind": "text", "input_type": "email",
                  "question_label": ""})
    assert before == after


def test_the_same_question_on_two_pages_is_still_one_field():
    """The whole point of a signature: learn the answer once, reuse it."""
    q = "Do you smoke?"
    assert _sig(_radio("Yes", q)) == _sig(_radio("Yes", q))


def test_a_signature_is_still_explainable():
    out = field_signature.compute(_radio("Yes", "Do you smoke?"), kind="radio")
    assert out["signature"]
    assert isinstance(out, dict) and len(out) > 1, "features must survive for audit"
