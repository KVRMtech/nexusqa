"""E3 Catalog — unit tests for control inventory extraction, merging,
provenance, outcome surfacing, and summary statistics.

Tests cover: extract_controls (from form_snapshot_signals + field_ledger),
extract_outcomes (from displayed_values), merge_controls/merge_outcomes
(upsert-by-name semantics), provenance logic (observed/confirmed/
client_declared), build_states_index, build_ledger_by_url, catalog_summary.
"""
from __future__ import annotations

from app.services.catalog import (
    PROVENANCE_CLIENT_DECLARED,
    PROVENANCE_CONFIRMED,
    PROVENANCE_OBSERVED,
    apply_outcome_provenance,
    apply_provenance,
    build_ledger_by_url,
    build_states_index,
    catalog_summary,
    effective_provenance,
    extract_controls,
    extract_outcomes,
    merge_controls,
    merge_outcomes,
)


# ── extract_controls ────────────────────────────────────────────────────────

def test_extract_controls_from_signals():
    page_state = {
        "location": "https://app.example.com/apply",
        "form_snapshot_signals": {
            "Product Type": {"type": "select", "options": ["Term", "Whole"], "required": True},
            "Age": {"type": "text", "required": True},
        },
    }
    controls = extract_controls(page_state)
    assert len(controls) == 2
    by_name = {c["name"]: c for c in controls}
    assert by_name["Product Type"]["type"] == "select"
    assert by_name["Product Type"]["options"] == ["Term", "Whole"]
    assert by_name["Product Type"]["required"] is True
    assert by_name["Age"]["type"] == "text"
    assert by_name["Age"]["required"] is True


def test_extract_controls_merges_with_ledger():
    page_state = {
        "location": "https://app.example.com/apply",
        "form_snapshot_signals": {
            "Product Type": {"type": "select", "options": ["Term", "Whole"], "required": True},
        },
    }
    ledger = {
        "https://app.example.com/apply": [
            {"name": "Product Type", "signature": "sig_abc", "semantic_type": "product_selection"},
        ],
    }
    controls = extract_controls(page_state, ledger)
    assert len(controls) == 1
    ctrl = controls[0]
    assert ctrl["signature"] == "sig_abc"
    assert ctrl["semantic_type"] == "product_selection"
    assert ctrl["type"] == "select"


def test_extract_controls_ledger_only_entries():
    page_state = {
        "location": "https://app.example.com/apply",
        "form_snapshot_signals": {},
    }
    ledger = {
        "https://app.example.com/apply": [
            {"name": "Coverage Amount", "signature": "sig_cov",
             "semantic_type": "monetary", "options": ["100k", "250k", "500k"]},
        ],
    }
    controls = extract_controls(page_state, ledger)
    assert len(controls) == 1
    ctrl = controls[0]
    assert ctrl["name"] == "Coverage Amount"
    assert ctrl["options"] == ["100k", "250k", "500k"]
    assert ctrl["semantic_type"] == "monetary"


def test_extract_controls_depends_on():
    page_state = {
        "location": "https://app.example.com/apply",
        "form_snapshot_signals": {
            "Sub Product": {"type": "select", "options": ["A", "B"],
                            "required": False, "depends_on": "Product Type"},
        },
    }
    controls = extract_controls(page_state)
    assert controls[0]["depends_on"] == "Product Type"


def test_extract_controls_deduplicates_by_normalized_name():
    page_state = {
        "location": "https://app.example.com/apply",
        "form_snapshot_signals": {
            "First Name": {"type": "text", "required": True},
            "first name": {"type": "text", "required": True},
        },
    }
    controls = extract_controls(page_state)
    assert len(controls) == 1


def test_extract_controls_skips_empty_names():
    page_state = {
        "location": "https://app.example.com/apply",
        "form_snapshot_signals": {
            "": {"type": "text", "required": False},
            "Valid": {"type": "text", "required": False},
        },
    }
    controls = extract_controls(page_state)
    assert len(controls) == 1
    assert controls[0]["name"] == "Valid"


