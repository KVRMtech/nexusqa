"""P3 — the persona journey projector. Pure graph simulation, runs everywhere."""
from __future__ import annotations

from app.services.journey_projector import persona_answers_for, project_traversal

# q1 base; q2 revealed by q1=yes; q3 revealed by q2=yes; q4 base.
_QUESTIONS = [
    {"question_id": "q1"}, {"question_id": "q2"},
    {"question_id": "q3"}, {"question_id": "q4"},
]
_RULES = [
    {"question_id": "q1", "option": "yes", "reveals_question_ids": ["q2"]},
    {"question_id": "q2", "option": "yes", "reveals_question_ids": ["q3"]},
]


def test_a_no_answer_skips_the_conditional_children():
    # A "healthy" persona declines the trigger — children are never activated.
    out = project_traversal(_QUESTIONS, _RULES, {"q1": "No", "q4": "x"})
    assert out["visible"] == ["q1", "q4"]
    assert out["executed"] == ["q1", "q4"]
    assert out["activated"] == []
    assert out["skipped"] == ["q2", "q3"]


def test_a_yes_answer_activates_children_transitively():
    # A "tobacco" persona answers Yes down the chain — q2 then q3 activate.
    out = project_traversal(
        _QUESTIONS, _RULES, {"q1": "Yes", "q2": "Yes", "q4": "x"})
    assert out["visible"] == ["q1", "q2", "q3", "q4"]
    assert out["activated"] == ["q2", "q3"]          # both children reached
    assert out["executed"] == ["q1", "q2", "q4"]     # q3 visible but unanswered
    assert out["skipped"] == []


def test_activation_stops_where_the_answer_stops():
    # q1=Yes reveals q2, but q2 is unanswered → q3 is never revealed.
    out = project_traversal(_QUESTIONS, _RULES, {"q1": "Yes"})
    assert out["visible"] == ["q1", "q2", "q4"]
    assert out["activated"] == ["q2"]
    assert out["skipped"] == ["q3"]


def test_no_rules_means_every_question_is_base():
    out = project_traversal(_QUESTIONS, [], {"q1": "Yes"})
    assert out["visible"] == ["q1", "q2", "q3", "q4"]   # nothing is conditional
    assert out["activated"] == []


def test_persona_answers_map_by_name_and_semantic_type():
    questions = [
        {"question_id": "qa", "name": "Tobacco Use", "semantic_type": "tobacco"},
        {"question_id": "qb", "name": "State"},
    ]
    # by accessible name (normalized) and by semantic type; unknowns ignored.
    assert persona_answers_for(questions, {"tobacco use": "Yes", "State": "CA",
                                           "unrelated": "z"}) == {"qa": "Yes", "qb": "CA"}
    assert persona_answers_for(questions, {"tobacco": "Yes"}) == {"qa": "Yes"}


def test_projector_never_invents_an_answer():
    # A question with no persona answer is left unanswered — never guessed.
    out = project_traversal(_QUESTIONS, _RULES, {})
    assert out["executed"] == []          # nothing answered → nothing executed
    assert out["visible"] == ["q1", "q4"]  # only base questions are on the path


def test_rules_from_branches_links_trigger_to_named_children_only():
    from app.services.catalog import question_id_for
    from app.services.journey_projector import rules_from_branches
    child_qid = question_id_for({"name": "cigarettes per day"})
    questions = [
        {"question_id": question_id_for({"signature": "q:tobacco", "name": "tobacco use"}),
         "name": "tobacco use"},
        {"question_id": child_qid, "name": "Cigarettes Per Day"},
    ]
    branches = [{
        "control_signature": "q:tobacco", "control_label_norm": "tobacco use",
        "option_label_norm": "yes",
        "reveals": ["input:cigarettes per day", "button:yes"],  # button:yes has no named question
    }]
    rules = rules_from_branches(branches, questions)
    assert len(rules) == 1
    assert rules[0]["question_id"] == question_id_for(
        {"signature": "q:tobacco", "name": "tobacco use"})
    assert rules[0]["option"] == "yes"
    assert rules[0]["reveals_question_ids"] == [child_qid]   # only the named child


def test_end_to_end_branches_to_persona_journey():
    from app.services import catalog
    from app.services.catalog import question_id_for
    from app.services.journey_projector import project_traversal, rules_from_branches
    nodes = [{"node_fp": "n1", "title": "Health", "controls": [
        {"name": "Cigarettes Per Day", "signature": "sig-cig", "type": "number"}]}]
    branches = [
        {"node_fp": "n1", "control_signature": "q:tobacco",
         "control_label_norm": "tobacco use", "option_label_norm": "yes",
         "reveals": ["input:cigarettes per day"]},
        {"node_fp": "n1", "control_signature": "q:tobacco",
         "control_label_norm": "tobacco use", "option_label_norm": "no"},
    ]
    master = catalog.build_master_catalog(nodes, branches=branches)
    rules = rules_from_branches(branches, master["questions"])
    trig = question_id_for({"signature": "q:tobacco", "name": "tobacco use"})
    child = question_id_for({"signature": "sig-cig", "name": "Cigarettes Per Day"})
    smoker = project_traversal(master["questions"], rules, {trig: "yes"})
    assert child in smoker["activated"]           # tobacco=yes → cigarettes activates
    healthy = project_traversal(master["questions"], rules, {trig: "no"})
    assert child in healthy["skipped"]            # tobacco=no → cigarettes skipped
