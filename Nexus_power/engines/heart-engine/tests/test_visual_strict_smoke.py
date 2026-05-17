"""Smoke test for P1+P2 visual_strict generators.

Runs each deterministic strategy generator against a mocked visual evidence
graph and asserts that every emitted test_case carries real, derived values
(no stubs, no hardcoded placeholders).

Usage:
    python -m pytest engines/heart-engine/tests/test_visual_strict_smoke.py -v
    # or:
    python engines/heart-engine/tests/test_visual_strict_smoke.py
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

# Add heart-engine root to path so `from main import ...` works
_HEART_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_HEART_ROOT))


def _stub_module(name: str, **attrs) -> types.ModuleType:
    """Inject a stub module into sys.modules so import statements in main.py
    don't crash when nexus_sdk / app.* are unavailable in this test env."""
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


# ── Stubs for nexus_sdk + app.* so we can import main.py and grab helpers ──
def _make_placeholder(name: str) -> type:
    """Each placeholder is its own class so they're not duplicate bases."""
    return type(name, (), {"__init__": lambda self, *a, **kw: None})


_Placeholder = _make_placeholder("_Placeholder")


def _passthrough_decorator(*args, **kwargs):
    """Stub decorator that returns the wrapped function unchanged."""
    if args and callable(args[0]) and not kwargs:
        return args[0]
    def _wrap(fn):
        return fn
    return _wrap


_stub_module(
    "nexus_sdk",
    NexusEngine=_make_placeholder("NexusEngine"),
    EngineConfig=_make_placeholder("EngineConfig"),
)
_stub_module("nexus_sdk.config", production_guard=_passthrough_decorator)
_stub_module(
    "nexus_sdk.models",
    NexusRequest=_make_placeholder("NexusRequest"),
    NexusResponse=_make_placeholder("NexusResponse"),
    JobResponse=_make_placeholder("JobResponse"),
    JobStatus=_make_placeholder("JobStatus"),
    BusinessRule=_make_placeholder("BusinessRule"),
    TestCase=_make_placeholder("TestCase"),
    TestStep=_make_placeholder("TestStep"),
    SourceReference=_make_placeholder("SourceReference"),
    Confidence=_make_placeholder("Confidence"),
)
_stub_module(
    "nexus_sdk.auth",
    NexusUser=_make_placeholder("NexusUser"),
    get_current_user=lambda *a, **kw: None,
)
_stub_module(
    "nexus_sdk.events",
    NexusEvent=_make_placeholder("NexusEvent"),
    fire_stub_alert=lambda *a, **kw: None,
)
_stub_module(
    "nexus_sdk.llm",
    LLMConfig=_make_placeholder("LLMConfig"),
    LLMResponse=_make_placeholder("LLMResponse"),
    create_provider=lambda *a, **kw: None,
)
_stub_module("nexus_sdk.llm.base", LLMProvider=_make_placeholder("LLMProvider"))
_stub_module(
    "nexus_sdk.llm.tiered",
    TieredProviderConfig=_make_placeholder("TieredProviderConfig"),
    TieredLLMRouter=_make_placeholder("TieredLLMRouter"),
)
_stub_module("nexus_sdk.worker", GPUWorkerMixin=_make_placeholder("GPUWorkerMixin"))

# app.* sub-packages
_stub_module(
    "app",
    extractors=types.ModuleType("app.extractors"),
    generators=types.ModuleType("app.generators"),
    guardrails=types.ModuleType("app.guardrails"),
)
_stub_module(
    "app.extractors",
    RuleExtractor=_make_placeholder("RuleExtractor"),
    RULE_EXTRACTION_SYSTEM="",
    RULE_EXTRACTION_USER="",
    DOCUMENT_ANALYSIS_SYSTEM="",
)
_stub_module(
    "app.generators",
    TestGenerator=_make_placeholder("TestGenerator"),
    TEST_GENERATION_SYSTEM="",
    TEST_GENERATION_USER="",
    FlowExplorer=_make_placeholder("FlowExplorer"),
    EXPLORE_FLOWS_SYSTEM="",
    EXPLORE_FLOWS_USER="",
)
_stub_module(
    "app.guardrails",
    OutputValidator=_make_placeholder("OutputValidator"),
    ValidationResult=_make_placeholder("ValidationResult"),
    ValidationSeverity=_make_placeholder("ValidationSeverity"),
)