def test_extract_controls_tolerant_on_none():
    assert extract_controls(None) == []
    assert extract_controls({}) == []
    assert extract_controls({"form_snapshot_signals": None}) == []


def test_extract_controls_ledger_options_fill_empty_signal_options():
    page_state = {
        "location": "https://app.example.com/apply",
        "form_snapshot_signals": {
            "Tobacco": {"type": "radio", "options": [], "required": True},
        },
    }
    ledger = {
        "https://app.example.com/apply": [
            {"name": "Tobacco", "signature": "sig_tob",
             "options": ["Yes", "No"]},
        ],
    }
    controls = extract_controls(page_state, ledger)
    assert controls[0]["options"] == ["Yes", "No"]


def test_extract_controls_signal_options_win_over_ledger():
    page_state = {
        "location": "https://app.example.com/apply",
        "form_snapshot_signals": {
            "Tobacco": {"type": "radio", "options": ["Yes", "No", "Unknown"],
                        "required": True},
        },
    }
    ledger = {
        "https://app.example.com/apply": [
            {"name": "Tobacco", "signature": "sig_tob",
             "options": ["Yes", "No"]},
        ],
    }
    controls = extract_controls(page_state, ledger)
    assert controls[0]["options"] == ["Yes", "No", "Unknown"]


# ── extract_outcomes ────────────────────────────────────────────────────────

def test_extract_outcomes_basic():
    page_state = {
        "displayed_values": [
            {"label": "Monthly Premium", "selector": ".premium",
             "text": "$28.40", "value_type": "currency"},
            {"label": "Annual Premium", "selector": ".annual",
             "text": "$340.80", "value_type": "currency"},
        ],
    }
    outcomes = extract_outcomes(page_state)
    assert len(outcomes) == 2
    assert outcomes[0]["label"] == "Monthly Premium"
    assert outcomes[0]["selector"] == ".premium"
    assert outcomes[0]["value_type"] == "currency"


def test_extract_outcomes_deduplicates_by_label():
    page_state = {
        "displayed_values": [
            {"label": "Premium", "selector": ".p1", "value_type": "currency"},
            {"label": "premium", "selector": ".p2", "value_type": "currency"},
        ],
    }
    outcomes = extract_outcomes(page_state)
    assert len(outcomes) == 1


def test_extract_outcomes_skips_empty_labels():
    page_state = {
        "displayed_values": [
            {"label": "", "selector": ".x", "value_type": "currency"},
            {"label": "Premium", "selector": ".p", "value_type": "currency"},
        ],
    }
    outcomes = extract_outcomes(page_state)
    assert len(outcomes) == 1


def test_extract_outcomes_tolerant():
    assert extract_outcomes(None) == []
    assert extract_outcomes({}) == []
    assert extract_outcomes({"displayed_values": "bad"}) == []
    assert extract_outcomes({"displayed_values": [None, "bad"]}) == []


# ── merge_controls ──────────────────────────────────────────────────────────

def test_merge_controls_new_entries_appended():
    existing = [{"name": "Age", "type": "text", "options": [], "required": True}]
    incoming = [{"name": "Tobacco", "type": "radio", "options": ["Yes", "No"],
                 "required": False}]
    merged = merge_controls(existing, incoming)
    assert len(merged) == 2
    by_name = {c["name"]: c for c in merged}
    assert "Age" in by_name
    assert "Tobacco" in by_name


def test_merge_controls_existing_updated():
    existing = [{"name": "Age", "type": "text", "options": [], "required": False}]
    incoming = [{"name": "Age", "type": "text", "options": [], "required": True,
                 "semantic_type": "age"}]
    merged = merge_controls(existing, incoming)
    assert len(merged) == 1
    assert merged[0]["required"] is True
    assert merged[0]["semantic_type"] == "age"


