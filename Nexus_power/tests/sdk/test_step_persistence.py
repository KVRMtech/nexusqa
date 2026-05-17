"""Tests for the evidence_steps + cursor_events persistence helpers."""
from __future__ import annotations

import uuid

import pytest

from nexus_sdk.cursor import CursorEvent
from nexus_sdk.evidence.step_persistence import (
    cursor_events_to_db_rows,
    triangulated_actions_to_step_rows,
)
from nexus_sdk.evidence.triangulator import TriangulatedAction


# ─── evidence_steps row construction ─────────────────────────────────────────

def test_triangulated_actions_to_step_rows_populates_all_columns():
    action = TriangulatedAction(
        step_index=0,
        action_kind="click_cta",
        target_label="Submit",
        observed_value="Submit",
        confidence=0.85,
        agreement_score=0.75,
        evidence_signals=["audio_intent", "cursor"],
        before_frame_id="f0001",
        after_frame_id="f0002",
        start_ms=1000,
        end_ms=2000,
        cursor_x=420,
        cursor_y=240,
        audio_intent_text="Now I'll click Submit.",
        audio_intent_ts_ms=1500,
        trigger_control_id="ctrl-7",
        metadata={"step_id": "step-uuid-1", "extra": "value"},
    )
    rows = triangulated_actions_to_step_rows(
        actions=[action],
        artifact_id="art-1",
        scene_id="scene-1",
        tenant_id="t-1",
        session_id="sess-1",
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["step_id"] == "step-uuid-1"
    assert row["artifact_id"] == "art-1"
    assert row["scene_id"] == "scene-1"
    assert row["tenant_id"] == "t-1"
    assert row["session_id"] == "sess-1"
    assert row["step_index"] == 0
    assert row["action_kind"] == "click_cta"
    assert row["target_label"] == "Submit"
    assert row["observed_value"] == "Submit"
    assert row["trigger_control_id"] == "ctrl-7"
    assert row["before_frame_id"] == "f0001"
    assert row["after_frame_id"] == "f0002"
    assert row["start_ms"] == 1000
    assert row["end_ms"] == 2000
    assert row["confidence"] == pytest.approx(0.85)
    assert row["agreement_score"] == pytest.approx(0.75)
    assert row["evidence_signals"] == ["audio_intent", "cursor"]
    assert row["cursor_x"] == 420
    assert row["cursor_y"] == 240
    assert row["audio_intent_text"] == "Now I'll click Submit."
    assert row["audio_intent_ts_ms"] == 1500
    # step_id should NOT be duplicated into metadata_json
    assert "step_id" not in row["metadata_json"]
    assert row["metadata_json"]["extra"] == "value"


def test_triangulated_actions_with_no_step_id_generates_one():
    action = TriangulatedAction(
        step_index=0, action_kind="enter_text",
        target_label="", observed_value="1990",
        confidence=0.5, agreement_score=0.25,
        before_frame_id="a", after_frame_id="b",
        metadata={},
    )
    rows = triangulated_actions_to_step_rows(
        actions=[action], artifact_id="art-1", scene_id="s",
        tenant_id="t", session_id="s",
    )
    # uuid_generated is a valid UUIDv4
    uuid.UUID(rows[0]["step_id"])


def test_triangulated_actions_truncate_long_target_and_value():
    """Defensive truncation matches the column max sizes (500/2000)."""
    action = TriangulatedAction(
        step_index=0, action_kind="click_cta",
        target_label="X" * 800, observed_value="Y" * 5000,
        confidence=0.5, agreement_score=0.25,
        before_frame_id="a", after_frame_id="b",
    )
    rows = triangulated_actions_to_step_rows(
        actions=[action], artifact_id="art-1", scene_id="s",
        tenant_id="t", session_id="s",
    )
    assert len(rows[0]["target_label"]) == 500
    assert len(rows[0]["observed_value"]) == 2000


def test_triangulated_actions_handle_optional_fields_absent():
    """Missing optional fields stay None in the DB row dict."""
    action = TriangulatedAction(
        step_index=0, action_kind="ui_interaction",
        target_label="", observed_value="",
        confidence=0.4, agreement_score=0.5,
    )
    rows = triangulated_actions_to_step_rows(
        actions=[action], artifact_id="art-1", scene_id="s",
        tenant_id="t", session_id="s",
    )
    row = rows[0]
    assert row["cursor_x"] is None
    assert row["cursor_y"] is None
    assert row["audio_intent_ts_ms"] is None
    assert row["trigger_control_id"] is None
    assert row["before_frame_id"] is None
    assert row["after_frame_id"] is None


# ─── cursor_events row construction ─────────────────────────────────────────

def test_cursor_events_to_db_rows_from_dataclass():
    event = CursorEvent(
        event_id="evt-1",
        frame_id="f1",
        frame_index=5,
        timestamp_ms=2500,
        cursor_x=100, cursor_y=200,
        velocity=120.5,
        is_click=True, is_drag=False,
        detection_method="motion",
        confidence=0.9,
        metadata={"area_px": 32},
    )
    rows = cursor_events_to_db_rows(
        events=[event], artifact_id="art-1", tenant_id="t-1", session_id="sess-1",
    )
    row = rows[0]
    assert row["event_id"] == "evt-1"
    assert row["frame_id"] == "f1"
    assert row["artifact_id"] == "art-1"
    assert row["tenant_id"] == "t-1"
    assert row["session_id"] == "sess-1"
    assert row["frame_index"] == 5
    assert row["timestamp_ms"] == 2500
    assert row["cursor_x"] == 100
    assert row["cursor_y"] == 200
    assert row["velocity"] == pytest.approx(120.5)
    assert row["is_click"] is True
    assert row["is_drag"] is False
    assert row["detection_method"] == "motion"
    assert row["confidence"] == pytest.approx(0.9)
    assert row["metadata_json"] == {"area_px": 32}


def test_cursor_events_resolve_frame_id_via_lookup():
    """When CursorEvent.frame_id is empty the helper falls back to the
    lookup table — needed when persisting from a tracker run that only
    knew frame_index."""
    event = CursorEvent(
        event_id="evt-1",
        frame_id="",  # empty
        frame_index=3,
        timestamp_ms=1500,
        cursor_x=50, cursor_y=50,
        velocity=10.0,
    )
    rows = cursor_events_to_db_rows(
        events=[event],
        artifact_id="art-1", tenant_id="t-1", session_id="sess-1",
        frame_id_lookup={3: "frame-uuid-3"},
    )
    assert rows[0]["frame_id"] == "frame-uuid-3"


def test_cursor_events_to_db_rows_from_dict_input():
    """Plain dicts (e.g. from JSON-serialised tracker output) work too."""
    rows = cursor_events_to_db_rows(
        events=[{
            "frame_id": "f9",
            "frame_index": 9,
            "timestamp_ms": 4500,
            "cursor_x": 300, "cursor_y": 400,
            "velocity": 0.0,
            "is_click": True,
        }],
        artifact_id="art-1", tenant_id="t-1", session_id="sess-1",
    )
    row = rows[0]
    assert row["frame_id"] == "f9"
    assert row["is_click"] is True
    # event_id must be a valid UUID when not supplied
    uuid.UUID(row["event_id"])


def test_cursor_events_default_detection_method():
    """If detection_method missing, default to ``motion``."""
    rows = cursor_events_to_db_rows(
        events=[{
            "frame_index": 0, "timestamp_ms": 0,
            "cursor_x": 0, "cursor_y": 0,
        }],
        artifact_id="a", tenant_id="t", session_id="s",
    )
    assert rows[0]["detection_method"] == "motion"
    assert rows[0]["confidence"] == 0.0
    assert rows[0]["is_click"] is False


def test_cursor_events_to_db_rows_invalid_input_raises():
    with pytest.raises(TypeError):
        cursor_events_to_db_rows(
            events=[42],  # type: ignore[list-item]
            artifact_id="a", tenant_id="t", session_id="s",
        )


# ─── Test-case step generation (Phase C.3) ───────────────────────────────────

from nexus_sdk.evidence.step_persistence import evidence_steps_to_test_case_step_rows


def _ev_step(
    *,
    action_kind: str,
    target_label: str = "",
    observed_value: str = "",
    confidence: float = 0.7,
    agreement_score: float = 0.5,
    signals: list[str] | None = None,
    scene_id: str = "scene-1",
    trigger_control_id: str | None = None,
    audio_intent_text: str = "",
    step_id: str | None = None,
) -> dict:
    return {
        "step_id": step_id or str(uuid.uuid4()),
        "scene_id": scene_id,
        "trigger_control_id": trigger_control_id,
        "action_kind": action_kind,
        "target_label": target_label,
        "observed_value": observed_value,
        "confidence": confidence,
        "agreement_score": agreement_score,
        "evidence_signals": signals or [],
        "audio_intent_text": audio_intent_text,
    }


def test_evidence_to_test_case_steps_click():
    rows = evidence_steps_to_test_case_step_rows(
        evidence_steps=[_ev_step(
            action_kind="click_cta", target_label="Submit",
            trigger_control_id="ctrl-1",
        )],
        test_case_id="tc-1",
    )
    row = rows[0]
    assert row["test_case_id"] == "tc-1"
    assert row["step_number"] == 1
    assert row["action"] == "Click on Submit"
    assert row["target_element"] == "Submit"
    assert row["evidence_scene_id"] == "scene-1"
    assert row["evidence_control_id"] == "ctrl-1"


def test_evidence_to_test_case_steps_enter_text():
    rows = evidence_steps_to_test_case_step_rows(
        evidence_steps=[_ev_step(
            action_kind="enter_text", target_label="Year",
            observed_value="1990",
        )],
        test_case_id="tc-1",
    )
    assert rows[0]["action"] == "Enter '1990' in Year"
    assert "1990" in rows[0]["expected_result"]
    assert "1990" in rows[0]["verification"]


def test_evidence_to_test_case_steps_select():
    rows = evidence_steps_to_test_case_step_rows(
        evidence_steps=[_ev_step(
            action_kind="select_option", target_label="Nicotine",
            observed_value="No",
        )],
        test_case_id="tc-1",
    )
    assert rows[0]["action"] == "Select 'No' from Nicotine"


def test_evidence_to_test_case_steps_neutral_target_when_missing():
    """Empty target_label must NOT fabricate a button name — use 'the UI'."""
    rows = evidence_steps_to_test_case_step_rows(
        evidence_steps=[_ev_step(action_kind="click_cta", target_label="")],
        test_case_id="tc-1",
    )
    assert "the UI" in rows[0]["action"]


def test_evidence_to_test_case_steps_step_numbers_sequential():
    rows = evidence_steps_to_test_case_step_rows(
        evidence_steps=[
            _ev_step(action_kind="click_cta", target_label="A"),
            _ev_step(action_kind="enter_text", target_label="B", observed_value="x"),
            _ev_step(action_kind="submit_form"),
        ],
        test_case_id="tc-1",
        starting_step_number=10,
    )
    assert [r["step_number"] for r in rows] == [10, 11, 12]


def test_evidence_to_test_case_steps_metadata_links_back_to_evidence_step():
    """Generated test_case_step must carry the source evidence_step_id
    so reviewers can drill from a generated test back to the proof."""
    rows = evidence_steps_to_test_case_step_rows(
        evidence_steps=[_ev_step(
            action_kind="click_cta", target_label="Save",
            step_id="ev-uuid-99",
            signals=["audio_intent", "cursor"],
            agreement_score=0.5,
            audio_intent_text="Now I'll click Save.",
        )],
        test_case_id="tc-1",
    )
    md = rows[0]["metadata_json"]
    assert md["evidence_step_id"] == "ev-uuid-99"
    assert md["evidence_signals"] == ["audio_intent", "cursor"]
    assert md["audio_intent_text"] == "Now I'll click Save."
    assert rows[0]["evidence_mode"] == "multimodal"


def test_evidence_mode_single_signal_audio():
    rows = evidence_steps_to_test_case_step_rows(
        evidence_steps=[_ev_step(action_kind="click_cta", target_label="X",
                                 signals=["audio_intent"])],
        test_case_id="tc-1",
    )
    assert rows[0]["evidence_mode"] == "audio"


def test_evidence_to_test_case_steps_screenshot_flag_for_review():
    """Review and submit steps default to screenshot_required=True so
    reviewers/CI capture before/after evidence."""
    rows = evidence_steps_to_test_case_step_rows(
        evidence_steps=[
            _ev_step(action_kind="review", target_label="Estimate page"),
            _ev_step(action_kind="submit_form"),
            _ev_step(action_kind="click_cta", target_label="Continue"),
        ],
        test_case_id="tc-1",
    )
    assert rows[0]["screenshot_required"] is True
    assert rows[1]["screenshot_required"] is True
    assert rows[2]["screenshot_required"] is False
