"""
Multimodal Enrichment Utilities — read-only functions over canonical artifact data.

These helpers consume data already stored in the canonical artifact
(full_artifact_json) and produce structured multimodal analysis.
They never write back to the artifact, never import from existing
route modules, and never modify the persona or test-strategy caches.

Consumers: e2e_architect route (Phase C).
NOT consumed by: personas.py, test_strategy.py.
"""
from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


# ── Public API ─────────────────────────────────────────────────


def build_ui_inventory(visual_data: dict) -> dict:
    """Extract a deduplicated UI element inventory from visual_analysis frames.

    Args:
        visual_data: ``full_artifact_json["visual_analysis"]`` dict.
                     Expected shape: ``{frames: [{ui_elements: [...], ...}]}``

    Returns:
        Compact summary::

            {
                "buttons": ["Submit", "Calculate"],
                "text_fields": [{"label": "First Name", "value": "John"}, ...],
                "dropdowns": [{"label": "State", "observed_options": [...]}],
                "checkboxes": [{"label": "Agree", "selected": true}],
                "radios": [{"label": "Male", "selected": true}],
                "links": ["Click here", ...],
                "tabs": ["General", "Details"],
                "total_elements": 47,
                "by_scene": [{"scene_idx": 0, "timestamp": 0.0, "element_count": 12}, ...]
            }

    """
    frames = visual_data.get("frames") or []

    buttons: dict[str, None] = {}       # ordered set via dict
    text_fields: list[dict] = []
    dropdowns: list[dict] = []
    checkboxes: list[dict] = []
    radios: list[dict] = []
    links: list[dict] = []
    tabs: dict[str, None] = {}
    by_scene: list[dict] = []
    total_elements = 0

    seen_fields: set[str] = set()       # deduplicate text_field labels
    seen_dropdowns: set[str] = set()
    seen_checkboxes: set[str] = set()
    seen_radios: set[str] = set()

    for idx, frame in enumerate(frames):
        elements = frame.get("ui_elements") or []
        scene_count = 0

        for elem in elements:
            etype = _str(elem, "element_type").lower()
            text = _str(elem, "text")
            props = elem.get("properties") or {} if isinstance(elem, dict) else {}

            if not etype:
                continue

            scene_count += 1
            total_elements += 1

            if etype == "button":
                normalised = text.strip().title() if text else ""
                if normalised and normalised not in buttons:
                    buttons[normalised] = None

            elif etype == "text_field":
                label = _str(props, "label") or text
                value = _str(props, "value")
                key = label.lower().strip()
                if key and key not in seen_fields:
                    seen_fields.add(key)
                    entry: dict[str, Any] = {"label": label.strip()}
                    if value:
                        entry["value"] = value.strip()
                    text_fields.append(entry)

            elif etype == "dropdown":
                label = _str(props, "label") or text
                key = label.lower().strip()
                if key and key not in seen_dropdowns:
                    seen_dropdowns.add(key)
                    entry = {"label": label.strip()}
                    options = props.get("options")
                    if isinstance(options, list) and options:
                        entry["observed_options"] = [str(o) for o in options[:20]]
                    dropdowns.append(entry)

            elif etype == "checkbox":
                label = text.strip() if text else ""
                key = label.lower()
                if key and key not in seen_checkboxes:
                    seen_checkboxes.add(key)
                    checkboxes.append({
                        "label": label,
                        "selected": bool(props.get("selected")),
                    })

            elif etype == "radio":
                label = text.strip() if text else ""
                key = label.lower()
                if key and key not in seen_radios:
                    seen_radios.add(key)
                    radios.append({
                        "label": label,
                        "selected": bool(props.get("selected")),
                    })

            elif etype == "link":
                links.append(text.strip() if text else "")

            elif etype == "tab":
                normalised = text.strip() if text else ""
                if normalised and normalised not in tabs:
                    tabs[normalised] = None

        by_scene.append({
            "scene_idx": idx,
            "timestamp": _float(frame, "timestamp_seconds"),
            "element_count": scene_count,
        })

    return {
        "buttons": list(buttons.keys()),
        "text_fields": text_fields,
        "dropdowns": dropdowns,
        "checkboxes": checkboxes,
        "radios": radios,
        "links": [l for l in links if l],
        "tabs": list(tabs.keys()),
        "total_elements": total_elements,
        "by_scene": by_scene,
    }


