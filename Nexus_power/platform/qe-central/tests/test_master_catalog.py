"""P2 — Master Catalog: stable question ids, validation shape, app-scoped
aggregation. Pure-logic (plain dicts), runs everywhere."""
from __future__ import annotations

from app.services import catalog


def test_question_id_is_stable_from_signature_across_crawls():
    # Same signature → same id even if the artifact / crawl differs (Δ2).
    a = catalog.question_id_for({"name": "Tobacco", "signature": "sig-xyz"})
    b = catalog.question_id_for({"name": "TOBACCO USE", "signature": "sig-xyz"})
    assert a == b and a.startswith("q_")
    # Different signature → different id.
    assert catalog.question_id_for({"signature": "other"}) != a


def test_question_id_falls_back_to_normalized_name_without_signature():
    a = catalog.question_id_for({"name": "Coverage Amount"})
    b = catalog.question_id_for({"name": "coverage   amount"})
    assert a == b


def test_extract_controls_stamps_question_id_and_validation():
    page = {
        "location": "https://a.example/apply",
        "form_snapshot_signals": {
            "Email": {"type": "email", "required": True, "pattern": ".+@.+"},
            "State": {"type": "select", "options": ["CA", "NY"]},
        },
    }
    controls = {c["name"]: c for c in catalog.extract_controls(page)}
    assert controls["Email"]["question_id"].startswith("q_")
    assert controls["Email"]["validation"] == {"pattern": ".+@.+"}
    assert controls["State"]["options"] == ["CA", "NY"]
    assert "validation" not in controls["State"]      # no constraints → no key


def test_master_catalog_dedups_by_question_id_across_journeys():
    nodes = [
        {"node_fp": "n1", "title": "Page 1", "controls": [
            {"name": "Email", "type": "email", "required": True,
             "question_id": "q_x", "options": []}]},
        {"node_fp": "n2", "title": "Page 2", "controls": [
            {"name": "Email", "type": "email", "required": False,
             "question_id": "q_x", "options": []},
            {"name": "State", "type": "select", "options": ["CA"],
             "question_id": "q_y"}]},
    ]
    cat = catalog.build_master_catalog(
        nodes, edges=[{"from_fp": "n1", "to_fp": "n2"}])
    by = {q["question_id"]: q for q in cat["questions"]}
    assert set(by) == {"q_x", "q_y"}                       # Email appears ONCE
    assert by["q_x"]["required"] is True                   # sticky-True across nodes
    assert by["q_x"]["pages"] == ["Page 1", "Page 2"]      # seen on both
    assert by["q_x"]["expected_next_page"] == "n2"         # from the n1→n2 edge
    assert cat["summary"]["question_count"] == 2


def test_master_catalog_reads_controls_inventory_key_and_tolerates_junk():
    nodes = [
        {"fingerprint": "n1", "url": "u1",
         "controls_inventory": [{"name": "Q", "question_id": "q_1"}]},
        "not-a-node",
        {"node_fp": "n2", "controls": "not-a-list"},
    ]
    cat = catalog.build_master_catalog(nodes)
    assert [q["question_id"] for q in cat["questions"]] == ["q_1"]


def test_snapshot_hash_is_stable_and_changes_on_change():
    m1 = catalog.build_master_catalog([
        {"node_fp": "n1", "title": "P1", "controls": [
            {"name": "Email", "question_id": "q_x", "required": True, "options": []}]}])
    s1 = catalog.snapshot_catalog(m1, artifact_id="art-1")
    s1b = catalog.snapshot_catalog(m1, artifact_id="art-2")
    assert s1["snapshot_hash"] == s1b["snapshot_hash"]     # artifact id doesn't move the hash
    m2 = catalog.build_master_catalog([
        {"node_fp": "n1", "title": "P1", "controls": [
            {"name": "Email", "question_id": "q_x", "required": True, "options": ["x"]}]}])
    s2 = catalog.snapshot_catalog(m2)
    assert s2["snapshot_hash"] != s1["snapshot_hash"]      # an added option changes the hash
    assert s2["question_count"] == 1
