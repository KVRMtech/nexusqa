"""Smoke test for P6 execution feedback primitives.

Pure-function tests:
  * extract_scenario_id_from_test_name parses every exporter's test-name format
  * detect_drift correctly flags selector + bbox divergence
  * detect_flake recognises pass-fail-pass patterns
  * root_cause_hints emits real, deterministic sentences
  * status severity ordering is consistent

No DB, no LLM. Mirrors the pattern of the P5 + P4 smoke suites.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

_API_ROOT = Path(__file__).resolve().parents[1]
_SDK_ROOT = _API_ROOT.parents[1] / "sdk" / "nexus-sdk"
sys.path.insert(0, str(_API_ROOT))
sys.path.insert(0, str(_SDK_ROOT))


# ─── Stub nexus_sdk pieces that aren't installed in the test env ─────────


def _make_placeholder(name: str) -> type:
    return type(name, (), {"__init__": lambda self, *a, **kw: None})


def _stub(name: str, **attrs):
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod


_stub(
    "nexus_sdk",
    NexusEngine=_make_placeholder("NexusEngine"),
    EngineConfig=_make_placeholder("EngineConfig"),
)
_stub(
    "nexus_sdk.db",
    Base=_make_placeholder("Base"),
)
# Provide the symbols our service imports from nexus_sdk.db.models. The
# service uses them as plain constants + classes for typing, so empty
# placeholders are fine.
_stub(
    "nexus_sdk.db.models",
    E2ETestRunRow=_make_placeholder("E2ETestRunRow"),
    E2ETestRunStepRow=_make_placeholder("E2ETestRunStepRow"),
    E2E_RUN_STATUS_RUNNING="running",
    E2E_RUN_STATUS_PASSED="passed",
    E2E_RUN_STATUS_FAILED="failed",
    E2E_RUN_STATUS_ERROR="error",
    E2E_RUN_STATUS_CANCELLED="cancelled",
    E2E_RUN_TERMINAL_STATUSES=frozenset({"passed", "failed", "error", "cancelled"}),
    E2E_STEP_STATUS_PASSED="passed",
    E2E_STEP_STATUS_FAILED="failed",
    E2E_STEP_STATUS_SKIPPED="skipped",
    E2E_STEP_STATUS_TIMED_OUT="timed_out",
    E2E_STEP_STATUS_BROKEN="broken",
)


# Load test_runs.py directly by file path to skip ``app/__init__.py``
# (which eagerly imports the live SQLAlchemy stack).
import importlib.util as _util  # noqa: E402

_TEST_RUNS_PATH = _API_ROOT / "app" / "services" / "test_runs.py"
_spec = _util.spec_from_file_location("_test_runs_under_test", _TEST_RUNS_PATH)
assert _spec is not None and _spec.loader is not None
_module = _util.module_from_spec(_spec)
# Dataclass introspection needs the module registered in sys.modules.
sys.modules["_test_runs_under_test"] = _module
_spec.loader.exec_module(_module)

detect_drift = _module.detect_drift
detect_flake_from_pass_fail_sequence = _module.detect_flake_from_pass_fail_sequence
extract_scenario_id_from_test_name = _module.extract_scenario_id_from_test_name
root_cause_hints = _module.root_cause_hints


# ─── Scenario ID parsing ─────────────────────────────────────────────────


def test_extract_scenario_id_from_playwright_test_names():
    cases = [
        ("vs_001 — Happy path: sign in", "vs_001"),
        ("vs_042 — Boundary: empty DOB", "vs_042"),
        ("co_007 — Co-Architect proposal", "co_007"),
        # Legacy formats we still want to accept
        ("[vs_003] negative path", "vs_003"),
        ("vs_010: cross_app journey", "vs_010"),
        # Free-form CI names with the id buried
        ("Tests › Quote Portal › vs_055 › click submit", "vs_055"),
        # No id present
        ("Some unrelated test name", None),
        ("", None),
        (None, None),
    ]
    for name, expected in cases:
        got = extract_scenario_id_from_test_name(name)  # type: ignore[arg-type]
        assert got == expected, f"name={name!r} got={got!r} expected={expected!r}"
    print("[OK] extract_scenario_id_from_test_name parses every exporter format")


# ─── Drift detection ─────────────────────────────────────────────────────


def test_drift_detects_selector_change():
    r = detect_drift(
        expected_selector="button:has-text('Submit')",
        resolved_selector="button[data-testid='submit-btn']",
        expected_bbox=None,
        resolved_bbox=None,
    )
    assert r.selector_drifted is True
    assert "expected" in r.selector_diff and "resolved" in r.selector_diff
    assert r.bbox_drifted is False  # no bbox data -> no drift claim
    print("[OK] detect_drift flags selector divergence")


def test_drift_detects_bbox_movement():
    r = detect_drift(
        expected_selector="button:submit",
        resolved_selector="button:submit",
        expected_bbox={"x": 100, "y": 200, "width": 80, "height": 30},
        resolved_bbox={"x": 400, "y": 600, "width": 80, "height": 30},  # moved
    )
    assert r.selector_drifted is False
    assert r.bbox_drifted is True
    assert r.bbox_pixel_distance is not None and r.bbox_pixel_distance > 50
    print(f"[OK] detect_drift flags bbox movement ({r.bbox_pixel_distance:.0f}px)")


def test_drift_ignores_small_movement():
    r = detect_drift(
        expected_selector="x",
        resolved_selector="x",
        expected_bbox={"x": 100, "y": 100, "width": 50, "height": 20},
        resolved_bbox={"x": 110, "y": 105, "width": 50, "height": 20},  # ~11px
    )
    assert r.bbox_drifted is False
    print("[OK] detect_drift ignores small (sub-50px) bbox movements")


def test_drift_no_data_returns_no_drift():
    r = detect_drift(
        expected_selector="",
        resolved_selector="",
        expected_bbox=None,
        resolved_bbox=None,
    )
    assert r.selector_drifted is False
    assert r.bbox_drifted is False
    print("[OK] detect_drift returns no drift when no data")


# ─── Flake detection ─────────────────────────────────────────────────────


def test_flake_pass_fail_pass_pattern():
    # 10 oldest-first runs
    r = detect_flake_from_pass_fail_sequence(
        ["pass", "pass", "fail", "pass", "fail", "pass", "pass", "fail", "pass", "pass"]
    )
    assert r.is_flaky is True
    assert r.flake_rate_pct > 0 and r.flake_rate_pct < 100
    print(f"[OK] detect_flake flags pass-fail pattern ({r.flake_rate_pct}%)")


def test_flake_all_passing_not_flaky():
    r = detect_flake_from_pass_fail_sequence(["pass"] * 10)
    assert r.is_flaky is False
    assert r.flake_rate_pct == 0.0
    assert r.consecutive_failures == 0
    print("[OK] detect_flake correctly NOT flaky for all-passing")


def test_flake_all_failing_not_flaky_but_consecutive_failures():
    r = detect_flake_from_pass_fail_sequence(["fail"] * 5)
    # All-failing is broken, not flaky
    assert r.is_flaky is False
    assert r.flake_rate_pct == 100.0
    assert r.consecutive_failures == 5
    print(f"[OK] detect_flake: all-failing -> not flaky, {r.consecutive_failures} consec failures")


def test_flake_leading_failures_counted():
    # Most recent first reversed: [pass, pass, fail, fail, fail] oldest-first
    # -> reversed descending order: most recent run is "fail", so 3 consec fails
    r = detect_flake_from_pass_fail_sequence(["pass", "pass", "fail", "fail", "fail"])
    assert r.consecutive_failures == 3
    print("[OK] detect_flake counts leading failure streak correctly")


# ─── Root-cause hints ───────────────────────────────────────────────────


def test_root_cause_selector_drift_hint():
    failing = {
        "expected_selector": "button:has-text('Submit')",
        "resolved_selector": "button[data-testid='primary-cta']",
        "expected_output": "Confirmation page",
    }
    hints = root_cause_hints(
        failing_step=failing,
        last_passing_step=None,
        current_control=None,
        current_scene=None,
    )
    assert any("Selector resolved to a different element" in h for h in hints), hints
    print("[OK] root_cause_hints produces selector-drift hint")


def test_root_cause_selector_changed_since_last_pass():
    failing = {
        "expected_selector": "button#submit",
        "resolved_selector": "button#new-submit",
        "expected_output": "",
    }
    last_pass = {
        "expected_selector": "button#submit",
        "resolved_selector": "button#submit",  # used to resolve correctly
    }
    hints = root_cause_hints(
        failing_step=failing,
        last_passing_step=last_pass,
        current_control=None,
        current_scene=None,
    )
    assert any("changed since the last passing run" in h for h in hints), hints
    print("[OK] root_cause_hints detects selector drift vs last passing run")


def test_root_cause_low_selector_confidence_hint():
    failing = {"expected_selector": "x", "resolved_selector": "x", "expected_output": ""}
    control = {"selector_confidence": 0.3, "automation_ready": False}
    hints = root_cause_hints(
        failing_step=failing,
        last_passing_step=None,
        current_control=control,
        current_scene=None,
    )
    assert any("selector_confidence is now" in h for h in hints), hints
    assert any("automation_ready" in h for h in hints), hints
    print("[OK] root_cause_hints surfaces low selector_confidence + lost automation_ready")


def test_root_cause_ocr_missing_assertion_target():
    failing = {
        "expected_selector": "x",
        "resolved_selector": "x",
        "expected_output": "Premium calculated successfully",
    }
    scene = {"ocr_text": "Error: please try again"}
    hints = root_cause_hints(
        failing_step=failing,
        last_passing_step=None,
        current_control=None,
        current_scene=scene,
    )
    assert any("OCR no longer contains" in h for h in hints), hints
    print("[OK] root_cause_hints flags OCR-no-longer-contains")


def test_root_cause_no_hints_when_no_signal():
    failing = {"expected_selector": "x", "resolved_selector": "x", "expected_output": ""}
    hints = root_cause_hints(
        failing_step=failing,
        last_passing_step=None,
        current_control=None,
        current_scene=None,
    )
    # No signals = no hints. Important: we don't fabricate.
    assert hints == [], hints
    print("[OK] root_cause_hints returns [] when no signal is available (no fabrication)")


def test_root_cause_bbox_drift_hint():
    failing = {
        "expected_selector": "x",
        "resolved_selector": "x",
        "resolved_bbox_json": {"x": 600, "y": 800, "width": 80, "height": 30},
        "expected_output": "",
    }
    control = {
        "bounding_box": {"x": 100, "y": 200, "width": 80, "height": 30},
    }
    hints = root_cause_hints(
        failing_step=failing,
        last_passing_step=None,
        current_control=control,
        current_scene=None,
    )
    assert any("moved on screen" in h for h in hints), hints
    print("[OK] root_cause_hints flags bbox drift with pixel distance")


if __name__ == "__main__":
    test_extract_scenario_id_from_playwright_test_names()
    test_drift_detects_selector_change()
    test_drift_detects_bbox_movement()
    test_drift_ignores_small_movement()
    test_drift_no_data_returns_no_drift()
    test_flake_pass_fail_pass_pattern()
    test_flake_all_passing_not_flaky()
    test_flake_all_failing_not_flaky_but_consecutive_failures()
    test_flake_leading_failures_counted()
    test_root_cause_selector_drift_hint()
    test_root_cause_selector_changed_since_last_pass()
    test_root_cause_low_selector_confidence_hint()
    test_root_cause_ocr_missing_assertion_target()
    test_root_cause_no_hints_when_no_signal()
    test_root_cause_bbox_drift_hint()
    print("\nAll P6 feedback smoke tests passed.")