def time_align_scenes(
    visual_data: dict,
    transcript_data: dict,
) -> list[dict]:
    """Match visual frames to overlapping transcript segments by timestamp.

    Args:
        visual_data:    ``full_artifact_json["visual_analysis"]``
        transcript_data: ``full_artifact_json["transcript"]``  (or safe_transcript)

    Returns:
        List of time-aligned scenes::

            [{
                "scene_idx": 0,
                "timestamp_range": "0:00-0:15",
                "visual_description": "Insurance quote form...",
                "ui_elements": ["First Name field", "State dropdown"],
                "transcript_excerpt": "So first we enter customer info...",
                "speaker": "Agent_1"
            }, ...]

    """
    frames = visual_data.get("frames") or []
    segments = transcript_data.get("segments") or []

    if not frames:
        return []

    # Build a sorted list of transcript segments with timestamps.
    ts_segments = []
    for seg in segments:
        start = _to_seconds(seg.get("start_time") or seg.get("start") or seg.get("timestamp", 0))
        end = _to_seconds(seg.get("end_time") or seg.get("end", start))
        text = _str(seg, "text").strip()
        speaker = _str(seg, "speaker") or "Unknown"
        if text:
            ts_segments.append({
                "start": start,
                "end": end,
                "text": text,
                "speaker": speaker,
            })

    aligned: list[dict] = []

    for idx, frame in enumerate(frames):
        frame_ts = _float(frame, "timestamp_seconds")
        description = _str(frame, "description")
        elements = frame.get("ui_elements") or []

        # Collect transcript segments that overlap this frame's timestamp
        # Use a window: frame_ts ± half of typical scene duration
        # For keyframes, match segments within ±15 seconds
        window_start = max(0.0, frame_ts - 15.0)
        window_end = frame_ts + 15.0

        matching_segments: list[dict] = []
        for ts in ts_segments:
            if ts["end"] >= window_start and ts["start"] <= window_end:
                matching_segments.append(ts)

        # Take up to 3 segments — prefer those closest to frame timestamp
        matching_segments.sort(key=lambda s: abs(s["start"] - frame_ts))
        top_segments = matching_segments[:3]

        transcript_excerpt = " ".join(s["text"] for s in top_segments)
        primary_speaker = top_segments[0]["speaker"] if top_segments else "Unknown"

        # Summarise UI elements as compact labels
        ui_labels: list[str] = []
        for elem in elements:
            etype = _str(elem, "element_type").lower()
            text = _str(elem, "text")
            props = elem.get("properties") or {} if isinstance(elem, dict) else {}
            label = _str(props, "label") or text
            if label:
                suffix = f" {etype}" if etype and etype not in ("label", "unknown") else ""
                ui_labels.append(f"{label.strip()}{suffix}")

        # Determine next frame timestamp for range display
        next_ts = frame_ts + 15.0
        if idx + 1 < len(frames):
            next_ts = _float(frames[idx + 1], "timestamp_seconds")

        aligned.append({
            "scene_idx": idx,
            "timestamp_range": f"{_fmt_ts(frame_ts)}-{_fmt_ts(next_ts)}",
            "visual_description": description[:300] if description else "",
            "ui_elements": ui_labels[:20],
            "transcript_excerpt": transcript_excerpt[:500] if transcript_excerpt else "",
            "speaker": primary_speaker,
        })

    return aligned


