"""Tests for the triangulated action classifier.

Verify that combining multiple weak signals produces high-confidence,
high-agreement action records, and that sparse single-signal cases
either degrade gracefully (low confidence) or refuse to fabricate
(generic ui_interaction without corroboration → no row).
"""
from __future__ import annotations

import pytest

from nexus_sdk.evidence.triangulator import (
    TriangulatedAction,
    TriangulatedClassifier,
    TriangulatorConfig,
)


# ─── Frame helpers ───────────────────────────────────────────────────────────

def _frame(idx: int, *, ocr: str = "", desc: str = "", ts_ms: int = 0) -> dict:
    return {
        "frame_id": f"f{idx:04d}",
        "frame_index": idx,
        "timestamp_ms": ts_ms or idx * 1000,
        "extracted_text": ocr,
        "description": desc,
        "ui_elements": [],
    }


def _scene(scene_id: str = "s1") -> dict:
    return {
        "scene_id": scene_id,
        "scene_index": 0,
        "screen_name": "test scene",
    }


def _cursor_event(
    frame_index: int,
    *,
    cursor_x: int = 100,
    cursor_y: int = 100,
    is_click: bool = False,
    confidence: float = 0.9,
    label: str = "",
    control_id: str = "",
) -> dict:
    return {
        "frame_index": frame_index,
        "timestamp_ms": frame_index * 1000,
        "cursor_x": cursor_x,
        "cursor_y": cursor_y,
        "is_click": is_click,
        "confidence": confidence,
        "nearest_control_label": label,
        "nearest_control_id": control_id,
    }


def _intent(
    *,
    timestamp_ms: int,
    intent_kind: str = "click_cta",
    target_phrase: str = "Submit",
    raw_text: str = "Now I'll click Submit.",
    confidence: float = 0.9,
) -> dict:
    return {
        "timestamp_ms": timestamp_ms,
        "intent_kind": intent_kind,
        "target_phrase": target_phrase,
        "raw_text": raw_text,
        "confidence": confidence,
    }


# ─── Single-signal tests ─────────────────────────────────────────────────────

def test_no_signals_produces_no_actions():
    """Two frames with identical OCR + identical descriptions emit nothing."""
    classifier = TriangulatedClassifier()
    actions = classifier.classify_actions_in_scene(
        _scene(),
        scene_frames=[_frame(0, ocr="x"), _frame(1, ocr="x")],
    )
    assert actions == []


def test_single_frame_scene_produces_no_actions():
    classifier = TriangulatedClassifier()
    actions = classifier.classify_actions_in_scene(
        _scene(), scene_frames=[_frame(0, ocr="x")],
    )
    assert actions == []


def test_ocr_only_signal_emits_action_with_low_agreement():
    classifier = TriangulatedClassifier()
    actions = classifier.classify_actions_in_scene(
        _scene(),
        scene_frames=[
            _frame(0, ocr="Birthdate Continue"),
            _frame(1, ocr="Birthdate 1990 Continue"),
        ],
    )
    assert len(actions) == 1
    a = actions[0]
    assert a.action_kind == "enter_text"
    assert "1990" in a.observed_value
    assert "ocr_diff" in a.evidence_signals
    assert a.agreement_score == pytest.approx(0.25, abs=0.01)


def test_audio_alone_drives_action_kind_when_no_visual_signal():
    """Audio gives a verb even when OCR/cursor/LLaVA are silent."""
    classifier = TriangulatedClassifier()
    actions = classifier.classify_actions_in_scene(
        _scene(),
        scene_frames=[
            _frame(0, ocr="same", desc="same desc", ts_ms=0),
            _frame(1, ocr="same", desc="same desc", ts_ms=1500),
        ],
        audio_intents=[_intent(timestamp_ms=750, intent_kind="click_cta", target_phrase="Submit")],
    )
    # Only audio fired; classifier should still emit because audio is a
    # high-quality verb signal and ui_interaction-not-corroborated rule
    # only blocks the pure-fabrication case (no signals at all).
    assert len(actions) == 1
    a = actions[0]
    assert a.action_kind == "click_cta"
    assert a.target_label == "Submit"
    assert a.audio_intent_text.startswith("Now I'll click")


