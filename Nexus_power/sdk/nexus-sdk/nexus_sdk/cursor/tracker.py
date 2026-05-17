"""Frame-pair motion analysis for cursor localisation and click detection.

The cursor is detected as the connected component that is:

  * small (4-1024 px² depending on cursor size, DPI, theme)
  * spatially compact (bounding box width and height under
    :pyattr:`CursorTrackerConfig.max_cursor_dimension`)
  * persistent across the *surrounding* frame pairs (so we do not
    confuse a momentary loading spinner with a cursor)

The detector emits a :class:`CursorEvent` for each frame where it
confidently localised the cursor.  A click is inferred when the cursor
stays approximately stationary across at least
:pyattr:`CursorTrackerConfig.click_dwell_frames` consecutive frames —
the cleanest signal we can derive without OS-level mouse-event capture.

The implementation depends on OpenCV (``cv2``) and NumPy.  These are
already eyes-engine dependencies; we import lazily so unit tests for
this module can stub the image arrays directly without needing OpenCV
installed.
"""

from __future__ import annotations

import math
import os
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional


# ─── Public types ────────────────────────────────────────────────────────────

@dataclass
class CursorTrackerConfig:
    """Tunable thresholds for the motion detector.

    Defaults are reasonable for 1080p screen recordings of a Zoom or
    similar capture.  Operators can tune via env or by passing a
    ``CursorTrackerConfig`` directly.
    """

    # Pixel-difference threshold for binarising the absdiff image.  Higher
    # values reject more sub-pixel jitter at the cost of missing small
    # cursors.  EasyOCR background overlays trigger many sub-30 pixels of
    # change so we filter at 30 by default.
    diff_threshold: int = 30
    # Minimum/maximum connected-component area (in pixels) to accept as a
    # cursor candidate.  4 px guards against single-pixel anti-alias
    # twitches; 1024 px keeps us from picking up entire button hover
    # halos on high-DPI displays.
    min_cursor_area: int = 4
    max_cursor_area: int = 1024
    # Cursor bounding box must fit within this side length on both axes.
    max_cursor_dimension: int = 64
    # When more than this many connected components change between
    # frames, the frame pair is considered a scene change and we do not
    # emit a cursor reading — too many candidates to disambiguate.
    max_components_for_cursor_frame: int = 24
    # A stationary run of this many frames triggers a click annotation
    # on the frame just before the run ends.
    click_dwell_frames: int = 2
    # Distance (px) under which two cursor positions are considered
    # "stationary".  4 px tolerates sub-pixel anti-aliasing differences
    # without missing real cursor motion.
    stationary_distance_px: float = 4.0
    # Drag detection: when consecutive cursor positions move > this many
    # px in < click_dwell_frames frames, we mark is_drag=True.
    drag_min_velocity_px_per_s: float = 200.0


@dataclass
class FrameInput:
    """One frame to feed the tracker.

    ``image_bgr`` is the OpenCV BGR uint8 array.  ``frame_path`` is an
    optional disk path the tracker will read with ``cv2.imread`` when
    ``image_bgr`` is None.  Either must be provided.
    """

    frame_id: str
    frame_index: int
    timestamp_ms: int
    image_bgr: Optional[Any] = None
    frame_path: Optional[str] = None


@dataclass
class CursorEvent:
    """One per-frame cursor reading.

    Fields mirror the ``cursor_events`` table schema so the tracker
    output can be persisted directly without translation.
    """

    event_id: str
    frame_id: str
    frame_index: int
    timestamp_ms: int
    cursor_x: int
    cursor_y: int
    velocity: float
    is_click: bool = False
    is_drag: bool = False
    detection_method: str = "motion"
    confidence: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


# ─── Tracker ─────────────────────────────────────────────────────────────────

