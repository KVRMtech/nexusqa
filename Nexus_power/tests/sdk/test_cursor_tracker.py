"""Tests for the motion-based cursor tracker.

Tests build synthetic frames as numpy arrays — no real screen recordings
required, no OpenCV optional dependency dance for the algorithm-level
checks.  When OpenCV is unavailable the suite is skipped with a clear
reason rather than failing.

Coverage:
  * Single-cursor synthetic frames produce one reading per moved frame
  * Stationary cursor across N frames yields ``is_click=True`` on the
    last frame of the run
  * Fast cursor motion yields ``is_drag=True`` for the moving frames
  * Scene change (many simultaneous components) yields no cursor reading
  * Identical consecutive frames yield no events
  * Tracker config knobs (diff threshold, dwell frames, etc.) propagate
"""
from __future__ import annotations

import pytest

cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")

from nexus_sdk.cursor import (
    CursorEvent,
    CursorTracker,
    CursorTrackerConfig,
    FrameInput,
)


# ─── Frame builders ──────────────────────────────────────────────────────────

def _blank_frame(h: int = 360, w: int = 640) -> "np.ndarray":
    """Return a uniform grey frame to use as the static background."""
    return np.full((h, w, 3), 200, dtype=np.uint8)


def _frame_with_cursor(
    cursor_xy: tuple[int, int],
    *,
    h: int = 360,
    w: int = 640,
    cursor_size: int = 12,
) -> "np.ndarray":
    """Render a cursor (a small dark square) at ``cursor_xy`` on a grey background."""
    frame = _blank_frame(h, w)
    cx, cy = cursor_xy
    half = cursor_size // 2
    y1, y2 = max(0, cy - half), min(h, cy + half)
    x1, x2 = max(0, cx - half), min(w, cx + half)
    frame[y1:y2, x1:x2] = (10, 10, 10)
    return frame


def _frame_with_scene_change(h: int = 360, w: int = 640, *, seed: int) -> "np.ndarray":
    """Return a noisy frame with many random differences from baseline.

    Used to simulate a scene change: many simultaneous changes should
    cause the tracker to skip the frame pair entirely.
    """
    rng = np.random.default_rng(seed)
    frame = _blank_frame(h, w)
    for _ in range(60):
        cy = int(rng.integers(10, h - 10))
        cx = int(rng.integers(10, w - 10))
        size = int(rng.integers(5, 15))
        frame[cy:cy + size, cx:cx + size] = rng.integers(0, 256, size=3)
    return frame


def _input(idx: int, image: "np.ndarray", *, ts_step_ms: int = 100) -> FrameInput:
    return FrameInput(
        frame_id=f"f{idx:04d}",
        frame_index=idx,
        timestamp_ms=idx * ts_step_ms,
        image_bgr=image,
    )


# ─── Tests ───────────────────────────────────────────────────────────────────

def test_single_moved_cursor_produces_one_event():
    tracker = CursorTracker()
    frames = [
        _input(0, _frame_with_cursor((100, 100))),
        _input(1, _frame_with_cursor((150, 120))),
    ]
    events = tracker.track_frames(frames)
    assert len(events) == 1
    e = events[0]
    # Cursor should be detected near the new position.  The detector
    # picks the connected component centroid of the *changed* pixels,
    # which spans the cursor's old and new positions; we tolerate a few
    # px of drift around either pose.
    assert 80 <= e.cursor_x <= 170
    assert 80 <= e.cursor_y <= 140
    assert e.detection_method == "motion"
    assert e.confidence > 0


def test_two_consecutive_motions_track_cursor():
    tracker = CursorTracker()
    frames = [
        _input(0, _frame_with_cursor((100, 100))),
        _input(1, _frame_with_cursor((150, 100))),
        _input(2, _frame_with_cursor((200, 100))),
    ]
    events = tracker.track_frames(frames)
    assert len(events) == 2
    # Velocity in second motion should be > 0
    assert events[0].velocity >= 0.0
    assert events[1].velocity > 0.0


