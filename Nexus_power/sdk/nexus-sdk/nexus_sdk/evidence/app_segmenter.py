"""
App Segmenter — Phase 5 algorithm.

Groups scenes into application instances using three ordered boundary-detection
heuristics.  Every assignment carries ``segmentation_basis`` and
``segmentation_confidence`` so no field is labeled as absolute truth.

Instance IDs are **deterministic**: uuid5(NAMESPACE_OID, "instance:{artifact_id}:{first_scene_index}")
so that retrying the same artifact produces identical IDs and DB upserts are
idempotent via ON CONFLICT DO NOTHING.

No external dependencies — pure Python computation over scene dicts.
"""
from __future__ import annotations

import re
import uuid
from typing import Optional

# Namespace for deterministic IDs (shared convention across SDK evidence modules)
_NS = uuid.NAMESPACE_OID


# ── Segmentation basis labels & confidence scores ──────────────

SEGMENTATION_BASIS = {
    "app_type_change": 0.90,
    "domain_change": 0.85,
    "window_title_change": 0.65,
    "no_boundary": 1.00,
}

_URL_DOMAIN_RE = re.compile(r"https?://(?:www\.)?([^/\s]+)")


def _registered_domain(url: str) -> str:
    """Return the eTLD+1 portion of a URL (cheap approximation)."""
    m = _URL_DOMAIN_RE.match(url)
    if not m:
        return ""
    host = m.group(1)
    parts = host.split(".")
    # Use last 2 parts (cheap eTLD+1 — good enough for heuristic segmentation)
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def _detect_boundary(prev: dict, curr: dict) -> tuple[str, float]:
    """Return (segmentation_basis, confidence) for the transition prev→curr.

    Priority order:
    1. app_type_change  — application_type differs between scenes
    2. domain_change    — both scenes have detected_url with different domains
    3. window_title_change — screen_name changed (noisy heuristic)
    4. no_boundary
    """
    prev_app = (prev.get("app_type") or prev.get("application_type") or "").strip()
    curr_app = (curr.get("app_type") or curr.get("application_type") or "").strip()

    if prev_app and curr_app and prev_app.lower() != curr_app.lower():
        return "app_type_change", SEGMENTATION_BASIS["app_type_change"]

    prev_url = (prev.get("detected_url") or "").strip()
    curr_url = (curr.get("detected_url") or "").strip()
    if prev_url and curr_url:
        if _registered_domain(prev_url) != _registered_domain(curr_url):
            return "domain_change", SEGMENTATION_BASIS["domain_change"]

    prev_title = (prev.get("screen_name") or "").strip()
    curr_title = (curr.get("screen_name") or "").strip()
    if prev_title and curr_title and prev_title != curr_title:
        return "window_title_change", SEGMENTATION_BASIS["window_title_change"]

    return "no_boundary", SEGMENTATION_BASIS["no_boundary"]