class CursorTracker:
    """Motion-based cursor localisation across an ordered frame sequence.

    Stateless across calls — instances are cheap to create.  All state for
    a single tracking run lives on the stack of :meth:`track_frames`.
    """

    def __init__(self, config: Optional[CursorTrackerConfig] = None):
        self.config = config or CursorTrackerConfig()

    def track_frames(self, frames: Iterable[FrameInput]) -> list[CursorEvent]:
        """Run the detector on a frame sequence, returning one event per
        frame that produced a confident cursor reading.

        Frames are processed in the order supplied.  Implementations must
        sort by ``frame_index`` (or ``timestamp_ms``) before calling so
        adjacent inputs share temporal locality — the detector relies on
        consecutive frames being close in time.
        """
        frames_list = list(frames)
        if len(frames_list) < 2:
            return []

        cv2, np = _import_cv()  # raises ImportError if unavailable

        # First pass: per-pair candidate detection.
        per_frame_candidate: list[Optional[tuple[int, int, float, int]]] = [None] * len(frames_list)
        prev_image = self._load(frames_list[0], cv2)

        for idx in range(1, len(frames_list)):
            curr = frames_list[idx]
            curr_image = self._load(curr, cv2)
            if curr_image is None or prev_image is None:
                prev_image = curr_image
                continue

            cand = self._best_cursor_candidate(prev_image, curr_image, cv2, np)
            per_frame_candidate[idx] = cand
            prev_image = curr_image

        # Second pass: derive velocity, click, drag flags.
        events: list[CursorEvent] = []
        last_position: Optional[tuple[int, int, int]] = None  # (x, y, ts_ms)
        stationary_run = 0

        for idx, frame in enumerate(frames_list):
            cand = per_frame_candidate[idx]
            if cand is None:
                last_position = None
                stationary_run = 0
                continue

            cx, cy, area, comp_count = cand
            ts_ms = frame.timestamp_ms

            # Velocity = distance / elapsed time relative to last position.
            velocity = 0.0
            is_drag = False
            if last_position is not None:
                lx, ly, lts = last_position
                dx = cx - lx
                dy = cy - ly
                dist = math.hypot(dx, dy)
                dt_s = max((ts_ms - lts) / 1000.0, 0.001)
                velocity = dist / dt_s
                if dist <= self.config.stationary_distance_px:
                    stationary_run += 1
                else:
                    stationary_run = 0
                if velocity >= self.config.drag_min_velocity_px_per_s:
                    is_drag = True

            # Confidence: fewer simultaneous components = higher cursor
            # confidence (the cursor is the only thing moving).
            comp_factor = max(0.0, 1.0 - (comp_count - 1) / 12.0)
            area_factor = 1.0 if area <= 256 else max(0.4, 1.0 - (area - 256) / 768.0)
            confidence = round(min(1.0, max(0.0, comp_factor * area_factor)), 3)

            events.append(CursorEvent(
                event_id=str(uuid.uuid4()),
                frame_id=frame.frame_id,
                frame_index=frame.frame_index,
                timestamp_ms=ts_ms,
                cursor_x=int(cx),
                cursor_y=int(cy),
                velocity=round(velocity, 2),
                is_click=False,  # filled in below after dwell detection
                is_drag=is_drag,
                detection_method="motion",
                confidence=confidence,
                metadata={"area_px": int(area), "components": int(comp_count)},
            ))
            last_position = (cx, cy, ts_ms)

        # Click annotation: motion-based detection emits no event for
        # frames where the cursor is stationary (no motion → nothing to
        # localise from absdiff).  Translate gaps between motion events
        # into click annotations: when two consecutive emitted events
        # are far apart in frame index, the user held the cursor at
        # the first event's position for that many frames.
        self._annotate_clicks_from_gaps(events, frames_list)

        return events

    # ── Private helpers ─────────────────────────────────────────

    def _load(self, frame: FrameInput, cv2) -> Optional[Any]:
        """Return a BGR uint8 image array for a :class:`FrameInput`.

        Prefers the in-memory ``image_bgr`` when supplied (tests do this
        to skip disk I/O); otherwise reads from ``frame_path`` via cv2.
        Returns ``None`` when the image cannot be read — the caller
        skips that frame rather than aborting the whole run.
        """
        if frame.image_bgr is not None:
            return frame.image_bgr
        if frame.frame_path and os.path.exists(frame.frame_path):
            return cv2.imread(frame.frame_path)
        return None

    def _best_cursor_candidate(
        self,
        prev_image: Any,
        curr_image: Any,
        cv2,
        np,
    ) -> Optional[tuple[int, int, float, int]]:
        """Find the connected component most consistent with a cursor.

        Returns ``(centroid_x, centroid_y, area_px, total_components)`` or
        ``None`` when no plausible cursor candidate exists in this pair.
        """
        if prev_image.shape != curr_image.shape:
            return None

        diff = cv2.absdiff(curr_image, prev_image)
        gray = cv2.cvtColor(diff, cv2.COLOR_BGR2GRAY) if diff.ndim == 3 else diff
        _, mask = cv2.threshold(
            gray, self.config.diff_threshold, 255, cv2.THRESH_BINARY,
        )

        num_labels, _labels, stats, centroids = cv2.connectedComponentsWithStats(
            mask, connectivity=8,
        )
        # Skip label 0 — that's the background.
        candidates: list[tuple[int, int, float, int]] = []
        for label_idx in range(1, num_labels):
            area = int(stats[label_idx, cv2.CC_STAT_AREA])
            width = int(stats[label_idx, cv2.CC_STAT_WIDTH])
            height = int(stats[label_idx, cv2.CC_STAT_HEIGHT])
            if not (self.config.min_cursor_area <= area <= self.config.max_cursor_area):
                continue
            if width > self.config.max_cursor_dimension or height > self.config.max_cursor_dimension:
                continue
            cx = float(centroids[label_idx][0])
            cy = float(centroids[label_idx][1])
            candidates.append((int(cx), int(cy), float(area), num_labels - 1))

        if not candidates:
            return None
        # Too many simultaneous changes → likely a scene change, not a
        # cursor frame.  Skip rather than emit a low-confidence reading.
        if num_labels - 1 > self.config.max_components_for_cursor_frame:
            return None

        # Best candidate is the smallest one (most cursor-like).  When two
        # are equal, the more-compact bounding box wins.  Numpy ties on
        # area are rare in practice — the first one seen is chosen.
        best = min(candidates, key=lambda c: (c[2], abs(c[0]) + abs(c[1])))
        return best

    def _annotate_clicks_from_gaps(
        self,
        events: list[CursorEvent],
        all_frames: list[FrameInput],
    ) -> None:
        """Mark ``is_click=True`` from two complementary stationary patterns.

        Pattern A — motion gap.  Motion-based detection is silent during
        truly stationary cursor periods (no diff signal to localise).
        When two consecutive emitted events are separated by ≥
        ``click_dwell_frames`` skipped frames, the cursor was held
        during the gap and the user clicked at the pre-gap position.

        Pattern B — sub-pixel jitter.  When the cursor stays within
        ``stationary_distance_px`` for ≥ ``click_dwell_frames``
        consecutive emitted events (1-pixel anti-aliasing drift, OS
        rendering noise, ...), we still consider that a held position
        and annotate is_click on the trailing event of the run.

        Click is annotated on the event whose position immediately
        preceded the resumed motion so its ``after_frame_id`` can be the
        first frame of the response that follows.
        """
        config = self.config
        if len(events) < 2:
            return

        # ── Pattern A: motion gaps ──────────────────────────────
        for i in range(len(events) - 1):
            curr = events[i]
            nxt = events[i + 1]
            gap = nxt.frame_index - curr.frame_index - 1
            if gap < config.click_dwell_frames:
                continue
            curr.is_click = True
            curr.metadata = dict(curr.metadata or {})
            curr.metadata["click_dwell_frames"] = gap

        # ── Pattern B: consecutive sub-pixel-stationary events ──
        run_start: Optional[int] = None
        for i in range(1, len(events)):
            prev = events[i - 1]
            curr = events[i]
            dist = math.hypot(
                curr.cursor_x - prev.cursor_x, curr.cursor_y - prev.cursor_y,
            )
            if dist <= config.stationary_distance_px:
                if run_start is None:
                    run_start = i - 1
            else:
                self._maybe_mark_run_click(events, run_start, i - 1)
                run_start = None
        # Trailing run
        self._maybe_mark_run_click(events, run_start, len(events) - 1)

    def _maybe_mark_run_click(
        self,
        events: list[CursorEvent],
        run_start: Optional[int],
        run_end: int,
    ) -> None:
        if run_start is None:
            return
        run_length = run_end - run_start + 1
        if run_length >= self.config.click_dwell_frames:
            target = events[run_end]
            target.is_click = True
            target.metadata = dict(target.metadata or {})
            target.metadata["click_dwell_frames"] = run_length


# ─── Lazy OpenCV import ──────────────────────────────────────────────────────

def _import_cv():
    """Import ``cv2`` and ``numpy`` lazily so unit tests can mock or skip."""
    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore
    except ImportError as exc:  # pragma: no cover — environment-dependent
        raise ImportError(
            "cursor tracking requires OpenCV and NumPy. "
            "Install with: pip install opencv-python-headless numpy"
        ) from exc
    return cv2, np
