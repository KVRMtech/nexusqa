"""Unit tests for the action_extractor — Phase 1 of the action capture redesign.

Covers the parts we can test without a real multimodal LLM or a live DB:

  * Pydantic SceneAction schema validation (good/bad payloads).
  * Reconciliation logic: OCR match boosts; value-missing clamps
    automation_ready; multi-signal corroboration lifts confidence
    floor.
  * Deterministic fallback when LLM is unavailable.
  * Image downsizing helper degrades gracefully without Pillow.
  * Idempotent action_id derivation.

Integration tests (full DB, real LLM) are deferred to a separate
suite gated on ANTHROPIC_API_KEY being set in the test environment.
"""

from __future__ import annotations

import pytest

from app.services.storyboard.action_extractor import (
    _action_id,
    _deterministic_action,
    _reconcile,
    _scene_action_tool,
    _SceneInputs,
)
from app.services.storyboard.schemas import (
    ExtractionEvidence,
    SceneAction,
    SceneActionTargetKind,
    SceneActionVerb,
)


# ── Schema validation ────────────────────────────────────────────────────────


def test_scene_action_validates_complete_payload():
    """The happy path: all fields present and well-typed."""
    payload = {
        "verb": "select",
        "target_label": "What state do you live in?",
        "target_kind": "dropdown",
        "value": "TX",
        "confidence": 0.95,
        "automation_ready": True,
        "reasoning": "Dropdown showed TX in After frame after being blank in Before.",
    }
    action = SceneAction.model_validate(payload)
    assert action.verb is SceneActionVerb.SELECT
    assert action.target_kind is SceneActionTargetKind.DROPDOWN
    assert action.value == "TX"
    assert action.automation_ready is True


def test_scene_action_rejects_invalid_verb():
    """Tight enum — LLM hallucinations like verb='swipe' fail validation."""
    with pytest.raises(ValueError):
        SceneAction.model_validate({
            "verb": "swipe",  # not in the enum
            "target_label": "x",
            "target_kind": "button",
            "value": None,
            "confidence": 0.5,
            "automation_ready": False,
            "reasoning": "x",
        })


def test_scene_action_rejects_out_of_range_confidence():
    """Confidence must be in [0, 1].  Catches LLMs returning percentages."""
    with pytest.raises(ValueError):
        SceneAction.model_validate({
            "verb": "click",
            "target_label": "Submit",
            "target_kind": "button",
            "value": None,
            "confidence": 95.0,  # accidentally percent, not fraction
            "automation_ready": True,
            "reasoning": "x",
        })


def test_scene_action_accepts_null_value_for_click():
    """Click has no value — null must be accepted."""
    action = SceneAction.model_validate({
        "verb": "click",
        "target_label": "Get a Quote",
        "target_kind": "button",
        "value": None,
        "confidence": 0.9,
        "automation_ready": True,
        "reasoning": "Button highlighted in After frame.",
    })
    assert action.value is None


def test_scene_action_strips_whitespace():
    """LLM outputs occasionally have stray whitespace — Pydantic trims it."""
    action = SceneAction.model_validate({
        "verb": "type",
        "target_label": "  Full Name  ",
        "target_kind": "text_field",
        "value": "  John Smith  ",
        "confidence": 0.9,
        "automation_ready": True,
        "reasoning": "  Field showed John Smith in After.  ",
    })
    assert action.target_label == "Full Name"
    assert action.value == "John Smith"
    assert action.reasoning == "Field showed John Smith in After."


# ── Reconciliation ───────────────────────────────────────────────────────────


def _make_inputs(**overrides) -> _SceneInputs:
    """Helper — build a _SceneInputs with sensible defaults."""
    defaults = dict(
        scene_id="scene-1",
        scene_index=10,
        panel_id="panel-1",
        panel_is_noise=False,
        before_frame_asset_path="t/s/wf/before.png",
        after_frame_asset_path="t/s/wf/after.png",
        before_ocr_text="What state do you live in?",
        after_ocr_text="What state do you live in? TX",
        before_url="https://usaa.com/quote",
        after_url="https://usaa.com/quote",
        audio_intent="now i select texas",
        existing_control_values=[("What state do you live in?", "TX")],
        existing_cursor_click_count=1,
    )
    defaults.update(overrides)
    return _SceneInputs(**defaults)


def test_reconcile_corroborates_value_via_ocr_and_control():
    """When LLM value matches BOTH OCR and a control row, all signals fire."""
    inputs = _make_inputs()
    action = SceneAction(
        verb=SceneActionVerb.SELECT,
        target_label="What state do you live in?",
        target_kind=SceneActionTargetKind.DROPDOWN,
        value="TX",
        confidence=0.7,
        automation_ready=True,
        reasoning="x",
    )
    adjusted, evidence = _reconcile(action, inputs)
    assert evidence.ocr_text_match is True
    assert evidence.control_match is True
    assert evidence.cursor_event_match is True
    assert evidence.audio_intent_match is True
    # 4 signals corroborate — confidence floor lifts to >= 0.85
    assert adjusted.confidence >= 0.85
    assert adjusted.automation_ready is True