from main import (  # noqa: E402
    _gen_state_explorer,
    _gen_cross_app,
    _gen_error_state,
    _enrich_test_case,
    _derive_preconditions,
    _derive_expected_outcome,
    _derive_risk_areas,
    _derive_workflow_steps_covered,
    _pick_control_for_edge,
    _control_label,
    _scene_label,
)


# ─── Mock visual evidence graph ──────────────────────────────────────────
# A 4-scene quote-calculator demo with one app and one cross-app boundary.

SCENES = [
    {
        "scene_id": "sc-1",
        "scene_index": 0,
        "screen_name": "Login Page",
        "app_instance_id": "app-A",
        "scene_quality": "strong",
        "ocr_text": "Sign in to your account. Welcome.",
        "detected_url": "https://example.com/login",
        "scene_state_summary": {
            "screen_title": "Sign In",
            "screen_type": "form",
            "application_label": "Quote Portal",
            "domain": "example.com",
        },
    },
    {
        "scene_id": "sc-2",
        "scene_index": 1,
        "screen_name": "Quote Form",
        "app_instance_id": "app-A",
        "scene_quality": "strong",
        "ocr_text": "Premium Inputs. Date of birth. Smoker status.",
        "detected_url": "https://example.com/quote",
        "scene_state_summary": {
            "screen_title": "Quote Calculator",
            "screen_type": "form",
            "application_label": "Quote Portal",
            "domain": "example.com",
        },
    },
    {
        "scene_id": "sc-3",
        "scene_index": 2,
        "screen_name": "Quote Result",
        "app_instance_id": "app-A",
        "scene_quality": "strong",
        "ocr_text": "Your premium: $847/month. Sign out to exit.",
        "detected_url": "https://example.com/result",
        "scene_state_summary": {
            "screen_title": "Quote Result",
            "screen_type": "results",
            "application_label": "Quote Portal",
            "domain": "example.com",
        },
    },
    {
        "scene_id": "sc-error",
        "scene_index": 3,
        "screen_name": "Validation Error",
        "app_instance_id": "app-A",
        "scene_quality": "degraded",
        "ocr_text": "Date of birth is required. Please correct the highlighted fields.",
        "detected_url": "https://example.com/quote",
        "scene_state_summary": {
            "screen_title": "Quote Calculator",
            "screen_type": "error",
            "application_label": "Quote Portal",
            "domain": "example.com",
        },
    },
    {
        "scene_id": "sc-pdf",
        "scene_index": 4,
        "screen_name": "Quote PDF Viewer",
        "app_instance_id": "app-B",
        "scene_quality": "strong",
        "ocr_text": "Quote.pdf - Adobe Reader. Page 1 of 3.",
        "detected_url": "file:///tmp/quote.pdf",
        "scene_state_summary": {
            "screen_title": "Quote PDF",
            "screen_type": "detail",
            "application_label": "Adobe Reader",
            "domain": "",
        },
    },
]