def match_visual_to_workflows(
    visual_data: dict,
    domain_map: dict,
) -> list[dict]:
    """Best-effort: attempt to match scene descriptions to workflow steps.

    Returns matches with confidence scores.  This is approximate —
    keyword-based matching, not guaranteed workflow truth.

    Args:
        visual_data: ``full_artifact_json["visual_analysis"]``
        domain_map:  ``persona_draft_cache["domain_map"]``

    Returns:
        List of match dicts::

            [{
                "workflow_step_number": 1,
                "workflow_step_name": "Enter Customer Info",
                "matched_scene_idx": 2,
                "scene_description": "Form with First Name, Last Name...",
                "ui_elements_found": ["First Name text_field", ...],
                "match_quality": "strong" | "moderate" | "weak",
                "confidence": 0.7,
                "evidence": {
                    "text": "Form showing input fields...",
                    "source_modality": "visual",
                    "confidence": 0.7,
                    "timestamp_range": "0:15-0:30"
                }
            }]

    """
    workflows = domain_map.get("workflows") or []
    frames = visual_data.get("frames") or []

    if not workflows or not frames:
        return []

    # Build keyword index for each workflow step
    step_keywords: list[tuple[dict, set[str]]] = []
    for wf in workflows:
        if not isinstance(wf, dict):
            continue
        keywords: set[str] = set()
        # Extract keywords from step name and description
        name = _str(wf, "name")
        desc = _str(wf, "description")
        for text in (name, desc):
            keywords.update(_extract_keywords(text))
        # Add system names
        for sys_name in wf.get("systems") or []:
            if isinstance(sys_name, str):
                keywords.update(_extract_keywords(sys_name))
        # Add actor names
        for actor_name in wf.get("actors") or []:
            if isinstance(actor_name, str):
                keywords.update(_extract_keywords(actor_name))
        if keywords:
            step_keywords.append((wf, keywords))

    # Build searchable text per frame
    frame_texts: list[tuple[int, str, list[str]]] = []
    for idx, frame in enumerate(frames):
        desc = _str(frame, "description").lower()
        ocr = _str(frame, "extracted_text").lower()
        elem_labels = []
        for elem in frame.get("ui_elements") or []:
            text = _str(elem, "text")
            etype = _str(elem, "element_type")
            if text:
                elem_labels.append(f"{text.strip()} {etype}".strip())
        combined = f"{desc} {ocr} {' '.join(elem_labels)}"
        frame_texts.append((idx, combined, elem_labels))

    matches: list[dict] = []

    for wf, keywords in step_keywords:
        best_score = 0.0
        best_idx = -1
        best_frame_labels: list[str] = []

        for frame_idx, combined_text, elem_labels in frame_texts:
            # Count keyword hits
            hits = sum(1 for kw in keywords if kw in combined_text)
            if not hits:
                continue
            score = hits / len(keywords) if keywords else 0.0
            if score > best_score:
                best_score = score
                best_idx = frame_idx
                best_frame_labels = elem_labels

        if best_idx < 0 or best_score < 0.1:
            continue

        # Classify match quality
        if best_score >= 0.6:
            quality = "strong"
            confidence = min(0.9, 0.5 + best_score * 0.5)
        elif best_score >= 0.3:
            quality = "moderate"
            confidence = 0.4 + best_score * 0.3
        else:
            quality = "weak"
            confidence = max(0.2, best_score * 0.5)

        frame = frames[best_idx]
        frame_ts = _float(frame, "timestamp_seconds")
        frame_desc = _str(frame, "description")[:300]

        matches.append({
            "workflow_step_number": wf.get("step_number", 0),
            "workflow_step_name": _str(wf, "name"),
            "matched_scene_idx": best_idx,
            "scene_description": frame_desc,
            "ui_elements_found": best_frame_labels[:15],
            "match_quality": quality,
            "confidence": round(confidence, 2),
            "evidence": {
                "text": frame_desc[:200] if frame_desc else f"Scene {best_idx} visual observation",
                "source_modality": "visual",
                "confidence": round(confidence, 2),
                "timestamp_range": f"{_fmt_ts(frame_ts)}-{_fmt_ts(frame_ts + 15.0)}",
            },
        })

    return matches


