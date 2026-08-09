"""U3 — gesture verification read-backs (the crux: proven vs unverifiable). Pure."""
from __future__ import annotations

from app.gesture_verify import (
    GESTURE_DRAG,
    GESTURE_DRAW,
    GESTURE_SLIDER,
    drag_registered,
    draw_registered,
    slider_registered,
    verify_gesture,
)


def test_drag_proven_on_reorder_refuted_on_no_change_none_on_membership_change():
    assert drag_registered(["a", "b", "c"], ["b", "a", "c"]) is True     # reordered
    assert drag_registered(["a", "b", "c"], ["a", "b", "c"]) is False    # unchanged
    assert drag_registered(["a", "b"], ["a", "b", "c"]) is None          # membership changed
    assert drag_registered([], ["a"]) is None                            # unreadable


def test_draw_proven_when_canvas_goes_from_empty_to_inked():
    assert draw_registered(False, True) is True          # empty → inked
    assert draw_registered("empty", "hash-abc") is True  # hash changed + truthy
    assert draw_registered(True, True) is False          # already inked, no change
    assert draw_registered(True, False) is False         # nothing drawn now
    assert draw_registered(None, True) is None           # unknown before


def test_slider_proven_when_valuenow_moves_toward_target():
    assert slider_registered("10", "40", target="50") is True   # moved toward
    assert slider_registered("10", "10") is False               # unchanged
    assert slider_registered("10", "40") is True                # moved (no target)
    assert slider_registered("50", "10", target="60") is False  # moved AWAY from target
    assert slider_registered("x", "y") is None                  # unreadable


def test_verify_gesture_dispatch_and_unknown_kind_is_none():
    assert verify_gesture(GESTURE_DRAG, ["a", "b"], ["b", "a"]) is True
    assert verify_gesture(GESTURE_DRAW, False, True) is True
    assert verify_gesture(GESTURE_SLIDER, "1", "2") is True
    assert verify_gesture("mystery", 1, 2) is None    # unknown → unverifiable, never a false proven