def test_generic_ui_interaction_requires_corroboration():
    """LLaVA-only signal that does not point to a specific kind must
    NOT fabricate a generic ui_interaction record — that's the whole
    point of the corroboration rule."""
    classifier = TriangulatedClassifier()
    actions = classifier.classify_actions_in_scene(
        _scene(),
        scene_frames=[
            _frame(0, ocr="x", desc="A page with a form"),
            _frame(1, ocr="x", desc="A different page with a button"),
        ],
    )
    # LLaVA delta is the only signal, action_kind would be ui_interaction
    # — corroboration rule should suppress it.
    assert actions == []


# ─── Multi-signal triangulation ──────────────────────────────────────────────

def test_audio_plus_cursor_click_promotes_confidence():
    """Audio says 'click Submit' and cursor click fires near the button —
    triangulation should produce a click_cta with two signals and ≥0.5
    confidence."""
    classifier = TriangulatedClassifier()
    actions = classifier.classify_actions_in_scene(
        _scene(),
        scene_frames=[
            _frame(0, ocr="form", ts_ms=0),
            _frame(1, ocr="form", ts_ms=2000),
        ],
        cursor_events=[
            _cursor_event(1, is_click=True, confidence=0.9, label="Submit", control_id="c1"),
        ],
        audio_intents=[
            _intent(timestamp_ms=1500, intent_kind="click_cta", target_phrase="Submit"),
        ],
    )
    assert len(actions) == 1
    a = actions[0]
    assert a.action_kind == "click_cta"
    assert a.target_label == "Submit"
    assert a.cursor_x == 100 and a.cursor_y == 100
    assert a.trigger_control_id == "c1"
    assert {"audio_intent", "cursor"}.issubset(set(a.evidence_signals))
    assert a.agreement_score >= 0.5
    assert a.confidence >= 0.5


def test_all_four_signals_yields_max_agreement():
    classifier = TriangulatedClassifier()
    actions = classifier.classify_actions_in_scene(
        _scene(),
        scene_frames=[
            _frame(0, ocr="form", desc="Empty year field; Continue button visible.", ts_ms=0),
            _frame(1, ocr="form 1990 Continue",
                   desc="Year field shows 1990; Continue button enabled.", ts_ms=2000),
        ],
        cursor_events=[
            _cursor_event(1, is_click=False, confidence=0.9, label="Year input"),
        ],
        audio_intents=[
            _intent(timestamp_ms=1500, intent_kind="enter_text", target_phrase="year",
                    raw_text="Now I'll enter the year"),
        ],
    )
    assert len(actions) == 1
    a = actions[0]
    assert a.action_kind == "enter_text"
    assert "1990" in a.observed_value
    assert set(a.evidence_signals) >= {"audio_intent", "cursor", "ocr_diff", "llava_delta"}
    assert a.agreement_score == 1.0
    assert a.confidence >= 0.7


def test_audio_drives_action_kind_when_visual_implies_different():
    """OCR diff might suggest 'enter_text' (small token added) but audio
    says 'select_option'.  Audio wins for the action_kind."""
    classifier = TriangulatedClassifier()
    actions = classifier.classify_actions_in_scene(
        _scene(),
        scene_frames=[
            _frame(0, ocr="Nicotine Continue", ts_ms=0),
            _frame(1, ocr="Nicotine No Continue", ts_ms=2000),
        ],
        audio_intents=[
            _intent(timestamp_ms=1000, intent_kind="select_option", target_phrase="No"),
        ],
    )
    assert len(actions) == 1
    a = actions[0]
    assert a.action_kind == "select_option"
    assert a.target_label == "No"


# ─── Cursor / control linkage ────────────────────────────────────────────────