def test_merge_controls_case_insensitive():
    existing = [{"name": "First Name", "type": "text", "options": [], "required": True}]
    incoming = [{"name": "first name", "type": "text", "options": [],
                 "required": True, "signature": "sig_fn"}]
    merged = merge_controls(existing, incoming)
    assert len(merged) == 1
    assert merged[0]["signature"] == "sig_fn"


def test_merge_controls_none_existing():
    incoming = [{"name": "Age", "type": "text", "options": [], "required": True}]
    merged = merge_controls(None, incoming)
    assert len(merged) == 1


def test_merge_controls_capped_at_200():
    incoming = [{"name": f"field_{i}", "type": "text", "options": [],
                 "required": False} for i in range(250)]
    merged = merge_controls(None, incoming)
    assert len(merged) == 200


# ── merge_outcomes ──────────────────────────────────────────────────────────

def test_merge_outcomes_new_appended():
    existing = [{"label": "Premium", "value_type": "currency"}]
    incoming = [{"label": "Coverage", "value_type": "currency"}]
    merged = merge_outcomes(existing, incoming)
    assert len(merged) == 2


def test_merge_outcomes_existing_updated():
    existing = [{"label": "Premium", "value_type": "currency"}]
    incoming = [{"label": "Premium", "value_type": "currency",
                 "selector": ".prem-new"}]
    merged = merge_outcomes(existing, incoming)
    assert len(merged) == 1
    assert merged[0]["selector"] == ".prem-new"


def test_merge_outcomes_capped_at_100():
    incoming = [{"label": f"val_{i}", "value_type": "currency"} for i in range(150)]
    merged = merge_outcomes(None, incoming)
    assert len(merged) == 100


# ── build_states_index ──────────────────────────────────────────────────────

def test_build_states_index_basic():
    coverage = {
        "states": [
            {"ax_fingerprint": "fp_a", "location": "https://a.com"},
            {"ax_fingerprint": "fp_b", "location": "https://b.com"},
        ],
    }
    index = build_states_index(coverage)
    assert len(index) == 2
    assert index["fp_a"]["location"] == "https://a.com"
    assert index["fp_b"]["location"] == "https://b.com"


def test_build_states_index_skips_empty_fingerprint():
    coverage = {
        "states": [
            {"ax_fingerprint": "", "location": "https://a.com"},
            {"ax_fingerprint": "fp_b", "location": "https://b.com"},
        ],
    }
    index = build_states_index(coverage)
    assert len(index) == 1


def test_build_states_index_tolerant():
    assert build_states_index(None) == {}
    assert build_states_index({}) == {}
    assert build_states_index({"states": "bad"}) == {}
    assert build_states_index({"states": [None, "bad"]}) == {}


# ── build_ledger_by_url ────────────────────────────────────────────────────

def test_build_ledger_by_url_groups():
    coverage = {
        "field_ledger": [
            {"name": "Age", "url": "https://a.com/step1"},
            {"name": "Name", "url": "https://a.com/step1"},
            {"name": "Premium", "url": "https://a.com/step2"},
        ],
    }
    by_url = build_ledger_by_url(coverage)
    assert len(by_url) == 2
    assert len(by_url["https://a.com/step1"]) == 2
    assert len(by_url["https://a.com/step2"]) == 1


def test_build_ledger_by_url_tolerant():
    assert build_ledger_by_url(None) == {}
    assert build_ledger_by_url({}) == {}
    assert build_ledger_by_url({"field_ledger": "bad"}) == {}


# ── effective_provenance ────────────────────────────────────────────────────

def test_provenance_observed_for_captured():
    assert effective_provenance("captured") == PROVENANCE_OBSERVED


def test_provenance_confirmed_for_approved():
    assert effective_provenance("approved") == PROVENANCE_CONFIRMED


def test_provenance_confirmed_for_validated():
    assert effective_provenance("validated") == PROVENANCE_CONFIRMED


