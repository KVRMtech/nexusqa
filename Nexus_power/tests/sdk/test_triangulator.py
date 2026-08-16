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


def test_ocr_only_without_a_control_match_emits_nothing():
    """OCR-only deltas are REFUSED unless they resolve to a real control.

    Policy (triangulator.py): OCR on screen-capture text routinely yields
    fragments like "Get a", "senter", "fityour", "111". Emitting a row from a
    raw delta made the bottom panel meaningless to a reviewer, so an OCR-only
    pair whose target came from raw `delta_text` is now dropped. This is the
    anti-fabrication rule, not a gap — the corroborated forms are covered by
    the two tests below.
    """
    classifier = TriangulatedClassifier()
    actions = classifier.classify_actions_in_scene(
        _scene(),
        scene_frames=[
            _frame(0, ocr="Birthdate Continue"),
            _frame(1, ocr="Birthdate 1990 Continue"),
        ],
    )
    assert actions == []


def test_ocr_only_emits_when_the_delta_matches_a_known_control():
    """The one OCR-only path that survives: the control label is ground truth
    and the OCR delta only has to confirm a value appeared."""
    classifier = TriangulatedClassifier()
    actions = classifier.classify_actions_in_scene(
        _scene(),
        scene_frames=[
            _frame(0, ocr="Birthdate Continue"),
            _frame(1, ocr="Birthdate 1990 Continue"),
        ],
        controls=[{"label_text": "1990", "control_id": "dob"}],
    )
    assert len(actions) == 1
    a = actions[0]
    assert a.action_kind == "enter_text"
    assert "1990" in a.observed_value
    assert "ocr_diff" in a.evidence_signals
    assert a.agreement_score == pytest.approx(0.25, abs=0.01)


def test_audio_alone_emits_nothing_because_audio_is_not_visual_evidence():
    """Audio is DELIBERATELY ignored by the visual triangulator.

    Policy (triangulator.py::classify_actions_in_scene): narration is a
    transcript artefact. Mixing it in stamped the same SME utterance onto
    multiple evidence_steps and let audio fabricate actions the screen never
    showed. `audio_intents` is still accepted for backwards compatibility and
    ignored; the transcript still surfaces in the session transcript panel and
    the canonical artifact's safe_transcript.
    """
    classifier = TriangulatedClassifier()
    actions = classifier.classify_actions_in_scene(
        _scene(),
        scene_frames=[
            _frame(0, ocr="same", desc="same desc", ts_ms=0),
            _frame(1, ocr="same", desc="same desc", ts_ms=1500),
        ],
        audio_intents=[_intent(timestamp_ms=750, intent_kind="click_cta", target_phrase="Submit")],
    )
    assert actions == [], "audio must never be able to fabricate a visual action"


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

def test_cursor_click_carries_the_action_and_audio_adds_nothing():
    """A cursor click produces click_cta on its own; the audio intent supplied
    alongside it must NOT appear in the evidence signals (audio is ignored)."""
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
    assert set(a.evidence_signals) == {"cursor"}
    assert "audio_intent" not in a.evidence_signals
    assert a.confidence >= 0.5


def test_three_visual_signals_yield_max_agreement():
    """Agreement is still scored out of FOUR slots, so the visual ceiling is
    0.75 — audio occupies a slot it can no longer fill. Pinned deliberately:
    if audio is ever readmitted, this is the test that must be revisited."""
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
    )
    assert len(actions) == 1
    a = actions[0]
    assert "1990" in a.observed_value
    assert set(a.evidence_signals) == {"cursor", "ocr_diff", "llava_delta"}
    assert a.agreement_score == pytest.approx(0.75, abs=0.01)
    assert a.confidence >= 0.7


def test_audio_cannot_override_the_visual_action_kind():
    """The inverse of the old contract. Audio saying 'select_option' over an
    OCR-only pair used to rewrite the verb AND unlock the row; now it does
    neither, so nothing is emitted at all."""
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
    assert actions == []


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