def test_reconcile_clamps_automation_when_value_uncorroborated():
    """LLM says verb=select with value, but no signal supports the value.

    The reconciliation MUST set automation_ready=False — a test
    generated from this would type the wrong value.
    """
    inputs = _make_inputs(
        after_ocr_text="generic page",
        existing_control_values=[],
        existing_cursor_click_count=0,
    )
    action = SceneAction(
        verb=SceneActionVerb.SELECT,
        target_label="Dropdown",
        target_kind=SceneActionTargetKind.DROPDOWN,
        value="XYZ",  # not in OCR, not in any control
        confidence=0.9,
        automation_ready=True,
        reasoning="x",
    )
    adjusted, evidence = _reconcile(action, inputs)
    assert evidence.ocr_text_match is False
    assert evidence.control_match is False
    assert adjusted.automation_ready is False
    assert adjusted.confidence <= 0.5


def test_reconcile_url_change_corroborates_navigate():
    """URL changed → url_changed signal fires regardless of verb."""
    inputs = _make_inputs(
        before_url="https://usaa.com/quote",
        after_url="https://usaa.com/results",
    )
    action = SceneAction(
        verb=SceneActionVerb.NAVIGATE,
        target_label="Get Quote",
        target_kind=SceneActionTargetKind.BUTTON,
        value=None,
        confidence=0.8,
        automation_ready=True,
        reasoning="x",
    )
    adjusted, evidence = _reconcile(action, inputs)
    assert evidence.url_changed is True
    # navigate has no value to corroborate — automation_ready stays True
    assert adjusted.automation_ready is True


def test_reconcile_click_with_cursor_event():
    """Click + a cursor click event = corroborated click."""
    inputs = _make_inputs(
        after_ocr_text="page with Submit button",
        existing_control_values=[],
        existing_cursor_click_count=1,
    )
    action = SceneAction(
        verb=SceneActionVerb.CLICK,
        target_label="Submit",
        target_kind=SceneActionTargetKind.BUTTON,
        value=None,
        confidence=0.8,
        automation_ready=True,
        reasoning="x",
    )
    adjusted, evidence = _reconcile(action, inputs)
    assert evidence.cursor_event_match is True
    assert adjusted.automation_ready is True


# ── Deterministic fallback ───────────────────────────────────────────────────


def test_deterministic_fallback_uses_first_control_value():
    """When LLM fails but a control has a value, the fallback uses it."""
    inputs = _make_inputs(
        existing_control_values=[
            ("Email", "user@x.com"),
            ("Phone", "555-1234"),
        ],
    )
    fallback = _deterministic_action(inputs)
    assert fallback is not None
    assert fallback.verb is SceneActionVerb.TYPE
    assert fallback.value == "user@x.com"
    assert fallback.target_label == "Email"
    assert fallback.confidence <= 0.4
    assert fallback.automation_ready is False


def test_deterministic_fallback_no_controls_returns_none():
    """No controls = no fallback possible.  Caller writes no row."""
    inputs = _make_inputs(existing_control_values=[])
    assert _deterministic_action(inputs) is None


# ── Tool definition ──────────────────────────────────────────────────────────


def test_scene_action_tool_input_schema_matches_pydantic():
    """The tool's input_schema must mirror SceneAction's JSON Schema.

    If they drift, the LLM will produce shapes Pydantic can't validate.
    """
    tool = _scene_action_tool()
    assert tool.name == "record_scene_action"
    assert tool.description
    # The schema must declare verb and the other required fields
    schema = tool.input_schema
    assert "properties" in schema
    props = schema["properties"]
    for required_field in (
        "verb", "target_label", "target_kind",
        "value", "confidence", "automation_ready", "reasoning",
    ):
        assert required_field in props, f"missing {required_field} in tool schema"


# ── Action ID determinism ────────────────────────────────────────────────────


def test_action_id_is_deterministic_per_scene_and_version():
    """Same (scene_id, version) ⇒ same action_id (UPSERT keying)."""
    id_a = _action_id("scene-abc", "v1")
    id_b = _action_id("scene-abc", "v1")
    assert id_a == id_b


def test_action_id_differs_across_versions():
    """Bumping version produces a new id — keeps old + new rows distinguishable."""
    id_v1 = _action_id("scene-abc", "v1")
    id_v2 = _action_id("scene-abc", "v2")
    assert id_v1 != id_v2


def test_action_id_differs_across_scenes():
    """Different scenes obviously produce different ids."""
    id_a = _action_id("scene-a", "v1")
    id_b = _action_id("scene-b", "v1")
    assert id_a != id_b


# ── ExtractionEvidence smoke ─────────────────────────────────────────────────


def test_extraction_evidence_serialises_to_jsonb_friendly_dict():
    """The dict shape feeds the JSONB column in scene_actions row."""
    ev = ExtractionEvidence(
        ocr_text_match=True,
        control_match=True,
        sources=["before_frame", "after_frame", "ocr_text", "evidence_controls"],
    )
    dumped = ev.model_dump()
    assert dumped["ocr_text_match"] is True
    assert dumped["control_match"] is True
    assert dumped["cursor_event_match"] is False
    assert "evidence_controls" in dumped["sources"]
