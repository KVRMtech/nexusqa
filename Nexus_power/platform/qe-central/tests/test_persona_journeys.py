"""P3 — persona journey generation (project_from_catalog). Pure; exercises the
full catalog+branches → rules → projected journey chain."""
from __future__ import annotations

from app.services import catalog
from app.services.journey_projector import rules_from_branches
from app.services.persona_journeys import project_from_catalog


def _fixture():
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
    return master, rules


def test_smoker_persona_activates_the_conditional_question_by_name():
    master, rules = _fixture()
    smoker = project_from_catalog(master, rules, {"tobacco use": "yes"})
    assert "Cigarettes Per Day" in [q["name"] for q in smoker["activated"]]
    assert smoker["counts"]["activated"] == 1
    assert smoker["answered"] == 1


def test_healthy_persona_skips_the_conditional_question():
    master, rules = _fixture()
    healthy = project_from_catalog(master, rules, {"tobacco use": "no"})
    assert "Cigarettes Per Day" in [q["name"] for q in healthy["skipped"]]
    assert healthy["counts"]["activated"] == 0


def test_unknown_answer_key_is_dropped_not_guessed():
    master, rules = _fixture()
    none = project_from_catalog(master, rules, {"nonexistent question": "x"})
    assert none["answered"] == 0


def test_answers_keyed_by_question_id_also_work():
    master, rules = _fixture()
    from app.services.catalog import question_id_for
    trig = question_id_for({"signature": "q:tobacco", "name": "tobacco use"})
    smoker = project_from_catalog(master, rules, {trig: "yes"})
    assert smoker["counts"]["activated"] == 1