CONTROLS = {
    "sc-1": [
        {
            "control_id": "ctl-signin",
            "scene_id": "sc-1",
            "element_type": "button",
            "label_text": "Sign In",
            "playwright_selector": "button:has-text('Sign In')",
            "selector_confidence": 0.95,
            "selector_source": "ocr",
            "automation_ready": True,
        },
        {
            "control_id": "ctl-pwd",
            "scene_id": "sc-1",
            "element_type": "password",
            "label_text": "Password",
            "playwright_selector": "input[type='password']",
            "selector_confidence": 0.9,
            "selector_source": "ocr",
            "automation_ready": True,
        },
    ],
    "sc-2": [
        {
            "control_id": "ctl-dob",
            "scene_id": "sc-2",
            "element_type": "date",
            "label_text": "Date of birth",
            "playwright_selector": "input[name='dob']",
            "selector_confidence": 0.92,
            "selector_source": "ocr",
            "automation_ready": True,
        },
        {
            "control_id": "ctl-smoker",
            "scene_id": "sc-2",
            "element_type": "dropdown",
            "label_text": "Smoker status",
            "playwright_selector": "select[name='smoker']",
            "selector_confidence": 0.88,
            "selector_source": "ocr",
            "automation_ready": True,
        },
        {
            "control_id": "ctl-calculate",
            "scene_id": "sc-2",
            "element_type": "button",
            "label_text": "Calculate",
            "playwright_selector": "button:has-text('Calculate')",
            "selector_confidence": 0.94,
            "selector_source": "ocr",
            "automation_ready": True,
        },
    ],
    "sc-3": [
        {
            "control_id": "ctl-signout",
            "scene_id": "sc-3",
            "element_type": "link",
            "label_text": "Sign out",
            "playwright_selector": "a:has-text('Sign out')",
            "selector_confidence": 0.85,
            "selector_source": "ocr",
            "automation_ready": True,
        },
    ],
    "sc-error": [],
    "sc-pdf": [],
}

EDGES = [
    {
        "edge_id": "e-1",
        "edge_type": "action_confirmed_transition",
        "from_scene_id": "sc-1",
        "to_scene_id": "sc-2",
        "trigger_control_id": "ctl-signin",
        "action_type": "click",
        "action_confidence": 0.94,
        "evidence_confidence": 0.94,
        "intra_app": True,
        "primary_action_summary": {
            "action_kind": "click_cta",
            "action_label": "Click Sign In button",
        },
    },
    {
        "edge_id": "e-2",
        "edge_type": "action_confirmed_transition",
        "from_scene_id": "sc-2",
        "to_scene_id": "sc-3",
        "trigger_control_id": "ctl-calculate",
        "action_type": "click",
        "action_confidence": 0.91,
        "evidence_confidence": 0.91,
        "intra_app": True,
        "primary_action_summary": {
            "action_kind": "click_cta",
            "action_label": "Click Calculate",
        },
    },
    {
        # Error path: clicking Calculate without DOB → error scene
        "edge_id": "e-3",
        "edge_type": "action_confirmed_transition",
        "from_scene_id": "sc-2",
        "to_scene_id": "sc-error",
        "trigger_control_id": "ctl-calculate",
        "action_type": "click",
        "action_confidence": 0.80,
        "evidence_confidence": 0.80,
        "intra_app": True,
    },
    {
        # Cross-app boundary: app-A → app-B (PDF viewer)
        "edge_id": "e-4",
        "edge_type": "app_switch",
        "from_scene_id": "sc-3",
        "to_scene_id": "sc-pdf",
        "trigger_control_id": None,
        "action_type": "navigate",
        "action_confidence": 0.75,
        "evidence_confidence": 0.75,
        "intra_app": False,
    },
]

SCENE_BY_ID = {s["scene_id"]: s for s in SCENES}
CTRL_BY_SCENE = CONTROLS


# ─── Tests ────────────────────────────────────────────────────────────────


def assert_step_grounded(step: dict, msg: str = "") -> None:
    """Every step must cite real evidence — no stubs."""
    assert step.get("evidence_scene_id"), f"{msg}: missing evidence_scene_id"
    assert step["evidence_scene_id"] in SCENE_BY_ID, (
        f"{msg}: evidence_scene_id {step['evidence_scene_id']} not a real scene"
    )
    assert "evidence_edge_id" in step, f"{msg}: missing evidence_edge_id"
    assert step.get("proof_confidence", 0) > 0, f"{msg}: zero proof_confidence"
    # action and expected_output must be non-empty for real grounded steps
    assert step.get("action"), f"{msg}: empty action"