def test_stationary_cursor_yields_click():
    tracker = CursorTracker(CursorTrackerConfig(click_dwell_frames=2))
    frames = [
        _input(0, _frame_with_cursor((100, 100))),
        _input(1, _frame_with_cursor((140, 100))),    # moved
        _input(2, _frame_with_cursor((140, 100))),    # held
        _input(3, _frame_with_cursor((140, 100))),    # held
        _input(4, _frame_with_cursor((180, 120))),    # moved away
    ]
    events = tracker.track_frames(frames)
    # At least one of the stationary frames should be marked is_click.
    assert any(e.is_click for e in events), [e for e in events]


def test_identical_frames_produce_no_events():
    tracker = CursorTracker()
    frame = _frame_with_cursor((100, 100))
    frames = [_input(0, frame), _input(1, frame), _input(2, frame)]
    events = tracker.track_frames(frames)
    assert events == []


def test_scene_change_does_not_yield_cursor_event():
    """Lots of simultaneous changes ⇒ the detector refuses to emit a
    cursor reading.  Ambiguous cursor positions are worse than no event."""
    tracker = CursorTracker()
    frames = [
        _input(0, _blank_frame()),
        _input(1, _frame_with_scene_change(seed=1)),
        _input(2, _frame_with_scene_change(seed=2)),
    ]
    events = tracker.track_frames(frames)
    # Detector should refuse to lock onto a cursor under heavy change.
    # At most one event may slip through if the random scene is sparse;
    # we treat ≤1 event as acceptable.
    assert len(events) <= 1


def test_too_few_frames_returns_empty():
    tracker = CursorTracker()
    assert tracker.track_frames([]) == []
    assert tracker.track_frames([_input(0, _blank_frame())]) == []


def test_drag_flag_set_for_fast_motion():
    """At 100 ms/frame, moving 200 px = 2000 px/s, well above the
    drag threshold (200 px/s default)."""
    tracker = CursorTracker(CursorTrackerConfig(drag_min_velocity_px_per_s=200.0))
    frames = [
        _input(0, _frame_with_cursor((50, 50))),
        _input(1, _frame_with_cursor((250, 50))),
        _input(2, _frame_with_cursor((450, 50))),
    ]
    events = tracker.track_frames(frames)
    assert events
    # Second event has the velocity comparison and should be flagged.
    assert events[-1].is_drag is True


def test_config_diff_threshold_propagates():
    """A high diff_threshold rejects subtle motion that the default would catch."""
    tight = CursorTracker(CursorTrackerConfig(diff_threshold=200))
    frames = [
        _input(0, _frame_with_cursor((100, 100))),
        _input(1, _frame_with_cursor((150, 100))),
    ]
    # The cursor square has BGR (10,10,10) on a (200,200,200) bg, so the
    # absdiff at the cursor is 190 — below threshold 200.  Should yield
    # no detection at this aggressive setting.
    assert tight.track_frames(frames) == []


def test_event_metadata_includes_area_and_component_count():
    tracker = CursorTracker()
    frames = [
        _input(0, _frame_with_cursor((100, 100))),
        _input(1, _frame_with_cursor((150, 100))),
    ]
    events = tracker.track_frames(frames)
    assert events
    md = events[0].metadata
    assert "area_px" in md
    assert "components" in md
    assert md["area_px"] > 0


def test_stationary_distance_tolerance_handles_subpixel_jitter():
    """Sub-pixel jitter (1-3 px) within stationary_distance_px must still
    classify as stationary so OS rendering noise doesn't suppress click
    detection."""
    tracker = CursorTracker(CursorTrackerConfig(
        click_dwell_frames=2, stationary_distance_px=5.0,
    ))
    frames = [
        _input(0, _frame_with_cursor((100, 100))),
        _input(1, _frame_with_cursor((140, 100))),    # moved
        _input(2, _frame_with_cursor((141, 100))),    # held (1 px jitter)
        _input(3, _frame_with_cursor((140, 101))),    # held (1 px jitter)
        _input(4, _frame_with_cursor((180, 120))),    # moved away
    ]
    events = tracker.track_frames(frames)
    assert any(e.is_click for e in events), [
        (e.frame_index, e.is_click, e.velocity, e.cursor_x, e.cursor_y) for e in events
    ]
