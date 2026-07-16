"""Phase 5 — regression diff + classifier (the never-green-wash core).

The headline proof: a value/currency-shaped change ($250 → $180) is a GENUINE
regression even with NO explicit oracle tag (fail-safe), and an advisory 'match' signal
can never downgrade it. Plus: first-run establishes a baseline (no false storm), a
self-heal only counts with a proven clean run, and an unconfirmed change is UNPROVEN
(never benign).
"""
from __future__ import annotations

from app.controlplane.cycle import regression_diff as rd
from app.controlplane.cycle import regression_verdict as rv


# ── diff + fail-safe oracle detection ─────────────────────────────────────────
def test_value_shaped_strings_are_oracle():
    assert rd.is_value_shaped("$250.00")
    assert rd.is_value_shaped("1,000")
    assert rd.is_value_shaped("41.50")
    assert not rd.is_value_shaped("Sent")
    assert not rd.is_value_shaped("Completed")


def test_diff_marks_amount_change_as_oracle_without_a_tag():
    d = rd.diff_field("premium", "250.00", "180.00")
    assert d["kind"] == rd.VALUE_CHANGED and d["is_oracle"] is True


def test_diff_wording_change_is_not_oracle():
    d = rd.diff_field("confirmation", "Sent", "Completed")
    assert d["kind"] == rd.VALUE_CHANGED and d["is_oracle"] is False


def test_diff_missing_and_new():
    assert rd.diff_field("x", "v", None)["kind"] == rd.MISSING
    assert rd.diff_field("x", None, "v")["kind"] == rd.NEW


# ── classifier — the guard order ──────────────────────────────────────────────
def _diffs(baseline, current, oracle=()):
    return rd.diff(baseline, current, oracle_fields=oracle)


def test_amount_change_is_genuine_even_with_match_signal():
    # The flagship: $250 → $180, and the VLM says 'match' → still GENUINE (fail-safe).
    diffs = _diffs({"amount": "250.00"}, {"amount": "180.00"})
    v = rv.classify(run_status="passed", diffs=diffs, has_baseline=True, semantic_signal="match")
    assert v["disposition"] == rv.GENUINE_REGRESSION and v["needs_review"]


def test_wording_change_with_match_is_benign():
    diffs = _diffs({"confirmation": "Sent"}, {"confirmation": "Completed"})
    v = rv.classify(run_status="passed", diffs=diffs, has_baseline=True, semantic_signal="match")
    assert v["disposition"] == rv.BENIGN_DRIFT and not v["needs_review"]


def test_wording_change_without_confirming_signal_is_unproven():
    diffs = _diffs({"confirmation": "Sent"}, {"confirmation": "Completed"})
    v = rv.classify(run_status="passed", diffs=diffs, has_baseline=True, semantic_signal="")
    assert v["disposition"] == rv.HONEST_UNPROVEN


def test_semantic_deviation_is_genuine():
    diffs = _diffs({"confirmation": "Sent"}, {"confirmation": "Error"})
    v = rv.classify(run_status="passed", diffs=diffs, has_baseline=True, semantic_signal="deviation")
    assert v["disposition"] == rv.GENUINE_REGRESSION


def test_first_run_establishes_baseline_not_a_regression():
    diffs = _diffs({}, {"amount": "250.00"})
    v = rv.classify(run_status="passed", diffs=diffs, has_baseline=False)
    assert v["disposition"] == rv.FIRST_BASELINE and not v["needs_review"]


def test_failed_unhealed_is_genuine():
    v = rv.classify(run_status="failed", diffs=[], has_baseline=True, heal_result={})
    assert v["disposition"] == rv.GENUINE_REGRESSION


def test_failed_healed_with_proven_clean_run_is_self_healed():
    v = rv.classify(run_status="failed", diffs=[], has_baseline=True,
                    heal_result={"clean_run_version": "v3"})
    assert v["disposition"] == rv.SELF_HEALED and not v["needs_review"]


def test_failed_healed_without_clean_run_is_genuine():
    # A heal that never reached a proven clean run is NOT a silent green.
    v = rv.classify(run_status="failed", diffs=[], has_baseline=True,
                    heal_result={"clean_run_version": ""})
    assert v["disposition"] == rv.GENUINE_REGRESSION


def test_missing_observation_is_unproven():
    diffs = _diffs({"premium": "250.00"}, {})  # baseline had it, run did not observe it
    v = rv.classify(run_status="passed", diffs=diffs, has_baseline=True, semantic_signal="match")
    assert v["disposition"] == rv.HONEST_UNPROVEN


def test_no_change_is_pass():
    diffs = _diffs({"amount": "250.00"}, {"amount": "250.00"})
    v = rv.classify(run_status="passed", diffs=diffs, has_baseline=True)
    assert v["disposition"] == rv.PASS_UNCHANGED and not v["needs_review"]


# ── narrative ─────────────────────────────────────────────────────────────────
def test_narrative_for_benign_drift_matches_the_example():
    diffs = _diffs({"confirmation": "Sent"}, {"confirmation": "Completed"})
    v = rv.classify(run_status="passed", diffs=diffs, has_baseline=True, semantic_signal="match")
    text = rv.narrative("Transfer", v, diffs)
    assert "Transfer still works" in text and "Sent" in text and "Completed" in text


def test_narrative_for_genuine_regression():
    diffs = _diffs({"amount": "250.00"}, {"amount": "180.00"})
    v = rv.classify(run_status="passed", diffs=diffs, has_baseline=True)
    text = rv.narrative("Transfer", v, diffs)
    assert "review" in text.lower() and "180" in text
