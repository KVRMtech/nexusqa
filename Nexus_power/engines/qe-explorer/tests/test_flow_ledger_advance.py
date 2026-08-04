"""Flow ledger — the oracle_unavailable terminal and the per-step advance
evidence (who decided each advance, and the rollups the audit reads).

The law under test: ``completed`` derives from the terminal and ONLY
``submit_boundary`` / ``no_advance`` complete — an infrastructure failure
(``oracle_unavailable``) can never be reported as a covered journey.
"""
from __future__ import annotations

from app import flow_ledger


def _flow(terminal, steps=None):
    return flow_ledger.build_flow(
        entry_fingerprint="fpE", entry_url="https://a.example/q",
        entry_title="Quote", steps=steps or [], terminal=terminal,
        terminal_url="https://a.example/q3", max_steps=20)


def test_completing_terminals_membership_is_pinned():
    assert flow_ledger.COMPLETING_TERMINALS == frozenset({
        flow_ledger.TERMINAL_SUBMIT_BOUNDARY, flow_ledger.TERMINAL_NO_ADVANCE})


def test_oracle_unavailable_is_not_complete():
    f = _flow(flow_ledger.TERMINAL_ORACLE_UNAVAILABLE)
    assert f["terminal"] == "oracle_unavailable"
    assert f["completed"] is False


def test_no_advance_still_completes():
    assert _flow(flow_ledger.TERMINAL_NO_ADVANCE)["completed"] is True


def test_summarize_counts_oracle_unavailable_as_truncation():
    s = flow_ledger.summarize([
        _flow(flow_ledger.TERMINAL_SUBMIT_BOUNDARY),
        _flow(flow_ledger.TERMINAL_ORACLE_UNAVAILABLE),
        _flow(flow_ledger.TERMINAL_ORACLE_UNAVAILABLE),
    ])
    assert s["flows_completed"] == 1
    assert s["flows_truncated"] == 2
    assert s["truncation_reasons"] == {"oracle_unavailable": 2}


def test_step_advance_evidence_passes_through():
    steps = [
        {"fingerprint": "f1", "url": "u1", "title": "Step 1",
         "fields_filled": 3, "fields_unfilled": 0,
         "advance": {"tier": 1, "control_name": "Continue", "oracle": False}},
        {"fingerprint": "f2", "url": "u2", "title": "Step 2",
         "fields_filled": 2, "fields_unfilled": 1,
         "advance": {"tier": 3, "control_name": "See My Quote", "oracle": True,
                     "signature": "a" * 64}},
        {"fingerprint": "f3", "url": "u3", "title": "End",
         "fields_filled": 0, "fields_unfilled": 0},
    ]
    f = _flow(flow_ledger.TERMINAL_SUBMIT_BOUNDARY, steps=steps)
    assert f["steps"][0]["advance"] == {
        "tier": 1, "control_name": "Continue", "oracle": False}
    assert f["steps"][1]["advance"]["tier"] == 3
    assert f["steps"][1]["advance"]["oracle"] is True
    assert f["steps"][1]["advance"]["signature"] == "a" * 64
    assert "advance" not in f["steps"][2]


def test_steps_without_advance_stay_schema_compatible():
    f = _flow(flow_ledger.TERMINAL_NO_ADVANCE, steps=[
        {"fingerprint": "f1", "url": "u1", "title": "Only",
         "fields_filled": 1, "fields_unfilled": 0}])
    assert f["steps"][0] == {"fingerprint": "f1", "url": "u1", "title": "Only",
                             "fields_filled": 1, "fields_unfilled": 0}


def test_summarize_rolls_up_advances_by_tier():
    steps_a = [
        {"fingerprint": "f1", "url": "u", "title": "t",
         "advance": {"tier": 1, "control_name": "Continue", "oracle": False}},
        {"fingerprint": "f2", "url": "u", "title": "t",
         "advance": {"tier": 2, "control_name": "Continue to Payment", "oracle": False}},
    ]
    steps_b = [
        {"fingerprint": "f3", "url": "u", "title": "t",
         "advance": {"tier": 3, "control_name": "See My Quote", "oracle": True}},
    ]
    s = flow_ledger.summarize([
        _flow(flow_ledger.TERMINAL_SUBMIT_BOUNDARY, steps=steps_a),
        _flow(flow_ledger.TERMINAL_SUBMIT_BOUNDARY, steps=steps_b),
    ])
    assert s["advances_by_tier"] == {"1": 1, "2": 1, "3": 1}
    assert s["oracle_advances"] == 1


def test_summarize_without_advance_evidence_is_empty_rollup():
    s = flow_ledger.summarize([_flow(flow_ledger.TERMINAL_NO_ADVANCE)])
    assert s["advances_by_tier"] == {}
    assert s["oracle_advances"] == 0
