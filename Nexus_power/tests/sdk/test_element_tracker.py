"""Tests for the cross-frame UI element tracker."""
from __future__ import annotations

import pytest

from nexus_sdk.elements import ElementTracker
from nexus_sdk.elements.tracker import ElementTrackerConfig


def _el(
    element_type: str,
    text: str,
    *,
    cx: int,
    cy: int,
    width: int = 60,
    height: int = 30,
) -> dict:
    return {
        "element_type": element_type,
        "text": text,
        "confidence": 0.9,
        "properties": {},
        "bbox": {
            "x": cx - width // 2,
            "y": cy - height // 2,
            "width": width,
            "height": height,
        },
    }


def test_same_label_same_position_keeps_entity_across_frames():
    tracker = ElementTracker()
    f1 = tracker.assign_entities([_el("button", "Submit", cx=300, cy=400)], frame_index=0)
    f2 = tracker.assign_entities([_el("button", "Submit", cx=302, cy=399)], frame_index=1)
    f3 = tracker.assign_entities([_el("button", "Submit", cx=301, cy=400)], frame_index=2)
    assert f1[0]["entity_id"] == f2[0]["entity_id"] == f3[0]["entity_id"]
    assert f3[0]["persistence_count"] == 3
    assert f3[0]["first_frame_index"] == 0


def test_different_label_yields_different_entity():
    tracker = ElementTracker()
    f1 = tracker.assign_entities([_el("button", "Submit", cx=300, cy=400)], frame_index=0)
    f2 = tracker.assign_entities([_el("button", "Cancel", cx=300, cy=400)], frame_index=1)
    assert f1[0]["entity_id"] != f2[0]["entity_id"]


def test_label_change_in_place_falls_back_to_spatial_match():
    """Cursor hover momentarily clears the OCR label — same element_type
    + same position should still match via spatial pass."""
    cfg = ElementTrackerConfig(spatial_match_radius_px=20)
    tracker = ElementTracker(cfg)
    f1 = tracker.assign_entities([_el("button", "Submit", cx=300, cy=400)], frame_index=0)
    f2 = tracker.assign_entities([_el("button", "", cx=300, cy=400)], frame_index=1)
    # Empty label → spatial pass should still match.
    assert f1[0]["entity_id"] == f2[0]["entity_id"]
    assert f2[0]["persistence_count"] == 2


def test_far_movement_yields_new_entity_even_with_same_label():
    """A button moving 300px is a different control (or an OCR mismatch)."""
    cfg = ElementTrackerConfig(label_match_radius_px=80)
    tracker = ElementTracker(cfg)
    f1 = tracker.assign_entities([_el("button", "Save", cx=100, cy=100)], frame_index=0)
    f2 = tracker.assign_entities([_el("button", "Save", cx=600, cy=100)], frame_index=1)
    assert f1[0]["entity_id"] != f2[0]["entity_id"]


def test_compatible_element_types_alias_match():
    """``input`` and ``text_field`` should be treated as the same type."""
    tracker = ElementTracker()
    f1 = tracker.assign_entities([_el("input", "Year", cx=200, cy=300)], frame_index=0)
    f2 = tracker.assign_entities([_el("text_field", "Year", cx=200, cy=300)], frame_index=1)
    assert f1[0]["entity_id"] == f2[0]["entity_id"]


def test_entity_pruned_after_forget_window():
    cfg = ElementTrackerConfig(forget_after_frames=2)
    tracker = ElementTracker(cfg)
    f0 = tracker.assign_entities([_el("button", "X", cx=10, cy=10)], frame_index=0)
    # No matching element for several frames
    tracker.assign_entities([_el("link", "Y", cx=900, cy=900)], frame_index=1)
    tracker.assign_entities([_el("link", "Y", cx=900, cy=900)], frame_index=2)
    tracker.assign_entities([_el("link", "Y", cx=900, cy=900)], frame_index=3)
    f4 = tracker.assign_entities([_el("button", "X", cx=10, cy=10)], frame_index=4)
    # Original entity expired → fresh entity_id
    assert f0[0]["entity_id"] != f4[0]["entity_id"]