def test_cursor_click_alone_emits_click_cta():
    """A confident cursor click is sufficient to emit click_cta —
    even without audio or OCR change."""
    classifier = TriangulatedClassifier()
    actions = classifier.classify_actions_in_scene(
        _scene(),
        scene_frames=[
            _frame(0, ocr="same", ts_ms=0),
            _frame(1, ocr="same", ts_ms=1000),
        ],
        cursor_events=[
            _cursor_event(1, is_click=True, confidence=0.9, label="Submit", control_id="c1"),
        ],
    )
    assert len(actions) == 1
    a = actions[0]
    assert a.action_kind == "click_cta"
    assert a.cursor_x is not None
    assert "cursor" in a.evidence_signals


def test_low_confidence_cursor_is_dropped():
    """A cursor reading below the configured min_confidence is ignored."""
    cfg = TriangulatorConfig(cursor_min_confidence=0.5)
    classifier = TriangulatedClassifier(cfg)
    actions = classifier.classify_actions_in_scene(
        _scene(),
        scene_frames=[
            _frame(0, ocr="same"),
            _frame(1, ocr="same"),
        ],
        cursor_events=[_cursor_event(1, is_click=True, confidence=0.2)],
    )
    # No other signal fired and the cursor was below threshold.
    assert actions == []


# ─── Window alignment ────────────────────────────────────────────────────────

def test_audio_outside_window_does_not_align():
    """An intent whose timestamp is far from the frame pair must not bind."""
    cfg = TriangulatorConfig(audio_align_ms=200)
    classifier = TriangulatedClassifier(cfg)
    actions = classifier.classify_actions_in_scene(
        _scene(),
        scene_frames=[
            _frame(0, ocr="form Continue", ts_ms=0),
            _frame(1, ocr="form 1990 Continue", ts_ms=1000),
        ],
        audio_intents=[_intent(timestamp_ms=10_000)],
    )
    # OCR fired so we still emit, but with no audio signal.
    assert len(actions) == 1
    assert "audio_intent" not in actions[0].evidence_signals


def test_audio_inside_window_aligns():
    classifier = TriangulatedClassifier(TriangulatorConfig(audio_align_ms=2000))
    actions = classifier.classify_actions_in_scene(
        _scene(),
        scene_frames=[
            _frame(0, ocr="form Continue", ts_ms=0),
            _frame(1, ocr="form 1990 Continue", ts_ms=1000),
        ],
        audio_intents=[_intent(timestamp_ms=2500, intent_kind="click_cta", target_phrase="Save")],
    )
    assert len(actions) == 1
    assert actions[0].audio_intent_ts_ms == 2500
    assert actions[0].target_label == "Save"


# ─── Output shape ────────────────────────────────────────────────────────────

def test_action_record_carries_persistence_ready_fields():
    classifier = TriangulatedClassifier()
    actions = classifier.classify_actions_in_scene(
        _scene("scene-id-1"),
        scene_frames=[
            _frame(0, ocr="form", ts_ms=0),
            _frame(1, ocr="form 1990", ts_ms=2000),
        ],
    )
    a = actions[0]
    # All fields the evidence_steps row needs.
    assert a.before_frame_id == "f0000"
    assert a.after_frame_id == "f0001"
    assert a.start_ms == 0
    assert a.end_ms == 2000
    assert isinstance(a.evidence_signals, list)
    assert isinstance(a.metadata, dict)
    assert a.step_id  # uuid populated


def test_step_indexes_are_sequential():
    classifier = TriangulatedClassifier()
    actions = classifier.classify_actions_in_scene(
        _scene(),
        scene_frames=[
            _frame(0, ocr="page Continue", ts_ms=0),
            _frame(1, ocr="page Continue 1990", ts_ms=1000),
            _frame(2, ocr="page Continue 1990 male", ts_ms=2000),
            _frame(3, ocr="page Continue 1990 male nonsmoker", ts_ms=3000),
        ],
    )
    assert [a.step_index for a in actions] == [0, 1, 2]


