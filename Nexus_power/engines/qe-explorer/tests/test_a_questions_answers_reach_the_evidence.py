"""A QUESTION'S ANSWERS MUST REACH THE EVIDENCE BUNDLE.

MEASURED (health-declaration fixture, 2026-08-29), immediately after the fix that
made 25 questions appear as 25 rows rather than two:

    "1. Have you smoked tobacco in the last 12 months?"  opts=0  []
    "16. Is your BMI above 30?"                          opts=0  []

Every question named, and not one answer recorded -- while the SAME run's
inventory reported `radio_groups=25 radio_grouped=50`, so all fifty Yes/No
options were seen.

WHERE IT GOES. ``form_signal_for`` -- the only boundary qe-central reads a
question's answers from -- takes them from ``record["options"]``. The browser
fills that for a <select>, whose answers are its own children. A RADIO's answers
are its SIBLINGS, so they are recorded by the grouping pass in
``group_options`` ("every answer the question offers, in DOM order") and
``record["options"]`` stays empty. The signal then reported a question with no
answers, and ``options_total`` -- the honesty marker that exists precisely so a
clipped enumeration cannot be read as complete -- reported 0.

The client's question is "prove every option of every question was captured".
The bundle is the proof, and for every radio question in every crawl this
product has ever run, the answer list was empty.

Fall back to ``group_options`` when ``options`` is empty. A <select> is
unaffected: it has its own options and no group.
"""
from __future__ import annotations

from app.inventory import form_signal_for


def _radio(name, group_options, options_total=None):
    return {"name": name, "kind": "radio", "group_options": list(group_options),
            "group_size": len(group_options),
            "options": [], "options_total": options_total or 0}


# ── the measured regression ────────────────────────────────────────────────

def test_a_yes_no_question_reports_both_answers():
    """THE BUG. A health question offers two answers; the bundle said none."""
    sig = form_signal_for(_radio("Yes", ["Yes", "No"]))
    assert sig["options"] == ["Yes", "No"]
    assert sig["options_total"] == 2


def test_a_multi_answer_question_keeps_dom_order():
    sig = form_signal_for(_radio("Never", ["Never", "Occasionally", "Daily"]))
    assert sig["options"] == ["Never", "Occasionally", "Daily"]
    assert sig["options_total"] == 3


# ── controls: what must NOT change ─────────────────────────────────────────

def test_a_select_keeps_its_own_options():
    """THE CONTROL. A <select> carries its answers itself and has no group."""
    sig = form_signal_for({"name": "Country", "kind": "select",
                           "options": ["UK", "US"], "options_total": 2})
    assert sig["options"] == ["UK", "US"]
    assert sig["options_total"] == 2


def test_a_declared_total_larger_than_the_kept_list_survives():
    """options_total is the honesty marker for a CLIPPED enumeration."""
    sig = form_signal_for({"name": "Country", "kind": "select",
                           "options": ["UK", "US"], "options_total": 300})
    assert sig["options_total"] == 300


def test_a_plain_text_field_has_no_answers():
    sig = form_signal_for({"name": "Email", "kind": "text"})
    assert sig["options"] == []
    assert sig["options_total"] == 0


def test_a_button_is_still_not_a_field():
    assert form_signal_for({"name": "Submit", "kind": "button"}) is None
