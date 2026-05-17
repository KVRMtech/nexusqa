"""Cursor tracking — frame-pair motion analysis to localise the user's pointer.

Screen recordings nearly always contain a visible cursor: it is the small,
moving object that overlays an otherwise relatively static UI.  Locating
it per frame and detecting clicks (cursor pauses, then resumes) gives us
a strong, language-independent signal of *where* the user acted.

This module is intentionally **template-free** — it does not match
pre-saved cursor sprites because Windows / macOS / Linux all use
different cursor designs, theme overrides exist, and DPI scaling
changes the size.  Instead it relies on motion: between two
consecutive frames the cursor is the connected component that is
small, lonely (few other simultaneous changes), and persistent across
the surrounding frames.

Usage::

    from nexus_sdk.cursor import CursorTracker

    tracker = CursorTracker()
    events = tracker.track_frames([
        FrameInput(frame_id="f1", frame_index=0, timestamp_ms=0,    image_bgr=img0),
        FrameInput(frame_id="f2", frame_index=1, timestamp_ms=500,  image_bgr=img1),
        ...
    ])

The result is a list of :class:`CursorEvent` — one record per frame for
which a cursor location was confidently detected.  Frames where motion
was inconclusive are simply omitted (no fabricated coordinates).

The full pipeline persists the events to the ``cursor_events`` table
introduced in Alembic 017, where the triangulated action classifier
joins them onto frame_actions to populate ``cursor_x`` / ``cursor_y``
on each :class:`evidence_steps` row.
"""

from .tracker import (
    CursorEvent,
    CursorTracker,
    CursorTrackerConfig,
    FrameInput,
)

__all__ = [
    "CursorEvent",
    "CursorTracker",
    "CursorTrackerConfig",
    "FrameInput",
]