class AppSegmenter:
    """Segment a list of scenes into application instances."""

    def segment(
        self,
        scenes: list[dict],
        artifact_id: str = "",
        session_id: str = "",
        tenant_id: str = "",
    ) -> tuple[list[dict], list[dict]]:
        """Walk scenes chronologically and assign app_instance_id to each.

        Parameters
        ----------
        scenes:      Scene dicts produced by build_scenes(), in scene_index order.
        artifact_id: Canonical artifact scope.
        session_id:  Session scope.
        tenant_id:   Tenant scope.

        Returns
        -------
        (enriched_scenes, app_instances) where:
        - enriched_scenes: scenes list with app_instance_id, segmentation_basis,
          segmentation_confidence added to each dict.
        - app_instances: list of app_instance dicts ready for DB insert.
        """
        if not scenes:
            return [], []

        sorted_scenes = sorted(scenes, key=lambda s: int(s.get("scene_index", 0)))

        instances: list[dict] = []
        # Deterministic first instance ID — keyed on artifact + first scene index
        first_scene_idx = int(sorted_scenes[0].get("scene_index", 0))
        current_instance_id = str(uuid.uuid5(_NS, f"instance:{artifact_id}:{first_scene_idx}"))
        current_start_idx = 0
        current_basis = "no_boundary"
        current_confidence = 1.0

        enriched: list[dict] = []
        instance_scene_indices: list[int] = []  # scene_index values in current instance

        def _flush_instance(end_idx: int) -> None:
            """Close the current app instance and append to instances."""
            if not instance_scene_indices:
                return
            s = sorted_scenes[current_start_idx]
            instances.append({
                "instance_id": current_instance_id,
                "artifact_id": artifact_id,
                "session_id": session_id,
                "tenant_id": tenant_id,
                "app_type": (
                    s.get("app_type") or s.get("application_type") or "unknown"
                ),
                "app_name": _derive_app_name(s),
                "detected_url": s.get("detected_url", ""),
                "window_title": s.get("screen_name", ""),
                "segmentation_basis": current_basis,
                "segmentation_confidence": current_confidence,
                "first_scene_index": instance_scene_indices[0],
                "last_scene_index": instance_scene_indices[-1],
                "scene_count": len(instance_scene_indices),
            })

        prev_scene: Optional[dict] = None
        for i, scene in enumerate(sorted_scenes):
            if prev_scene is None:
                # First scene — start the first instance
                scene_copy = dict(scene)
                scene_copy["app_instance_id"] = current_instance_id
                scene_copy["segmentation_basis"] = "no_boundary"
                scene_copy["segmentation_confidence"] = 1.0
                enriched.append(scene_copy)
                instance_scene_indices.append(int(scene.get("scene_index", i)))
            else:
                basis, confidence = _detect_boundary(prev_scene, scene)
                if basis != "no_boundary":
                    # Close current instance
                    _flush_instance(i - 1)
                    # Deterministic new instance ID keyed on artifact + this scene's index
                    new_start_idx = int(scene.get("scene_index", i))
                    current_instance_id = str(uuid.uuid5(_NS, f"instance:{artifact_id}:{new_start_idx}"))
                    current_start_idx = i
                    current_basis = basis
                    current_confidence = confidence
                    instance_scene_indices = []

                scene_copy = dict(scene)
                scene_copy["app_instance_id"] = current_instance_id
                scene_copy["segmentation_basis"] = basis
                scene_copy["segmentation_confidence"] = confidence
                enriched.append(scene_copy)
                instance_scene_indices.append(int(scene.get("scene_index", i)))

            prev_scene = scene

        # Flush the last instance
        _flush_instance(len(sorted_scenes) - 1)

        return enriched, instances


# Pattern to detect heuristic-generated template screen names that carry
# no business meaning (e.g., "Screen showing desktop_app with 8 detected elements").
_TEMPLATE_SCREEN_NAME = re.compile(
    r"^Screen showing \w+ with \d+ detected elements?$", re.IGNORECASE
)

# Map ApplicationType enum values to human-readable labels
_APP_TYPE_LABELS: dict[str, str] = {
    "web_ui": "Web Application",
    "email_client": "Email",
    "mainframe_3270": "Mainframe",
    "pdf_document": "PDF Document",
    "excel_spreadsheet": "Spreadsheet",
    "terminal": "Terminal",
    "database_ui": "Database",
    "desktop_app": "Desktop Application",
    "unknown": "Application",
}


def _derive_app_name(scene: dict) -> str:
    """Derive a human-readable app name from a scene's metadata.

    Priority order (most meaningful first):
    1. URL hostname — e.g., "guardianlife.com" (must look like a real domain)
    2. app_type label — e.g., "Web Application", "Email"
       (only used when no URL; avoids generic "Desktop Application" alone)
    3. screen_name first phrase — truncated to keep it short
    4. Fallback: "Unknown"
    """
    # 1. URL hostname — only if it looks like a real domain (has a dot + TLD)
    url = (scene.get("detected_url") or "").strip()
    if url:
        m = _URL_DOMAIN_RE.match(url)
        if m:
            hostname = m.group(1)
            # Validate: must contain a dot (e.g. "guardianlife.com" not "Life.In")
            # and be at least 5 chars total
            if "." in hostname and len(hostname) >= 5:
                return hostname[:200]

    # 2. app_type humanized
    raw_type = (
        scene.get("app_type") or scene.get("application_type") or ""
    ).strip().lower()
    type_label = _APP_TYPE_LABELS.get(raw_type, "")

    # 3. screen_name — first short phrase only, not the full dump
    title = (scene.get("screen_name") or "").strip()
    if title and not _TEMPLATE_SCREEN_NAME.match(title):
        # Truncate to first clause
        short_title = title
        for sep in [" — ", " - ", " | ", ". "]:
            idx = short_title.find(sep)
            if 3 <= idx <= 40:
                short_title = short_title[:idx]
                break
        short_title = short_title[:50].strip()
        if type_label and type_label != "Application":
            return f"{type_label} — {short_title}"
        return short_title

    # 4. Just the type label
    if type_label:
        return type_label

    return "Unknown"