# ─── Phase F.4 — Action provenance graph ────────────────────────────────────

def test_provenance_records_audio_source_for_action_kind():
    classifier = TriangulatedClassifier()
    actions = classifier.classify_actions_in_scene(
        _scene(),
        scene_frames=[
            _frame(0, ocr="form", ts_ms=0),
            _frame(1, ocr="form", ts_ms=2000),
        ],
        audio_intents=[
            _intent(timestamp_ms=1500, intent_kind="click_cta", target_phrase="Submit"),
        ],
    )
    prov = actions[0].metadata["provenance"]
    assert prov["action_kind"]["source"] == "audio_intent"
    assert prov["action_kind"]["confidence"] > 0.0
    assert prov["target_label"]["source"] == "audio_intent"
    assert prov["audio_anchor"]["source"] == "audio_intent"


def test_provenance_records_ocr_source_for_observed_value():
    classifier = TriangulatedClassifier()
    actions = classifier.classify_actions_in_scene(
        _scene(),
        scene_frames=[
            _frame(0, ocr="Birthdate Continue", ts_ms=0),
            _frame(1, ocr="Birthdate 1990 Continue", ts_ms=1000),
        ],
    )
    prov = actions[0].metadata["provenance"]
    assert prov["observed_value"]["source"] == "ocr_diff"
    assert prov["action_kind"]["source"] == "ocr_diff"


def test_provenance_records_cursor_source_for_xy():
    classifier = TriangulatedClassifier()
    actions = classifier.classify_actions_in_scene(
        _scene(),
        scene_frames=[
            _frame(0, ocr="form same", ts_ms=0),
            _frame(1, ocr="form same", ts_ms=1000),
        ],
        cursor_events=[
            _cursor_event(1, is_click=True, confidence=0.9, label="Submit"),
        ],
    )
    prov = actions[0].metadata["provenance"]
    assert prov["cursor_xy"]["source"] == "cursor_event"
    assert prov["target_label"]["source"] == "cursor_control"
    assert prov["action_kind"]["source"] == "cursor_click"


def test_provenance_each_field_documents_its_signal():
    """All 4 signals fire — each contributing field has a provenance entry."""
    classifier = TriangulatedClassifier()
    actions = classifier.classify_actions_in_scene(
        _scene(),
        scene_frames=[
            _frame(0, ocr="form", desc="Empty year field; Continue button visible.", ts_ms=0),
            _frame(1, ocr="form 1990 Continue",
                   desc="Year field shows 1990; Continue button enabled.", ts_ms=2000),
        ],
        cursor_events=[
            _cursor_event(1, is_click=False, confidence=0.9, label="Year input"),
        ],
        audio_intents=[
            _intent(timestamp_ms=1500, intent_kind="enter_text", target_phrase="year",
                    raw_text="Now I'll enter the year"),
        ],
    )
    prov = actions[0].metadata["provenance"]
    # Audio wins for action_kind + target_label + audio_anchor
    assert prov["action_kind"]["source"] == "audio_intent"
    assert prov["target_label"]["source"] == "audio_intent"
    assert prov["audio_anchor"]["source"] == "audio_intent"
    # OCR wins for observed_value (delta tokens are typed)
    assert prov["observed_value"]["source"] == "ocr_diff"
    # Cursor populates cursor_xy regardless of click status
    assert prov["cursor_xy"]["source"] == "cursor_event"


def test_provenance_confidence_values_in_range():
    """Every provenance.confidence must be between 0 and 1."""
    classifier = TriangulatedClassifier()
    actions = classifier.classify_actions_in_scene(
        _scene(),
        scene_frames=[
            _frame(0, ocr="page Continue", ts_ms=0),
            _frame(1, ocr="page Continue 1990 1991 1992", ts_ms=1000),
        ],
    )
    prov = actions[0].metadata["provenance"]
    for field, entry in prov.items():
        assert 0.0 <= entry["confidence"] <= 1.0, f"{field} confidence out of range: {entry}"
