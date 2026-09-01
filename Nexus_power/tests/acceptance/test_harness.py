"""Unit tests for the acceptance harness — exercise the matching logic
without hitting the live API or requiring real recordings.

These tests run as part of the regular SDK test suite so the harness
itself stays correct as the production pipeline evolves.
"""
from __future__ import annotations

import os
import textwrap

import pytest

from .harness import (
    AcceptanceThresholds,
    ExpectedAction,
    FixtureManifest,
    evaluate_fixture,
    load_fixture,
    match_actions_to_steps,
)


# ─── Synthetic step builders ─────────────────────────────────────────────────

def _step(
    action_kind: str,
    start_ms: int,
    *,
    target_label: str = "",
    observed_value: str = "",
) -> dict:
    return {
        "action_kind": action_kind,
        "start_ms": start_ms,
        "end_ms": start_ms + 500,
        "target_label": target_label,
        "observed_value": observed_value,
        "step_id": f"s-{start_ms}",
    }


def _expected(
    kind: str,
    *,
    ts_min: int,
    ts_max: int,
    target: str | None = None,
    value: str | None = None,
) -> ExpectedAction:
    return ExpectedAction(
        timestamp_ms_min=ts_min,
        timestamp_ms_max=ts_max,
        action_kind=kind,
        target_label_contains=target,
        observed_value_contains=value,
    )


# ─── match_actions_to_steps ──────────────────────────────────────────────────

def test_perfect_match_all_actions_paired():
    expected = [
        _expected("click_cta", ts_min=0, ts_max=2000, target="Submit"),
        _expected("enter_text", ts_min=3000, ts_max=5000, target="Year", value="1990"),
    ]
    actual = [
        _step("click_cta", 1000, target_label="Submit button"),
        _step("enter_text", 4000, target_label="Year input", observed_value="1990"),
    ]
    matches, spurious = match_actions_to_steps(expected, actual)
    assert len(matches) == 2
    assert all(m.actual_step is not None for m in matches)
    assert spurious == []
    assert all(m.kind_correct for m in matches)
    assert all(m.target_correct for m in matches)
    assert matches[1].value_correct is True


def test_compatible_action_kinds_match():
    """``click`` (actual) satisfies an expected ``click_cta`` annotation."""
    expected = [_expected("click_cta", ts_min=0, ts_max=2000, target="Submit")]
    actual = [_step("click", 1000, target_label="Submit")]
    matches, spurious = match_actions_to_steps(expected, actual)
    assert matches[0].actual_step is not None
    assert matches[0].kind_correct is True


def test_step_outside_window_is_spurious():
    expected = [_expected("click_cta", ts_min=1000, ts_max=2000, target="Submit")]
    actual = [_step("click_cta", 5000, target_label="Submit")]
    matches, spurious = match_actions_to_steps(expected, actual)
    assert matches[0].actual_step is None
    assert len(spurious) == 1


def test_target_substring_match_case_insensitive():
    expected = [_expected("click_cta", ts_min=0, ts_max=2000, target="submit")]
    actual = [_step("click_cta", 1000, target_label="Submit Form Button")]
    matches, _ = match_actions_to_steps(expected, actual)
    assert matches[0].target_correct is True


def test_target_mismatch_records_kind_correct_but_target_wrong():
    expected = [_expected("click_cta", ts_min=0, ts_max=2000, target="Submit")]
    actual = [_step("click_cta", 1000, target_label="Cancel")]
    matches, _ = match_actions_to_steps(expected, actual)
    assert matches[0].kind_correct is True
    assert matches[0].target_correct is False


def test_value_match_only_counted_when_required():
    """Expected actions WITHOUT observed_value_contains do not penalise."""
    expected = [_expected("click_cta", ts_min=0, ts_max=2000, target="Submit")]
    actual = [_step("click_cta", 1000, target_label="Submit", observed_value="ignored")]
    matches, _ = match_actions_to_steps(expected, actual)
    # No value expected — value_correct trivially True.
    assert matches[0].value_correct is True


def test_greedy_centre_proximity_when_multiple_candidates():
    """When two actual steps fall in the same window, the one closer to
    the window centre is paired."""
    expected = [_expected("click_cta", ts_min=0, ts_max=2000, target="A")]
    actual = [
        _step("click_cta", 200, target_label="A"),    # far from centre 1000
        _step("click_cta", 950, target_label="A"),    # near centre
    ]
    matches, spurious = match_actions_to_steps(expected, actual)
    assert matches[0].actual_step["start_ms"] == 950
    assert spurious[0]["start_ms"] == 200


def test_extra_actual_steps_count_as_spurious():
    expected = [_expected("click_cta", ts_min=0, ts_max=2000, target="A")]
    actual = [
        _step("click_cta", 1000, target_label="A"),
        _step("click_cta", 1500, target_label="B"),
        _step("enter_text", 1800, target_label="C"),
    ]
    matches, spurious = match_actions_to_steps(expected, actual)
    assert len(matches) == 1
    assert len(spurious) == 2


def test_unmatched_expected_action_yields_none_actual():
    expected = [
        _expected("click_cta", ts_min=0, ts_max=2000, target="A"),
        _expected("submit_form", ts_min=3000, ts_max=5000),
    ]
    actual = [_step("click_cta", 1000, target_label="A")]
    matches, _ = match_actions_to_steps(expected, actual)
    assert matches[1].actual_step is None