def test_provenance_observed_for_drifted():
    assert effective_provenance("drifted") == PROVENANCE_OBSERVED


# ── apply_provenance ────────────────────────────────────────────────────────

def test_apply_provenance_all_observed():
    controls = [
        {"name": "Age", "type": "text"},
        {"name": "Tobacco", "type": "radio"},
    ]
    result = apply_provenance(controls, "captured")
    assert all(c["provenance"] == PROVENANCE_OBSERVED for c in result)


def test_apply_provenance_all_confirmed():
    controls = [
        {"name": "Age", "type": "text"},
        {"name": "Tobacco", "type": "radio"},
    ]
    result = apply_provenance(controls, "approved")
    assert all(c["provenance"] == PROVENANCE_CONFIRMED for c in result)


def test_apply_provenance_client_declared_override():
    controls = [
        {"name": "Monthly Premium", "type": "text"},
        {"name": "Age", "type": "text"},
    ]
    rule_fields = {"monthly_premium"}
    result = apply_provenance(controls, "captured", rule_fields)
    assert result[0]["provenance"] == PROVENANCE_CLIENT_DECLARED
    assert result[1]["provenance"] == PROVENANCE_OBSERVED


def test_apply_provenance_client_declared_wins_over_confirmed():
    controls = [{"name": "Monthly Premium", "type": "text"}]
    rule_fields = {"monthly_premium"}
    result = apply_provenance(controls, "approved", rule_fields)
    assert result[0]["provenance"] == PROVENANCE_CLIENT_DECLARED


def test_apply_provenance_case_insensitive_rule_match():
    controls = [{"name": "Monthly Premium", "type": "text"}]
    rule_fields = {"Monthly Premium"}
    result = apply_provenance(controls, "captured", rule_fields)
    assert result[0]["provenance"] == PROVENANCE_CLIENT_DECLARED


# ── apply_outcome_provenance ────────────────────────────────────────────────

def test_outcome_provenance_observed():
    outcomes = [{"label": "Premium"}]
    result = apply_outcome_provenance(outcomes, "captured")
    assert result[0]["provenance"] == PROVENANCE_OBSERVED


def test_outcome_provenance_confirmed():
    outcomes = [{"label": "Premium"}]
    result = apply_outcome_provenance(outcomes, "validated")
    assert result[0]["provenance"] == PROVENANCE_CONFIRMED


def test_outcome_provenance_client_declared():
    outcomes = [{"label": "Monthly Premium"}]
    result = apply_outcome_provenance(outcomes, "captured", {"monthly_premium"})
    assert result[0]["provenance"] == PROVENANCE_CLIENT_DECLARED


# ── catalog_summary ─────────────────────────────────────────────────────────

def test_catalog_summary_counts():
    nodes = [
        {
            "controls": [
                {"name": "Age", "type": "text", "required": True, "options": []},
                {"name": "Product", "type": "select", "required": True,
                 "options": ["Term", "Whole"]},
                {"name": "Tobacco", "type": "radio", "required": False,
                 "options": ["Yes", "No"]},
            ],
            "displayed_outcomes": [
                {"label": "Premium", "value_type": "currency"},
            ],
        },
        {
            "controls": [
                {"name": "Name", "type": "text", "required": True, "options": []},
            ],
            "displayed_outcomes": [],
        },
    ]
    summary = catalog_summary(nodes)
    assert summary["node_count"] == 2
    assert summary["total_controls"] == 4
    assert summary["controls_with_options"] == 2
    assert summary["required_controls"] == 3
    assert summary["total_outcomes"] == 1


def test_catalog_summary_empty():
    summary = catalog_summary([])
    assert summary["node_count"] == 0
    assert summary["total_controls"] == 0
    assert summary["total_outcomes"] == 0


def test_catalog_summary_tolerant_on_bad_nodes():
    summary = catalog_summary([None, "bad", {}])
    assert summary["node_count"] == 3
    assert summary["total_controls"] == 0