def build_e2e_brain_payload(artifact: dict) -> dict:
    """Assemble the complete payload for Brain E2E Architect from a canonical artifact.

    Reads persona_draft_cache, test_strategy_cache, visual_analysis,
    visual_graph, and transcript from the artifact — produces a dict
    ready to POST to ``/api/v1/brain/generate-e2e-architect``.

    Args:
        artifact: The full artifact row as a dict (from ``row_to_dict(row)``).

    Returns:
        Dict with all fields expected by ``GenerateE2EArchitectRequest``.

    """
    full_json = artifact.get("full_artifact_json") or {}
    persona_cache = full_json.get("persona_draft_cache") or {}
    test_cache = full_json.get("test_strategy_cache") or {}
    visual_data = full_json.get("visual_analysis") or {}
    visual_graph = full_json.get("visual_graph") or {}
    transcript_data = full_json.get("transcript") or {}

    persona = persona_cache.get("persona") or {}
    domain_map = persona_cache.get("domain_map") or {}
    grounding = persona_cache.get("grounding_contract") or {}

    # Existing test scenarios (for deduplication)
    existing_scenarios = test_cache.get("test_scenarios") or []
    # Normalise: ensure each entry is a plain dict
    existing_scenarios_dicts = []
    for s in existing_scenarios:
        if isinstance(s, dict):
            existing_scenarios_dicts.append(s)

    # Scene descriptions from visual frames
    scene_descriptions = [
        f.get("description", "")
        for f in visual_data.get("frames") or []
        if f.get("description")
    ]

    # UI element inventory (Phase B helper)
    ui_inventory = build_ui_inventory(visual_data)

    # Time-aligned multimodal scenes (Phase B helper)
    multimodal_scenes = time_align_scenes(visual_data, transcript_data)

    # Transcript segments for Brain
    segments = transcript_data.get("segments") or []

    # Raw OCR evidence — first-class visual text when structured ui_elements
    # are sparse.  Brain can use this to infer form fields, labels, and state
    # even when the heuristic structuring layer missed them.
    raw_ocr_per_scene: list[dict] = []
    for idx, frame in enumerate(visual_data.get("frames") or []):
        ocr_text = _str(frame, "extracted_text").strip()
        if ocr_text:
            raw_ocr_per_scene.append({
                "scene_idx": idx,
                "timestamp": _float(frame, "timestamp_seconds"),
                "ocr_text": ocr_text[:2000],
            })

    return {
        "artifact_id": artifact.get("artifact_id", ""),
        "session_id": artifact.get("session_id", ""),
        "tenant_id": artifact.get("tenant_id", ""),
        "persona_name": persona.get("name", ""),
        "persona_description": persona.get("description", ""),
        "domain_map": domain_map,
        "grounding_contract": grounding,
        "existing_test_scenarios": existing_scenarios_dicts,
        "visual_summary": artifact.get("visual_summary", "") or "",
        "scene_descriptions": scene_descriptions,
        "ui_element_inventory": ui_inventory,
        "multimodal_scenes": multimodal_scenes,
        "raw_ocr_evidence": raw_ocr_per_scene,
        "application_types_seen": artifact.get("application_types_seen") or [],
        "visual_graph_nodes": visual_graph.get("nodes") or [],
        "transcript_segments": segments,
        "duration_seconds": artifact.get("duration_seconds", 0.0) or 0.0,
        "frame_count": artifact.get("frame_count", 0) or 0,
        "scene_count": artifact.get("scene_count", 0) or 0,
    }


# ── Private helpers ────────────────────────────────────────────


def _str(d: Any, key: str) -> str:
    """Safely extract a string value from a dict-like object."""
    if isinstance(d, dict):
        v = d.get(key, "")
        return str(v) if v is not None else ""
    return ""


def _float(d: Any, key: str) -> float:
    """Safely extract a float value from a dict-like object."""
    if isinstance(d, dict):
        v = d.get(key, 0.0)
        try:
            return float(v) if v is not None else 0.0
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def _to_seconds(value: Any) -> float:
    """Convert a timestamp value to seconds (handles str like '1:23' and numeric)."""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        value = value.strip()
        # Handle M:SS or H:MM:SS format
        parts = value.split(":")
        try:
            if len(parts) == 2:
                return float(parts[0]) * 60 + float(parts[1])
            if len(parts) == 3:
                return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
            return float(value)
        except (ValueError, IndexError):
            return 0.0
    return 0.0


def _fmt_ts(seconds: float) -> str:
    """Format seconds as M:SS string."""
    minutes = int(seconds) // 60
    secs = int(seconds) % 60
    return f"{minutes}:{secs:02d}"


def _extract_keywords(text: str) -> set[str]:
    """Extract meaningful lowercase keywords from a text string.

    Filters out very short words and common stop words.
    """
    if not text:
        return set()
    words = re.findall(r"[a-zA-Z]{3,}", text.lower())
    stop = {
        "the", "and", "for", "are", "but", "not", "you", "all",
        "can", "had", "her", "was", "one", "our", "out", "has",
        "his", "how", "its", "may", "new", "now", "old", "see",
        "way", "who", "did", "get", "let", "say", "she", "too",
        "use", "with", "this", "that", "from", "they", "been",
        "have", "many", "some", "them", "than", "each", "make",
        "like", "will", "into", "time", "very", "when", "come",
        "step", "then", "also", "back", "only", "just", "over",
        "such", "after", "should", "which", "their", "about",
    }
    return {w for w in words if w not in stop}