# ─── evaluate_fixture / FixtureResult ────────────────────────────────────────

def _fixture(thresholds: AcceptanceThresholds, expected: list[ExpectedAction]) -> FixtureManifest:
    return FixtureManifest(
        name="test-fixture",
        description="",
        video_path=None,
        artifact_id=None,
        thresholds=thresholds,
        expected_actions=expected,
    )


def test_passing_fixture_meets_all_thresholds():
    thresholds = AcceptanceThresholds(
        action_kind_accuracy=0.5,
        target_match_rate=0.5,
        value_match_rate=0.0,
        max_spurious_steps=2,
    )
    expected = [
        _expected("click_cta", ts_min=0, ts_max=2000, target="Save"),
        _expected("enter_text", ts_min=3000, ts_max=5000, target="Year", value="1990"),
    ]
    actual = [
        _step("click_cta", 1000, target_label="Save"),
        _step("enter_text", 4000, target_label="Year", observed_value="1990"),
    ]
    result = evaluate_fixture(_fixture(thresholds, expected), actual)
    assert result.passed is True
    assert result.action_kind_accuracy == 1.0


def test_failing_fixture_below_kind_accuracy():
    thresholds = AcceptanceThresholds(
        action_kind_accuracy=0.9, target_match_rate=0.0,
        value_match_rate=0.0, max_spurious_steps=10,
    )
    expected = [
        _expected("click_cta", ts_min=0, ts_max=1000),
        _expected("submit_form", ts_min=2000, ts_max=3000),
    ]
    actual = [_step("click_cta", 500)]
    result = evaluate_fixture(_fixture(thresholds, expected), actual)
    assert result.action_kind_accuracy == 0.5
    assert result.passed is False


def test_too_many_spurious_steps_fails():
    thresholds = AcceptanceThresholds(
        action_kind_accuracy=0.0, target_match_rate=0.0,
        value_match_rate=0.0, max_spurious_steps=1,
    )
    expected = [_expected("click_cta", ts_min=0, ts_max=2000)]
    actual = [
        _step("click_cta", 1000),
        _step("click_cta", 5000),  # spurious
        _step("enter_text", 6000),  # spurious
    ]
    result = evaluate_fixture(_fixture(thresholds, expected), actual)
    assert len(result.spurious_steps) == 2
    assert result.passed is False


def test_summary_line_includes_pass_fail_and_metrics():
    thresholds = AcceptanceThresholds()
    expected = [_expected("click_cta", ts_min=0, ts_max=2000, target="Save")]
    actual = [_step("click_cta", 1000, target_label="Save")]
    result = evaluate_fixture(_fixture(thresholds, expected), actual)
    line = result.summary_line()
    assert result.fixture.name in line
    assert "PASS" in line or "FAIL" in line
    assert "matched" in line


# ─── load_fixture ───────────────────────────────────────────────────────────

def test_load_fixture_parses_valid_yaml(tmp_path):
    yaml = pytest.importorskip("yaml")
    path = tmp_path / "f.yaml"
    path.write_text(textwrap.dedent("""\
        name: usaa-life-quote
        description: Quick demo of the USAA life-quote form
        video_path: ../recordings/x.mp4
        artifact_id: art-123
        thresholds:
          action_kind_accuracy: 0.85
          target_match_rate: 0.7
          value_match_rate: 0.5
          max_spurious_steps: 4
        expected_actions:
          - timestamp_ms_min: 1000
            timestamp_ms_max: 3000
            action_kind: click_cta
            target_label_contains: Continue
          - timestamp_ms_min: 5000
            timestamp_ms_max: 8000
            action_kind: enter_text
            target_label_contains: Birthdate
            observed_value_contains: '1990'
            notes: 'Form-fill step'
    """))
    fix = load_fixture(str(path))
    assert fix.name == "usaa-life-quote"
    assert fix.artifact_id == "art-123"
    assert fix.thresholds.action_kind_accuracy == 0.85
    assert fix.thresholds.max_spurious_steps == 4
    assert len(fix.expected_actions) == 2
    e1 = fix.expected_actions[0]
    assert e1.action_kind == "click_cta"
    assert e1.target_label_contains == "Continue"
    assert e1.observed_value_contains is None
    e2 = fix.expected_actions[1]
    assert e2.observed_value_contains == "1990"
    assert e2.notes == "Form-fill step"


def test_load_fixture_missing_name_raises(tmp_path):
    pytest.importorskip("yaml")
    path = tmp_path / "bad.yaml"
    path.write_text("description: missing name field\n")
    with pytest.raises(ValueError, match="name"):
        load_fixture(str(path))


def test_load_fixture_invalid_top_level_raises(tmp_path):
    pytest.importorskip("yaml")
    path = tmp_path / "bad.yaml"
    path.write_text("- just\n- a\n- list\n")
    with pytest.raises(ValueError):
        load_fixture(str(path))


def test_load_fixture_thresholds_default_when_absent(tmp_path):
    pytest.importorskip("yaml")
    path = tmp_path / "minimal.yaml"
    path.write_text(textwrap.dedent("""\
        name: minimal
        expected_actions: []
    """))
    fix = load_fixture(str(path))
    assert fix.thresholds.action_kind_accuracy == AcceptanceThresholds.action_kind_accuracy
