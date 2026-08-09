"""P5 — the fallback ladder: honest rung selection + coverage roll-up. Pure."""
from __future__ import annotations

from app.services import fallback_ladder as FL
from app.services.coverage import (
    PROV_DETERMINISTIC, PROV_INFERRED, PROV_LIVE_CONFIRMED,
)


def test_deterministic_wins_and_is_grounded():
    d = FL.resolve_rung(deterministic={"resolved": True})
    assert d["rung"] == FL.RUNG_DETERMINISTIC
    assert d["resolved"] and d["verified"]
    assert d["provenance"] == PROV_DETERMINISTIC
    assert d["needs_human"] is False


def test_verified_agentic_counts_as_live_confirmed():
    d = FL.resolve_rung(
        deterministic={"resolved": False},
        agentic={"resolved": True, "verified": True})
    assert d["rung"] == FL.RUNG_AGENTIC
    assert d["provenance"] == PROV_LIVE_CONFIRMED
    assert d["needs_human"] is False


def test_unverified_agentic_is_not_coverage_and_descends():
    # The anti-green-wash rule: an agent action that could not be verified does
    # NOT count — it descends to record-once (or human).
    d = FL.resolve_rung(
        agentic={"resolved": True, "verified": False},
        record_once_available=True)
    assert d["rung"] == FL.RUNG_RECORD_ONCE
    assert d["needs_human"] is True
    assert d["provenance"] == PROV_INFERRED
    assert "could not be verified" in d["reason"]


def test_nothing_resolves_falls_to_flagged_human():
    d = FL.resolve_rung()
    assert d["rung"] == FL.RUNG_HUMAN
    assert d["resolved"] is False and d["needs_human"] is True


def test_record_once_is_resolved_but_needs_a_one_time_human():
    d = FL.resolve_rung(record_once_available=True)
    assert d["rung"] == FL.RUNG_RECORD_ONCE
    assert d["resolved"] is True          # replays after the one recording
    assert d["needs_human"] is True
    assert d["touch_type"] == "widget_record"


def test_coverage_by_rung_is_honest_no_average():
    decisions = [
        FL.resolve_rung(deterministic={"resolved": True}),
        FL.resolve_rung(agentic={"resolved": True, "verified": True}),
        FL.resolve_rung(record_once_available=True),
        FL.resolve_rung(),                 # human, unresolved
    ]
    rollup = FL.coverage_by_rung(decisions)
    assert rollup["total"] == 4
    assert rollup["by_rung"] == {
        "deterministic": 1, "agentic": 1, "record_once": 1, "human": 1}
    assert rollup["resolved"] == 3         # the human one is unresolved
    assert rollup["needs_human"] == 2      # record_once + human
    assert rollup["unresolved"] == 1


# ── U5: wire capture-mode → ladder rung, per-control ledger ──────────────────────

def test_rung_for_capture_maps_capture_modes():
    assert FL.rung_for_capture("dom", None)["rung"] == FL.RUNG_DETERMINISTIC
    assert FL.rung_for_capture("vision", True)["rung"] == FL.RUNG_AGENTIC
    assert FL.rung_for_capture("vision", False)["rung"] == FL.RUNG_HUMAN   # unverified descends
    assert FL.rung_for_capture("record_once", None)["rung"] == FL.RUNG_RECORD_ONCE
    assert FL.rung_for_capture("some-canvas-thing", None)["rung"] == FL.RUNG_HUMAN


def test_coverage_for_controls_builds_an_honest_ledger():
    controls = [
        {"qec": {"capture_mode": "dom"}},
        {"qec": {"capture_mode": "vision"}, "verified": True},
        {"qec": {"capture_mode": "vision"}, "verified": False},   # descends to human
        {"capture_mode": "record_once"},                          # top-level capture_mode
        {"qec": {"capture_mode": "canvas-thing"}},                # unknown → human
        "junk",                                                   # skipped
    ]
    cov = FL.coverage_for_controls(controls)
    assert cov["total"] == 5
    assert cov["by_rung"] == {"deterministic": 1, "agentic": 1,
                              "record_once": 1, "human": 2}
    assert cov["resolved"] == 3
    assert cov["needs_human"] == 3
    assert cov["unresolved"] == 2
