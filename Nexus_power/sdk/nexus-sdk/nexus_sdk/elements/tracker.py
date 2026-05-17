"""Per-artifact UI element tracker.

Greedy assignment with two-level scoring:

  1. Label exact match + bbox centre within ``label_match_radius_px``
     → same entity (highest priority).
  2. Bbox centre within ``spatial_match_radius_px`` AND element_type
     compatible AND no better label match available → same entity
     (handles a cursor hover that briefly clears the label OCR).
  3. Otherwise a fresh entity_id is allocated.

Entities expire after ``forget_after_frames`` consecutive frames with no
match — protects long sessions from the tracker matching against a
stale element from 100 frames ago that happens to be at the same
coordinates as something new.

The matcher is deliberately not the optimal Hungarian assignment —
QA recordings have ≤ ~30 elements per frame so greedy O(N·M) is fine
and the algorithm stays auditable for debugging.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional


# ─── Public types ────────────────────────────────────────────────────────────

@dataclass
class TrackedElement:
    """One element after entity assignment.

    Original detection fields (element_type, text, properties, …) are
    preserved verbatim; ``entity_id``, ``first_frame_index``,
    ``persistence_count`` are appended so consumers can spot truly
    persistent controls (≥ 3 frames is a strong signal for stability).
    """

    element_type: str
    text: str
    bbox_centre_x: Optional[int]
    bbox_centre_y: Optional[int]
    bbox_width: Optional[int]
    bbox_height: Optional[int]
    confidence: float
    properties: dict[str, Any]
    entity_id: str
    first_frame_index: int
    last_frame_index: int
    persistence_count: int


@dataclass
class ElementTrackerConfig:
    """Tunable thresholds.  Defaults are tested against KT recordings."""

    # Label-match search radius in pixels.  Same label within this
    # distance is the strongest "same entity" signal.
    label_match_radius_px: float = 80.0
    # Spatial-only fallback radius.  Used when label changed (cursor
    # hover, text caret blink) but element_type matches and the box
    # centre is essentially in place.
    spatial_match_radius_px: float = 30.0
    # Element_type compatibility groups — sub-types that should still
    # match each other (e.g. "input" / "text_field").
    element_type_aliases: dict[str, set[str]] = field(default_factory=lambda: {
        "input": {"input", "text_field", "textbox", "field"},
        "text_field": {"input", "text_field", "textbox", "field"},
        "select": {"select", "dropdown"},
        "dropdown": {"select", "dropdown"},
        "link": {"link", "anchor"},
        "anchor": {"link", "anchor"},
    })
    # Forget an entity after this many consecutive frames with no match.
    forget_after_frames: int = 8


# ─── Tracker ─────────────────────────────────────────────────────────────────

class ElementTracker:
    """Stateful greedy entity tracker across a frame sequence."""

    def __init__(self, config: Optional[ElementTrackerConfig] = None):
        self._cfg = config or ElementTrackerConfig()
        # entity_id -> last seen state.  Mutated in place as frames
        # are processed; entities older than forget_after_frames are
        # pruned at the start of each ``assign_entities`` call.
        self._entities: dict[str, dict[str, Any]] = {}

    # ── Public API ───────────────────────────────────────────────

    def assign_entities(
        self,
        ui_elements: Iterable[dict],
        *,
        frame_index: int,
    ) -> list[dict]:
        """Return a new list of UI element dicts annotated with entity_id.

        The original input is not mutated.  ``frame_index`` must be
        monotonically non-decreasing across calls — the tracker uses it
        to age out unseen entities.
        """
        self._prune_stale(frame_index)

        candidates = list(ui_elements or [])
        # Pre-compute features for matching.
        features: list[dict[str, Any]] = []
        for el in candidates:
            feat = self._extract_features(el)
            if feat is None:
                # Ignore element entries without enough signal to track —
                # they are still emitted but with a fresh entity_id (no
                # persistence reasoning).
                features.append({"feat": None, "el": el})
            else:
                features.append({"feat": feat, "el": el})

        used_entity_ids: set[str] = set()
        assignments: list[Optional[str]] = [None] * len(features)

        # Pass 1 — exact label match within label_match_radius_px.
        for i, item in enumerate(features):
            feat = item["feat"]
            if not feat or not feat.get("normalised_label"):
                continue
            best = self._find_best_label_match(feat, used_entity_ids)
            if best is not None:
                assignments[i] = best
                used_entity_ids.add(best)

        # Pass 2 — spatial proximity match for remaining elements.
        for i, item in enumerate(features):
            if assignments[i] is not None:
                continue
            feat = item["feat"]
            if not feat:
                continue
            best = self._find_best_spatial_match(feat, used_entity_ids)
            if best is not None:
                assignments[i] = best
                used_entity_ids.add(best)

        # Pass 3 — every still-unassigned element gets a fresh entity.
        out: list[dict] = []
        for i, item in enumerate(features):
            el = item["el"]
            feat = item["feat"]
            entity_id = assignments[i]
            if entity_id is None:
                entity_id = str(uuid.uuid4())

            # Update the tracker's state for this entity.
            entry = self._entities.get(entity_id)
            if entry is None:
                entry = {
                    "entity_id": entity_id,
                    "first_frame_index": frame_index,
                    "last_frame_index": frame_index,
                    "persistence_count": 1,
                    "label": (feat or {}).get("normalised_label", ""),
                    "element_type": (feat or {}).get("element_type", ""),
                    "centre_x": (feat or {}).get("centre_x"),
                    "centre_y": (feat or {}).get("centre_y"),
                }
                self._entities[entity_id] = entry
            else:
                entry["last_frame_index"] = frame_index
                entry["persistence_count"] = int(entry.get("persistence_count", 0)) + 1
                if feat is not None:
                    entry["centre_x"] = feat.get("centre_x") or entry.get("centre_x")
                    entry["centre_y"] = feat.get("centre_y") or entry.get("centre_y")
                    if feat.get("normalised_label"):
                        entry["label"] = feat["normalised_label"]

            # Annotate the output dict with the assignment without
            # mutating the caller's input.
            annotated = dict(el or {})
            annotated["entity_id"] = entity_id
            annotated["first_frame_index"] = entry["first_frame_index"]
            annotated["last_frame_index"] = entry["last_frame_index"]
            annotated["persistence_count"] = entry["persistence_count"]
            out.append(annotated)

        return out

    # ── Internals ────────────────────────────────────────────────

    def _prune_stale(self, current_frame_index: int) -> None:
        if not self._entities:
            return
        threshold = current_frame_index - self._cfg.forget_after_frames
        to_drop = [
            eid for eid, entry in self._entities.items()
            if int(entry.get("last_frame_index", 0)) < threshold
        ]
        for eid in to_drop:
            self._entities.pop(eid, None)

    def _extract_features(self, el: dict) -> Optional[dict[str, Any]]:
        """Compute matching features for one element.

        Tolerates the two common bbox encodings:
          * dict {x, y, width, height} or {x1, y1, x2, y2}
          * list [x1, y1, x2, y2]

        Returns ``None`` when no meaningful spatial signal is present
        — those elements are tracked but never matched.
        """
        if not isinstance(el, dict):
            return None
        element_type = str(el.get("element_type") or el.get("type") or "").lower().strip()
        text = str(el.get("text") or el.get("label") or "")
        normalised_label = " ".join(text.split()).lower()[:120]

        bbox = el.get("bbox") or el.get("location") or el.get("bounding_box")
        cx = cy = w = h = None
        if isinstance(bbox, dict):
            if all(k in bbox for k in ("x", "y", "width", "height")):
                cx = int(bbox["x"]) + int(bbox["width"]) // 2
                cy = int(bbox["y"]) + int(bbox["height"]) // 2
                w = int(bbox["width"])
                h = int(bbox["height"])
            elif all(k in bbox for k in ("x1", "y1", "x2", "y2")):
                cx = (int(bbox["x1"]) + int(bbox["x2"])) // 2
                cy = (int(bbox["y1"]) + int(bbox["y2"])) // 2
                w = int(bbox["x2"]) - int(bbox["x1"])
                h = int(bbox["y2"]) - int(bbox["y1"])
        elif isinstance(bbox, list) and len(bbox) == 4:
            cx = (int(bbox[0]) + int(bbox[2])) // 2
            cy = (int(bbox[1]) + int(bbox[3])) // 2
            w = int(bbox[2]) - int(bbox[0])
            h = int(bbox[3]) - int(bbox[1])

        if cx is None and not normalised_label:
            return None

        return {
            "element_type": element_type,
            "normalised_label": normalised_label,
            "centre_x": cx,
            "centre_y": cy,
            "width": w,
            "height": h,
        }

    def _types_compatible(self, a: str, b: str) -> bool:
        if not a or not b:
            return True
        if a == b:
            return True
        for group in self._cfg.element_type_aliases.values():
            if a in group and b in group:
                return True
        return False

    def _find_best_label_match(
        self, feat: dict[str, Any], used: set[str],
    ) -> Optional[str]:
        target_label = feat.get("normalised_label")
        if not target_label:
            return None
        cx = feat.get("centre_x")
        cy = feat.get("centre_y")
        radius = self._cfg.label_match_radius_px

        best: Optional[tuple[float, str]] = None
        for entity_id, entry in self._entities.items():
            if entity_id in used:
                continue
            if entry.get("label") != target_label:
                continue
            if not self._types_compatible(
                feat.get("element_type", ""), entry.get("element_type", ""),
            ):
                continue
            ex = entry.get("centre_x")
            ey = entry.get("centre_y")
            if cx is not None and ex is not None:
                d = math.hypot(cx - ex, cy - ey)
                if d > radius:
                    continue
                score = d
            else:
                # Label match without spatial info still wins, but with
                # the largest possible distance so any element that has
                # spatial info edges it out.
                score = radius + 1.0
            if best is None or score < best[0]:
                best = (score, entity_id)
        return best[1] if best else None

    def _find_best_spatial_match(
        self, feat: dict[str, Any], used: set[str],
    ) -> Optional[str]:
        """Spatial fallback for elements that lost their label this frame.

        Fires only when the *current* element has no readable label
        (cursor hover obscured the OCR, focus ring repaint, etc.).  If
        the new element does have a non-empty label and that label does
        NOT match the prior entity's label, the spatial pass refuses to
        bind — a label change is a strong signal of identity change
        even at the same coordinate.
        """
        cx = feat.get("centre_x")
        cy = feat.get("centre_y")
        if cx is None:
            return None
        new_label = feat.get("normalised_label") or ""
        radius = self._cfg.spatial_match_radius_px
        best: Optional[tuple[float, str]] = None
        for entity_id, entry in self._entities.items():
            if entity_id in used:
                continue
            if not self._types_compatible(
                feat.get("element_type", ""), entry.get("element_type", ""),
            ):
                continue
            ex = entry.get("centre_x")
            ey = entry.get("centre_y")
            if ex is None:
                continue
            entry_label = entry.get("label") or ""
            # Both have a label and they differ → different entity.  We
            # only spatial-match when the new element's label is empty
            # (it lost OCR signal this frame).
            if new_label and entry_label and new_label != entry_label:
                continue
            d = math.hypot(cx - ex, cy - ey)
            if d > radius:
                continue
            if best is None or d < best[0]:
                best = (d, entity_id)
        return best[1] if best else None