def test_state_explorer_emits_only_grounded_tests():
    tests = _gen_state_explorer(
        scenes=SCENES,
        edges=EDGES,
        scene_by_id=SCENE_BY_ID,
        ctrl_by_scene=CTRL_BY_SCENE,
        max_paths=5,
        max_depth=8,
    )
    assert len(tests) >= 1, "state_explorer should find at least one path"
    for tc in tests:
        assert tc["strategy"] == "state_explorer"
        assert tc["title"].startswith("Reach state:"), tc["title"]
        assert tc["steps"], "test must have steps"
        for step in tc["steps"]:
            assert_step_grounded(step, f"state_explorer {tc['title']}")
    print(f"[OK]state_explorer: {len(tests)} grounded tests")


def test_cross_app_emits_one_per_boundary():
    tests = _gen_cross_app(
        scenes=SCENES,
        edges=EDGES,
        scene_by_id=SCENE_BY_ID,
        ctrl_by_scene=CTRL_BY_SCENE,
    )
    # We have exactly one cross-app boundary in EDGES (app-A → app-B)
    assert len(tests) == 1, f"expected 1 cross_app test, got {len(tests)}"
    tc = tests[0]
    assert tc["strategy"] == "cross_app"
    assert "Adobe Reader" not in tc["title"] or "PDF" in tc["title"] or "Quote" in tc["title"]
    assert len(tc["steps"]) == 1
    step = tc["steps"][0]
    assert step["evidence_edge_id"] == "e-4"
    assert step["evidence_scene_id"] == "sc-3"
    print(f"[OK]cross_app: {len(tests)} grounded tests, boundary edge: {step['evidence_edge_id']}")


def test_error_state_finds_validation_scene():
    tests = _gen_error_state(
        scenes=SCENES,
        edges=EDGES,
        scene_by_id=SCENE_BY_ID,
        ctrl_by_scene=CTRL_BY_SCENE,
    )
    assert len(tests) >= 1, "error_state should detect the validation scene"
    found_required = False
    for tc in tests:
        assert tc["strategy"] == "error_state"
        assert len(tc["steps"]) == 1
        step = tc["steps"][0]
        assert_step_grounded(step, f"error_state {tc['title']}")
        # The trigger should be the click that led to the error scene
        assert step["evidence_edge_id"] == "e-3", f"expected error trigger edge e-3, got {step['evidence_edge_id']}"
        if "required" in tc["title"].lower():
            found_required = True
    assert found_required, "expected 'required' keyword in at least one error_state test title"
    print(f"[OK]error_state: {len(tests)} grounded tests")


def test_enrich_populates_real_values():
    """An LLM-style test_case fed into _enrich_test_case must come out with
    real preconditions, expected_outcome, workflow_steps_covered, and
    risk_areas_addressed — never stubs."""
    raw_test = {
        "title": "Happy path: sign in and calculate quote",
        "strategy": "happy_path",
        "steps": [
            {
                "step_number": 1,
                "action": "Click Sign In",
                "target_element": "Sign In",
                "expected_output": "Quote form visible",
                "evidence_scene_id": "sc-1",
                "evidence_control_id": "ctl-signin",
                "evidence_edge_id": "e-1",
                "proof_confidence": 0.94,
                "input_data": "",
            },
            {
                "step_number": 2,
                "action": "Click Calculate",
                "target_element": "Calculate",
                "expected_output": "Premium visible",
                "evidence_scene_id": "sc-2",
                "evidence_control_id": "ctl-calculate",
                "evidence_edge_id": "e-2",
                "proof_confidence": 0.91,
                "input_data": "",
            },
        ],
    }
    enriched = _enrich_test_case(
        raw_test,
        scene_by_id=SCENE_BY_ID,
        ctrl_by_scene=CTRL_BY_SCENE,
    )
    # preconditions: derived from first scene (sc-1 = Login page with auth keywords)
    pre = enriched["preconditions"]
    assert pre, "preconditions must not be empty"
    assert any("Sign In" in p or "Quote Portal" in p for p in pre), pre
    # expected_outcome: derived from last scene's OCR (sc-2)
    out = enriched["expected_outcome"]
    assert out, "expected_outcome must not be empty"
    assert "Quote Calculator" in out or "Premium Inputs" in out, out
    # workflow_steps_covered: unique scene_indexes + 1 (1-based)
    cov = enriched["workflow_steps_covered"]
    assert cov == [1, 2], f"expected [1, 2], got {cov}"
    # risk_areas: derived from control element_types (button → action triggering)
    risks = enriched["risk_areas_addressed"]
    assert risks, "risk_areas must not be empty"
    assert "action triggering" in risks, risks
    print(f"[OK]_enrich_test_case: preconditions={pre}, outcome={out!r}, coverage={cov}, risks={risks}")


