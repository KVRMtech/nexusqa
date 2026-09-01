"""U0 — accessible-name confidence (the escalate-to-vision signal). Pure."""
from __future__ import annotations

from app.inventory import (
    build_inventory,
    name_confidence_for,
    name_confidence_summary,
)


def test_name_confidence_grading_by_rung():
    assert name_confidence_for("label-for", "Email") == "high"
    assert name_confidence_for("aria-label", "Email") == "high"
    assert name_confidence_for("aria-labelledby", "Email") == "high"
    assert name_confidence_for("wrapping-label", "Email") == "medium"
    assert name_confidence_for("content", "Submit") == "medium"
    assert name_confidence_for("title", "x") == "low"
    assert name_confidence_for("placeholder", "Search") == "low"
    assert name_confidence_for("none", "") == "none"


def test_no_name_is_none_regardless_of_rung():
    assert name_confidence_for("label-for", "") == "none"
    assert name_confidence_for("aria-label", "   ") == "none"


def test_unknown_rung_is_none():
    assert name_confidence_for("some-future-rung", "x") == "none"


def test_build_inventory_stamps_confidence_in_qec():
    raw = [{"name": "Email", "name_source": "label-for", "role": "textbox",
            "tag": "input", "input_type": "email"}]
    inv = build_inventory(raw)
    assert inv[0]["qec"]["name_confidence"] == "high"


def test_build_inventory_unlabelled_control_is_none():
    raw = [{"name": "", "name_source": "none", "role": "button", "tag": "canvas"}]
    inv = build_inventory(raw)
    assert inv[0]["qec"]["name_confidence"] == "none"


def test_name_confidence_summary_counts_tiers_and_tolerates_junk():
    controls = [
        {"qec": {"name_confidence": "high"}},
        {"qec": {"name_confidence": "low"}},
        {"qec": {"name_confidence": "none"}},
        {"qec": {"name_confidence": "high"}},
        "not-a-control",
        {"qec": {}},                       # missing → none
    ]
    assert name_confidence_summary(controls) == {
        "high": 2, "medium": 0, "low": 1, "none": 2}