def test_multiple_elements_per_frame_each_get_own_entity():
    tracker = ElementTracker()
    elements = [
        _el("button", "Save", cx=100, cy=400),
        _el("button", "Cancel", cx=200, cy=400),
        _el("input", "Email", cx=300, cy=200),
    ]
    out = tracker.assign_entities(elements, frame_index=0)
    ids = [e["entity_id"] for e in out]
    assert len(set(ids)) == 3


def test_two_buttons_with_same_label_resolved_by_position():
    """Two Save buttons in different scene regions stay distinct entities."""
    tracker = ElementTracker()
    f1 = tracker.assign_entities([
        _el("button", "Save", cx=100, cy=400),
        _el("button", "Save", cx=900, cy=400),
    ], frame_index=0)
    f2 = tracker.assign_entities([
        _el("button", "Save", cx=98, cy=402),    # left button moved 2px
        _el("button", "Save", cx=905, cy=405),   # right button moved 5px
    ], frame_index=1)
    assert f1[0]["entity_id"] == f2[0]["entity_id"]   # left matches left
    assert f1[1]["entity_id"] == f2[1]["entity_id"]   # right matches right
    assert f1[0]["entity_id"] != f1[1]["entity_id"]   # not collapsed


def test_element_without_bbox_or_label_yields_fresh_entity():
    """Untrackable entries still get a unique entity_id, never None."""
    tracker = ElementTracker()
    out = tracker.assign_entities([{"element_type": "image"}], frame_index=0)
    assert out[0]["entity_id"]
    assert out[0]["persistence_count"] == 1


def test_input_does_not_match_button_even_at_same_position():
    """element_type differences across non-aliased types prevent a match."""
    tracker = ElementTracker()
    f1 = tracker.assign_entities([_el("button", "Go", cx=200, cy=300)], frame_index=0)
    f2 = tracker.assign_entities([_el("input", "Go", cx=200, cy=300)], frame_index=1)
    assert f1[0]["entity_id"] != f2[0]["entity_id"]


def test_bbox_as_x1y1x2y2_dict_supported():
    """Some upstream emitters use {x1,y1,x2,y2} instead of {x,y,width,height}."""
    tracker = ElementTracker()
    el = {
        "element_type": "button",
        "text": "OK",
        "bbox": {"x1": 100, "y1": 200, "x2": 200, "y2": 230},
    }
    out = tracker.assign_entities([el], frame_index=0)
    assert out[0]["entity_id"]


def test_bbox_as_list_supported():
    tracker = ElementTracker()
    el = {
        "element_type": "button",
        "text": "OK",
        "bbox": [100, 200, 200, 230],
    }
    out = tracker.assign_entities([el], frame_index=0)
    # Followup frame with same coords matches → persistence_count == 2
    out2 = tracker.assign_entities([
        {"element_type": "button", "text": "OK", "bbox": [100, 200, 200, 230]},
    ], frame_index=1)
    assert out[0]["entity_id"] == out2[0]["entity_id"]


def test_caller_input_not_mutated():
    """The tracker must not modify the dict the caller passed in."""
    tracker = ElementTracker()
    original = _el("button", "Save", cx=100, cy=100)
    snapshot = dict(original)
    tracker.assign_entities([original], frame_index=0)
    assert original == snapshot


def test_persistence_count_growth_across_long_run():
    tracker = ElementTracker()
    el = _el("button", "Continue", cx=300, cy=400)
    for i in range(10):
        out = tracker.assign_entities([el], frame_index=i)
    assert out[0]["persistence_count"] == 10
    assert out[0]["first_frame_index"] == 0
    assert out[0]["last_frame_index"] == 9