def test_pick_control_for_edge_prefers_trigger():
    edge = EDGES[0]  # e-1 with trigger_control_id=ctl-signin
    ctrl = _pick_control_for_edge(edge, CTRL_BY_SCENE)
    assert ctrl == "ctl-signin"

    # Edge without trigger — should fall back to highest-confidence automation_ready
    edge_no_trigger = {**EDGES[3], "trigger_control_id": None}
    edge_no_trigger["from_scene_id"] = "sc-2"  # has controls
    ctrl = _pick_control_for_edge(edge_no_trigger, CTRL_BY_SCENE)
    # sc-2 has ctl-dob (0.92), ctl-smoker (0.88), ctl-calculate (0.94) — calculate wins
    assert ctrl == "ctl-calculate", f"expected ctl-calculate, got {ctrl}"
    print(f"[OK]_pick_control_for_edge: fallback to highest-confidence works")


def test_no_stub_fields_in_output():
    """Sanity check: scan all generator outputs for known stub markers."""
    forbidden_titles = ["Visual Flow Smoke Test"]
    forbidden_outcomes = ["All transitions succeed"]
    all_tests = (
        _gen_state_explorer(scenes=SCENES, edges=EDGES, scene_by_id=SCENE_BY_ID, ctrl_by_scene=CTRL_BY_SCENE)
        + _gen_cross_app(scenes=SCENES, edges=EDGES, scene_by_id=SCENE_BY_ID, ctrl_by_scene=CTRL_BY_SCENE)
        + _gen_error_state(scenes=SCENES, edges=EDGES, scene_by_id=SCENE_BY_ID, ctrl_by_scene=CTRL_BY_SCENE)
    )
    for tc in all_tests:
        assert tc["title"] not in forbidden_titles, f"stub title leaked: {tc['title']}"
        for step in tc["steps"]:
            assert step.get("expected_output") not in forbidden_outcomes, (
                f"stub expected_output leaked: {step['expected_output']}"
            )
    print(f"[OK]no stub fields detected across {len(all_tests)} generated tests")


def test_helpers_handle_empty_graphs_gracefully():
    """Generators must return [] (not crash) on empty input."""
    assert _gen_state_explorer(scenes=[], edges=[], scene_by_id={}, ctrl_by_scene={}) == []
    assert _gen_cross_app(scenes=[], edges=[], scene_by_id={}, ctrl_by_scene={}) == []
    assert _gen_error_state(scenes=[], edges=[], scene_by_id={}, ctrl_by_scene={}) == []
    # Scenes only, no edges
    assert _gen_state_explorer(scenes=SCENES, edges=[], scene_by_id=SCENE_BY_ID, ctrl_by_scene=CTRL_BY_SCENE) == []
    print("[OK]empty-input handling works")


def test_derive_helpers_handle_missing_scenes():
    """Derive helpers must return empty/safe values when fed an empty scene."""
    assert _derive_preconditions({}) == []
    assert _derive_expected_outcome({}) == ""
    assert _derive_risk_areas({"steps": []}, {}) == []
    assert _derive_workflow_steps_covered({"steps": []}, {}) == []
    print("[OK]derive helpers handle missing data safely")


if __name__ == "__main__":
    test_state_explorer_emits_only_grounded_tests()
    test_cross_app_emits_one_per_boundary()
    test_error_state_finds_validation_scene()
    test_enrich_populates_real_values()
    test_pick_control_for_edge_prefers_trigger()
    test_no_stub_fields_in_output()
    test_helpers_handle_empty_graphs_gracefully()
    test_derive_helpers_handle_missing_scenes()
    print("\nAll smoke tests passed.")
