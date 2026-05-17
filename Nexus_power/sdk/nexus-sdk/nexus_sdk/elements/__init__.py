"""UI element persistence — track the same control across consecutive frames.

Without persistence, every frame's ui_elements list is opaque: element
#5 in frame N is unrelated to element #5 in frame N+1, even though
they're the same Submit button.  That kills:

  * Cursor → control linking ("user clicked which button?")
  * Stable selector resolution ("same entity → same selector")
  * Per-entity behaviour analysis ("typed in this field across 8 frames")
  * UI dictionary recognition ("seen this control 4 times in this scene")

This module assigns a stable ``entity_id`` (UUID) to elements that
persist across frames using bounded greedy matching on label + bbox
proximity.  The output is a per-frame list of elements where each
element has an extra ``entity_id`` field; consumers downstream
(triangulator, control_extractor, ui_dictionary) join on it.

Usage::

    from nexus_sdk.elements import ElementTracker

    tracker = ElementTracker()
    for frame in ordered_frames:
        frame["ui_elements"] = tracker.assign_entities(
            frame.get("ui_elements") or [],
            frame_index=frame["frame_index"],
        )

The tracker is stateful across calls so two frames in different
``track`` invocations cannot share an entity — instantiate one per
artifact (or per scene to bound scope).
"""

from .tracker import ElementTracker, TrackedElement

__all__ = [
    "ElementTracker",
    "TrackedElement",
]
