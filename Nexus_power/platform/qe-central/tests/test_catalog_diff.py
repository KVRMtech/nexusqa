"""P6 — catalog regression diff. Pure; exercises the real P2→P6 chain
(build_master_catalog → snapshot_catalog → diff_catalogs)."""
from __future__ import annotations

from app.services import catalog
from app.services.catalog_diff import diff_catalogs


def _snap(nodes, edges=None):
    return catalog.snapshot_catalog(catalog.build_master_catalog(nodes, edges=edges))


def test_identical_catalogs_have_no_changes():
    nodes = [{"node_fp": "n1", "title": "P1", "controls": [
        {"name": "Email", "question_id": "q1", "required": True, "options": []}]}]
    d = diff_catalogs(_snap(nodes), _snap(nodes))
    assert d["summary"]["has_changes"] is False
    assert d["added"] == [] and d["removed"] == [] and d["changed"] == []


def test_added_and_removed_questions_are_named():
    old = [{"node_fp": "n1", "controls": [{"name": "Email", "question_id": "q1"}]}]
    new = [{"node_fp": "n1", "controls": [
        {"name": "Email", "question_id": "q1"},
        {"name": "Phone", "question_id": "q2"}]}]
    assert diff_catalogs(_snap(old), _snap(new))["added"] == ["q2"]
    assert diff_catalogs(_snap(new), _snap(old))["removed"] == ["q2"]


def test_a_changed_option_set_is_flagged():
    old = [{"node_fp": "n1", "controls": [
        {"name": "State", "question_id": "q1", "options": ["CA"]}]}]
    new = [{"node_fp": "n1", "controls": [
        {"name": "State", "question_id": "q1", "options": ["CA", "NY"]}]}]
    d = diff_catalogs(_snap(old), _snap(new))
    assert len(d["changed"]) == 1
    ch = d["changed"][0]
    assert ch["question_id"] == "q1" and "options_changed" in ch["kinds"]
    assert ch["changes"]["options"]["to"] == ["CA", "NY"]


def test_a_moved_next_page_is_flagged_as_a_moved_branch():
    nodes = [{"node_fp": "n1", "controls": [{"name": "Q", "question_id": "q1"}]},
             {"node_fp": "n2", "controls": []}]
    d = diff_catalogs(
        _snap(nodes, [{"from_fp": "n1", "to_fp": "n2"}]),
        _snap(nodes, [{"from_fp": "n1", "to_fp": "n3"}]))
    assert len(d["changed"]) == 1
    assert "moved_next_page" in d["changed"][0]["kinds"]
    assert d["changed"][0]["changes"]["expected_next_page"] == {"from": "n2", "to": "n3"}


def test_a_tightened_required_flag_is_flagged():
    old = [{"node_fp": "n1", "controls": [
        {"name": "SSN", "question_id": "q1", "required": False}]}]
    new = [{"node_fp": "n1", "controls": [
        {"name": "SSN", "question_id": "q1", "required": True}]}]
    d = diff_catalogs(_snap(old), _snap(new))
    assert d["changed"][0]["kinds"] == ["required_changed"]
    assert d["changed"][0]["changes"]["required"] == {"from": False, "to": True}