@pytest.mark.parametrize(
    "align_ms, intent_ts",
    [
        (200, 10_000),    # far outside any window
        (2000, 2500),     # comfortably inside the window
    ],
)
def test_audio_alignment_window_has_no_effect_either_way(align_ms, intent_ts):
    """`audio_align_ms` is now inert.

    It used to decide whether a narration intent bound to a frame pair. Audio no
    longer contributes at all, so neither an aligned nor an unaligned intent can
    change the outcome: the OCR-only pair below is refused for want of a control
    match in both cases. Parametrised over the two sides of the window so a
    silent re-admission of audio fails here.
    """
    classifier = TriangulatedClassifier(TriangulatorConfig(audio_align_ms=align_ms))
    actions = classifier.classify_actions_in_scene(
        _scene(),
        scene_frames=[
            _frame(0, ocr="form Continue", ts_ms=0),
            _frame(1, ocr="form 1990 Continue", ts_ms=1000),
        ],
        audio_intents=[_intent(timestamp_ms=intent_ts, intent_kind="click_cta",
                               target_phrase="Save")],
    )
    assert actions == []


# ─── Output shape ────────────────────────────────────────────────────────────

def test_action_record_carries_persistence_ready_fields():
    classifier = TriangulatedClassifier()
    actions = classifier.classify_actions_in_scene(
        _scene("scene-id-1"),
        scene_frames=[
            _frame(0, ocr="form", ts_ms=0),
            _frame(1, ocr="form 1990", ts_ms=2000),
        ],
        # The OCR-only path emits only against a known control (anti-fabrication
        # rule); this test is about the RECORD SHAPE, so give it the control it
        # needs rather than asserting shape on an empty list.
        controls=[{"label_text": "1990", "control_id": "dob"}],
    )
    assert len(actions) == 1
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
        # One control per successive OCR delta, so all three pairs clear the
        # control-match rule and the INDEXING is what is under test.
        controls=[
            {"label_text": "1990", "control_id": "dob"},
            {"label_text": "male", "control_id": "sex"},
            {"label_text": "nonsmoker", "control_id": "smoker"},
        ],
    )
    assert [a.step_index for a in actions] == [0, 1, 2]


# ─── Phase F.4 — Action provenance graph ────────────────────────────────────

def test_provenance_never_attributes_anything_to_audio():
    """No emitted field may ever carry an `audio_intent` provenance source.

    The provenance map is what the audit UI and compliance reports render, so
    this is the strongest statement of the audio rule: not merely that audio
    cannot create a row, but that it cannot be cited as the source of a value on
    a row some other signal created.
    """
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
    prov = actions[0].metadata["provenance"]
    assert prov["action_kind"]["source"] == "cursor_click"
    assert prov["target_label"]["source"] == "cursor_control"
    assert "audio_anchor" not in prov
    assert all(entry.get("source") != "audio_intent" for entry in prov.values())


def test_provenance_records_ocr_source_for_observed_value():
    classifier = TriangulatedClassifier()
    actions = classifier.classify_actions_in_scene(
        _scene(),
        scene_frames=[
            _frame(0, ocr="Birthdate Continue", ts_ms=0),
            _frame(1, ocr="Birthdate 1990 Continue", ts_ms=1000),
        ],
        controls=[{"label_text": "1990", "control_id": "dob"}],
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
    """All three VISUAL signals fire — each contributing field is attributed."""
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
    )
    prov = actions[0].metadata["provenance"]
    # Cursor supplies the target (it names a real control) and the coordinates.
    assert prov["target_label"]["source"] == "cursor_control"
    assert prov["cursor_xy"]["source"] == "cursor_event"
    # OCR supplies the observed value (delta tokens are what got typed).
    assert prov["observed_value"]["source"] == "ocr_diff"
    # Nothing is attributed to narration.
    assert "audio_anchor" not in prov


def test_provenance_confidence_values_in_range():
    """Every provenance.confidence must be between 0 and 1."""
    classifier = TriangulatedClassifier()
    actions = classifier.classify_actions_in_scene(
        _scene(),
        scene_frames=[
            _frame(0, ocr="page Continue", ts_ms=0),
            _frame(1, ocr="page Continue 1990 1991 1992", ts_ms=1000),
        ],
        controls=[{"label_text": "1990", "control_id": "y1"}],
    )
    assert actions, "expected an emitted action to inspect provenance on"
    prov = actions[0].metadata["provenance"]
    for field, entry in prov.items():
        assert 0.0 <= entry["confidence"] <= 1.0, f"{field} confidence out of range: {entry}"
