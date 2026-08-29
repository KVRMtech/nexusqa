"""BRANCH COVERAGE: every question asked with every answer, reveals recorded.

MEASURED (underwriting fixture, 2026-08-29). A page of 60 questions where eight
answers reveal 44 more, four levels deep -- 104 questions reachable in total.
The crawl captured 60 and reported `branch_coverage: false`, which every bundle
this product has written has always said:

    "One path per journey. At each decision point a single option was taken, so
     business paths behind the other options were not visited."

These tests pin the rules that turn 60/104 into 104/104, and -- just as
important -- pin the boundary of the claim, so a SWEEP can never be read as an
exhaustive combinatorial walk.
"""
from __future__ import annotations

from app.branch_walk import (BranchLedger, answerable_questions, budget_exhausted,
                             newly_revealed, question_key, should_descend)


def _radio(name, qid, label, opts=("Yes", "No")):
    return {"name": name, "kind": "radio", "group_id": qid,
            "question_label": label, "group_options": list(opts)}


# ── which questions are branch points ──────────────────────────────────────

def test_a_yes_no_question_is_a_branch_point():
    qs = answerable_questions([_radio("Yes", "q1", "Do you smoke?"),
                               _radio("No", "q1", "Do you smoke?")])
    assert len(qs) == 1
    assert qs[0]["options"] == ["Yes", "No"]
    assert qs[0]["label"] == "Do you smoke?"


def test_a_select_with_four_answers_is_one_branch_point_with_four():
    qs = answerable_questions([{
        "name": "Frequency", "kind": "select", "group_id": "q9",
        "question_label": "How often?",
        "options": ["Never", "Occasionally", "Frequently", "Daily"]}])
    assert len(qs) == 1 and len(qs[0]["options"]) == 4


def test_a_question_with_one_answer_is_not_a_branch_point():
    """THE CONTROL. Sweeping it spends budget to learn nothing."""
    assert answerable_questions([{"name": "Only", "kind": "radio",
                                  "group_id": "q2", "group_options": ["Only"]}]) == []


def test_a_text_field_is_not_a_branch_point():
    assert answerable_questions([{"name": "Email", "kind": "text"}]) == []


def test_two_questions_are_two_rows_even_with_identical_wording():
    """Two 'Details?' questions must not merge, or one is covered by the other."""
    qs = answerable_questions([_radio("Yes", "qA", "Details?"),
                               _radio("Yes", "qB", "Details?")])
    assert len(qs) == 2


# ── what an answer revealed ────────────────────────────────────────────────

def test_controls_that_appear_after_an_answer_are_the_reveal():
    before = [_radio("Yes", "q1", "Heart disease?")]
    after = before + [_radio("Yes", "c1", "Which condition?"),
                      _radio("Yes", "c2", "Which year?")]
    rev = newly_revealed(before, after)
    assert [r["question_label"] for r in rev] == ["Which condition?", "Which year?"]


def test_a_rerender_of_the_same_controls_is_not_a_reveal():
    """THE CONTROL. A page that rebuilds itself must not read as revealing."""
    before = [_radio("Yes", "q1", "Heart disease?")]
    after = [dict(_radio("Yes", "q1", "Heart disease?"))]
    assert newly_revealed(before, after) == []


def test_the_reveal_keeps_the_pages_own_order():
    before = []
    after = [_radio("Yes", "b", "second"), _radio("Yes", "a", "first")]
    assert [r["question_label"] for r in newly_revealed(before, after)] == ["second", "first"]


# ── the ledger, and the boundary of the claim ──────────────────────────────

def test_the_ledger_records_each_answer_and_its_reveals():
    led = BranchLedger()
    led.record(question="q1", label="Heart disease?", option="Yes", depth=0,
               revealed=[_radio("Yes", "c1", "Which condition?")])
    led.record(question="q1", label="Heart disease?", option="No", depth=0, revealed=[])
    s = led.summary()
    assert s["questions_swept"] == 1
    assert s["answers_taken"] == 2
    assert s["questions_revealed"] == 1


def test_the_summary_states_it_is_a_sweep_not_a_combinatorial_walk():
    """THE HONESTY MARKER. A sweep must never be readable as exhaustive."""
    s = BranchLedger().summary()
    assert s["mode"] == "per_question_sweep"
    assert s["combinatorial"] is False
    assert "two specific answers AT ONCE" in s["note"]


def test_a_skipped_answer_is_named_with_its_reason():
    led = BranchLedger()
    led.skip(question="q3", option="Daily", reason="budget_exhausted")
    assert led.summary()["skipped"] == 1
    assert led.skipped[0]["reason"] == "budget_exhausted"


# ── the bounds ─────────────────────────────────────────────────────────────

def test_the_sweep_stops_at_its_visit_budget():
    led = BranchLedger()
    for i in range(5):
        led.record(question=f"q{i}", label="", option="Yes", depth=0, revealed=[])
    assert budget_exhausted(led, max_visits=5) is True
    assert budget_exhausted(led, max_visits=6) is False


def test_the_sweep_stops_descending_at_its_depth_limit():
    assert should_descend(0, max_depth=4) is True
    assert should_descend(3, max_depth=4) is True
    assert should_descend(4, max_depth=4) is False


def test_question_identity_prefers_the_declared_container():
    assert question_key({"question_group_id": "A", "group_id": "B"}) == "A"
    assert question_key({"group_id": "B"}) == "B"
    assert question_key({"question_label": "Do you smoke?"}) == "Do you smoke?"
