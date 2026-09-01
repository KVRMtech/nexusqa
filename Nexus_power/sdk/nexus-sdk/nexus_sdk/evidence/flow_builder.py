"""
Flow Builder — Phase 7 algorithm.

Builds a directed visual flow graph as two accumulation layers:
  Layer 1 — observed_transition: chronological fact (scene A → scene B).
  Layer 2 — action_confirmed_transition: Layer 1 edge enriched with an
             identified trigger control or action verb.

Edge IDs are **deterministic**: uuid5(NAMESPACE_OID, "edge:{artifact_id}:{from_scene_id}:{to_scene_id}:{edge_type}:{action_type}")
so that retrying the same artifact produces identical IDs and DB upserts are
idempotent via ON CONFLICT DO NOTHING.

No external dependencies — pure Python computation over scene/frame dicts.
"""
from __future__ import annotations

import re
import uuid
from typing import Optional

# Namespace for deterministic IDs (shared convention across SDK evidence modules)
_NS = uuid.NAMESPACE_OID

_ACTION_VERBS = re.compile(
    r"\b(click|press|select|submit|navigate|type|tap|enter|choose|pick)\b",
    re.IGNORECASE,
)

# Eyes transition-pair LLM emits canonical action_kind values; map to
# enrich_with_actions action_type vocabulary (used by _KIND_MAP / summaries).
_TRANSITION_KIND_TO_ACTION_TYPE: dict[str, str] = {
    "click_cta": "click",
    "enter_text": "type",
    "select_option": "select",
    "toggle": "click",
    "navigate": "navigate",
    "submit_form": "submit",
    "scroll": "navigate",
}


def _safe_app_id(scene: dict) -> Optional[str]:
    return scene.get("app_instance_id") or None


def _longest_new_phrase(from_text: str, to_text: str, max_len: int = 60) -> str:
    """Find the longest contiguous phrase in *to_text* absent from *from_text*.

    Instead of dumping a random 5-word bag from set difference, this scans the
    target OCR for the longest substring (by word count) that doesn't appear
    anywhere in the source OCR.  Returns a human-readable action label like
    "Term Life Insurance Calculator" rather than "for fields being questions: how".
    """
    from_lower = from_text.lower()
    if not from_lower or not to_text:
        return ""

    # Tokenize target text preserving order
    words = to_text.split()
    if not words:
        return ""

    best = ""
    # Sliding window: try longest phrases first
    for length in range(min(len(words), 8), 0, -1):
        for start in range(len(words) - length + 1):
            candidate = " ".join(words[start:start + length])
            # Candidate must not appear (case-insensitive) in source text
            if candidate.lower() not in from_lower:
                # Skip if it's just noise (all short/non-alpha words)
                alpha_words = [w for w in words[start:start + length] if len(w) >= 3 and w.isalpha()]
                if len(alpha_words) >= max(1, length // 2):
                    if len(candidate) > len(best):
                        best = candidate
        if best:
            break  # Found at current length; no need to check shorter

    return best[:max_len]


def _extract_verb_action(description: str) -> str:
    """Extract a concise 'verb + object' from a LLaVA description.

    Instead of dumping the full 200-char description, extracts the clause
    containing the action verb (up to 60 chars).
    """
    match = _ACTION_VERBS.search(description)
    if not match:
        return ""

    # Find the sentence containing the verb
    verb_pos = match.start()
    # Walk backward to sentence start (period, start-of-string)
    start = max(description.rfind(". ", 0, verb_pos) + 2, 0)
    # Walk forward to sentence end (period, end-of-string)
    end = description.find(". ", verb_pos)
    if end == -1:
        end = len(description)

    clause = description[start:end].strip()
    return clause[:60]


class FlowBuilder:
    """Build and optionally enrich a flow graph from scenes and frames."""

    def build_observed_transitions(
        self,
        scenes: list[dict],
        artifact_id: str = "",
        tenant_id: str = "",
    ) -> list[dict]:
        """Layer 1: chronological transitions.

        For each consecutive scene pair (scenes[i], scenes[i+1]) produce an
        ``observed_transition`` edge.  evidence_confidence is always 1.0
        because chronological sequence is an unambiguous fact.

        Parameters
        ----------
        scenes:      Scene dicts (with app_instance_id populated by Phase 5).
        artifact_id: Canonical artifact scope.
        tenant_id:   Tenant scope.

        Returns
        -------
        List of edge dicts ready for DB insert (visual_flow_edges table).
        """
        if len(scenes) < 2:
            return []

        sorted_scenes = sorted(scenes, key=lambda s: int(s.get("scene_index", 0)))
        edges: list[dict] = []

        for i in range(len(sorted_scenes) - 1):
            s_from = sorted_scenes[i]
            s_to = sorted_scenes[i + 1]

            from_flow = s_from.get("flow_id")
            to_flow = s_to.get("flow_id")
            # Determine edge type: cross-flow context switch vs intra-flow transition.
            # Previously cross-flow edges were silently dropped, breaking multi-app
            # session graphs entirely.  Now they are emitted as "app_switch" edges so
            # the frontend can render context-switching arcs between application lanes.
            is_cross_flow = bool(from_flow and to_flow and from_flow != to_flow)
            edge_type = "app_switch" if is_cross_flow else "observed_transition"

            from_app = _safe_app_id(s_from)
            to_app = _safe_app_id(s_to)
            intra_app = (from_app is not None and from_app == to_app)

            transition_ms = max(
                0,
                int(s_to.get("start_ms", 0)) - int(s_from.get("end_ms", 0)),
            )

            from_scene_id = s_from.get("scene_id", "")
            to_scene_id = s_to.get("scene_id", "")
            # Deterministic edge_id: same from→to pair always → same UUID
            edge_id = str(uuid.uuid5(
                _NS,
                f"edge:{artifact_id}:{from_scene_id}:{to_scene_id}:{edge_type}:transition",
            ))

            edges.append({
                "edge_id": edge_id,
                "artifact_id": artifact_id,
                "tenant_id": tenant_id,
                "from_scene_id": from_scene_id,
                "to_scene_id": to_scene_id,
                "edge_type": edge_type,
                "trigger_control_id": None,
                "action_type": "app_switch" if is_cross_flow else "transition",
                "action_value": "",
                "transition_ms": transition_ms,
                "intra_app": intra_app,
                "evidence_confidence": 1.0,
            })

        return edges

    def enrich_with_actions(
        self,
        edges: list[dict],
        frames: list[dict],
        controls: list[dict],
        scenes: list[dict],
        scene_transitions: Optional[list[dict]] = None,
    ) -> list[dict]:
        """Layer 2: enrich Layer 1 edges with action evidence.

        For each edge, examines the last frame of ``from_scene`` for action
        signals.  Promotes edge_type to "action_confirmed_transition" when
        evidence is found.

        Signals and confidence:
          Pair-frame transition LLM (exit → entry) when Eyes enables it → model confidence (capped 0.95)
          LLaVA action verb in last from-scene frame description → 0.80
          URL change between scenes (same domain, path changed) → 0.85
          New OCR value in to-scene (field fill) → 0.75
          trigger_control_id linked from evidence_controls → 0.90

        Parameters
        ----------
        edges:    Layer 1 edges list (mutated in place, original dicts are
                  replaced with updated copies to preserve caller's list).
        frames:   All frame dicts for this artifact.
        controls: Evidence controls dicts from Phase 6.
        scenes:   Scene dicts (used to look up OCR text etc.).
        scene_transitions: Optional list of SceneTransitionAnalysis-shaped dicts
            (``from_frame_id``, ``to_frame_id``, ``action_kind``, …) from Eyes
            ``EYES_TRANSITION_LLM``. Matched on each edge's chronological exit
            frame of ``from_scene`` and entry frame of ``to_scene``.

        Returns
        -------
        Updated edge list (same length as input, some edges promoted).
        """
        trans_by_pair: dict[tuple[str, str], dict] = {}
        for t in scene_transitions or []:
            if not isinstance(t, dict):
                continue
            k = (t.get("from_frame_id") or "", t.get("to_frame_id") or "")
            if k[0] and k[1]:
                trans_by_pair[k] = t

        # Build lookup indices
        scene_by_id: dict[str, dict] = {s["scene_id"]: s for s in scenes if s.get("scene_id")}
        controls_by_scene: dict[str, list[dict]] = {}
        for c in controls:
            sid = c.get("scene_id", "")
            controls_by_scene.setdefault(sid, []).append(c)

        # Group frames by scene_id; sort by frame_index descending (last frame first)
        frames_by_scene: dict[str, list[dict]] = {}
        for f in frames:
            sid = f.get("scene_id", "")
            if sid:
                frames_by_scene.setdefault(sid, []).append(f)
        for sid in frames_by_scene:
            frames_by_scene[sid].sort(key=lambda f: int(f.get("frame_index", 0)), reverse=True)

        enriched: list[dict] = []
        for edge in edges:
            updated = dict(edge)
            from_id = edge.get("from_scene_id", "")
            to_id = edge.get("to_scene_id", "")

            from_scene = scene_by_id.get(from_id, {})
            to_scene = scene_by_id.get(to_id, {})

            # Last frame of from_scene
            from_frames = frames_by_scene.get(from_id, [])
            last_frame = from_frames[0] if from_frames else {}
            to_frames_list = frames_by_scene.get(to_id, [])
            first_in_to = (
                min(
                    to_frames_list,
                    key=lambda f: int(f.get("frame_index", 0)),
                )
                if to_frames_list
                else {}
            )
            exit_fid = last_frame.get("frame_id", "")
            entry_fid = first_in_to.get("frame_id", "")
            trans = trans_by_pair.get((exit_fid, entry_fid)) if exit_fid and entry_fid else None

            description = (
                last_frame.get("description") or last_frame.get("llava_description") or ""
            )

            action_type = "transition"
            action_value = ""
            evidence_confidence = 1.0  # Layer 1 baseline
            trigger_control_id: Optional[str] = None
            confirmed = False
            evidence_sources: list[str] = []
            fallback_used = False

            # Signal 1: LLaVA action verb — extract concise clause
            verb_match = _ACTION_VERBS.search(description)
            if verb_match:
                action_type = verb_match.group(1).lower()
                action_value = _extract_verb_action(description)
                evidence_confidence = 0.80
                confirmed = True
                evidence_sources.append("llava_verb")

            # Signal 2: URL path change (same domain)
            from_url = (from_scene.get("detected_url") or "").strip()
            to_url = (to_scene.get("detected_url") or "").strip()
            if from_url and to_url and from_url != to_url:
                from_domain = _domain(from_url)
                to_domain = _domain(to_url)
                if from_domain and from_domain == to_domain:
                    action_type = "navigate"
                    action_value = to_url
                    evidence_confidence = 0.85
                    confirmed = True
                    evidence_sources.append("url_path_change")

            # Signal 3: Meaningful new content in to_scene
            if not confirmed:
                from_ocr_text = (from_scene.get("ocr_text") or "")
                to_ocr_text = (to_scene.get("ocr_text") or "")
                phrase = _longest_new_phrase(from_ocr_text, to_ocr_text)
                if phrase:
                    action_type = "navigate"
                    action_value = phrase
                    evidence_confidence = 0.75
                    confirmed = True
                    evidence_sources.append("ocr_delta")
                    fallback_used = True

            # Transition pair LLM: two-frame step label from Eyes (optional).
            if trans:
                t_kind = (trans.get("action_kind") or "").strip().lower()
                t_conf = float(trans.get("confidence") or 0.0)
                mapped = _TRANSITION_KIND_TO_ACTION_TYPE.get(t_kind, "")
                if mapped and t_conf >= 0.5:
                    t_label = (trans.get("action_label") or "").strip()
                    t_target = (trans.get("target_element_label") or "").strip()
                    t_observed = (trans.get("observed_value") or "").strip()
                    t_value = t_label or t_target or t_observed
                    url_keeps = (
                        "url_path_change" in evidence_sources
                        and t_conf <= evidence_confidence + 1e-9
                    )
                    promote_transition = (not confirmed or t_conf > evidence_confidence + 1e-9) and not url_keeps
                    if promote_transition:
                        action_type = mapped
                        action_value = t_value
                        evidence_confidence = min(0.95, t_conf)
                        confirmed = True
                        evidence_sources = ["transition_llm"]
                        fallback_used = False
                        trigger_control_id = None
                        verb_match = _ACTION_VERBS.search(description)

            # Signal 4: Link to automation-ready control from from_scene
            trigger_control: Optional[dict] = None
            if confirmed and verb_match:
                from_controls = controls_by_scene.get(from_id, [])
                for ctrl in from_controls:
                    if (
                        ctrl.get("automation_ready")
                        and ctrl.get("label_text", "").lower() in description.lower()
                    ):
                        trigger_control_id = ctrl.get("control_id")
                        trigger_control = ctrl
                        evidence_confidence = 0.90
                        evidence_sources.append("grounded_control")
                        break

            if confirmed and trans and "transition_llm" in evidence_sources:
                tlab = (trans.get("target_element_label") or "").strip().lower()
                if tlab and trigger_control_id is None:
                    from_controls = controls_by_scene.get(from_id, [])
                    for ctrl in from_controls:
                        if not ctrl.get("automation_ready"):
                            continue
                        cl = ctrl.get("label_text", "").strip().lower()
                        if cl and (cl in tlab or tlab in cl):
                            trigger_control_id = ctrl.get("control_id")
                            trigger_control = ctrl
                            evidence_confidence = max(evidence_confidence, 0.90)
                            evidence_sources.append("grounded_control")
                            break

            if confirmed:
                updated["edge_type"] = "action_confirmed_transition"
                updated["action_type"] = action_type
                updated["action_value"] = action_value
                updated["evidence_confidence"] = evidence_confidence
                updated["trigger_control_id"] = trigger_control_id
                # Recompute deterministic edge_id for the promoted edge so that
                # repeated runs with the same evidence produce the same PK and
                # ON CONFLICT DO NOTHING actually fires.
                artifact_id_for_edge = updated.get("artifact_id", "")
                updated["edge_id"] = str(uuid.uuid5(
                    _NS,
                    f"edge:{artifact_id_for_edge}:{updated['from_scene_id']}:{updated['to_scene_id']}:action_confirmed_transition:{action_type}",
                ))

            # Phase 8 — structured action summary and quality fields
            action_summary = _build_primary_action_summary(
                action_type=action_type if confirmed else "transition",
                action_value=action_value,
                evidence_confidence=evidence_confidence,
                evidence_sources=evidence_sources,
                from_scene_id=from_id,
                to_scene_id=to_id,
                to_scene=to_scene,
                trigger_control=trigger_control,
                fallback_used=fallback_used,
            )
            updated["primary_action_summary"] = action_summary
            updated["action_quality"] = action_summary["action_quality"]
            updated["action_confidence"] = action_summary["action_confidence"]

            enriched.append(updated)

        return enriched


def _domain(url: str) -> str:
    """Return domain from URL (cheap extraction)."""
    m = re.match(r"https?://(?:www\.)?([^/\s]+)", url)
    return m.group(1) if m else ""


# Canonical verb classes for action_kind field.
_KIND_MAP: dict[str, str] = {
    "navigate": "navigate",
    "click": "click_cta",
    "submit": "submit_form",
    "type": "enter_text",
    "enter": "enter_text",
    "select": "select_option",
    "choose": "select_option",
    "pick": "select_option",
    "press": "click_cta",
    "transition": "navigate",
    "app_switch": "navigate",
}


def _build_primary_action_summary(
    action_type: str,
    action_value: str,
    evidence_confidence: float,
    evidence_sources: list[str],
    from_scene_id: str,
    to_scene_id: str,
    to_scene: dict,
    trigger_control: Optional[dict],
    fallback_used: bool,
) -> dict:
    """Build a structured, confidence-scored primary_action_summary for an edge.

    Returns the persisted truth object that the UI should render for the
    transition into ``to_scene``.  Confidence is preserved verbatim from the
    signal that produced the edge so that the UI can apply its own rendering
    threshold (strong / degraded / weak).

    action_quality:
      strong  — confidence >= 0.85  (grounded control or URL navigation)
      degraded — 0.55 <= confidence < 0.85 (one strong signal, possibly noisy)
      weak    — confidence < 0.55  (OCR phrase diff only or no signal)
    """
    action_kind = _KIND_MAP.get(action_type.lower(), "navigate")
    low_confidence_reasons: list[str] = []

    # Derive destination screen label from scene_state_summary when available
    state_summary = to_scene.get("scene_state_summary") or {}
    screen_title = (
        state_summary.get("screen_title")
        or to_scene.get("screen_name")
        or "next screen"
    )
    screen_short = screen_title.split(" › ")[-1] if " › " in screen_title else screen_title.split(" — ")[-1] if " — " in screen_title else screen_title

    # ── Action label and subject ──────────────────────────────────────────
    action_subject = screen_short
    action_value_out = action_value

    if trigger_control:
        ctrl_type = trigger_control.get("element_type", "")
        ctrl_label = (trigger_control.get("label_text") or "").strip()
        observed = (trigger_control.get("observed_value") or "").strip()

        if ctrl_type in ("dropdown", "select") and observed:
            action_kind = "select_option"
            action_subject = ctrl_label or "option"
            action_value_out = observed
            if ctrl_label:
                action_label = f"Selected {ctrl_label} = {observed}"
            else:
                action_label = f"Selected: {observed}"
        elif ctrl_type in ("text_field", "input", "textarea") and observed:
            action_kind = "enter_text"
            action_subject = ctrl_label or "field"
            action_value_out = observed[:60]
            action_label = f"Entered {ctrl_label}: {observed[:30]}" if ctrl_label else f"Typed: {observed[:30]}"
        elif ctrl_label:
            action_label = f"Clicked {ctrl_label}"
            action_subject = ctrl_label
        else:
            action_label = f"Clicked and reached {screen_short}"

    elif (
        "transition_llm" in evidence_sources
        and (action_value or "").strip()
        and not (action_type == "navigate" and action_value.strip().startswith("http"))
    ):
        av = action_value.strip()
        action_label = av[:120]
        action_subject = screen_short
        action_value_out = av[:120]

    elif action_type == "navigate" and action_value and action_value.startswith("http"):
        # URL navigation — most reliable signal
        action_label = f"Navigated to {screen_short}"
        action_subject = screen_short

    elif action_type == "navigate" and action_value:
        # OCR phrase diff navigation — do NOT embed the raw OCR diff phrase in the label.
        # The diff phrase often contains ambient noise: Zoom participant name overlays,
        # browser nav-menu expansions, and other non-action OCR bleed.
        # Use the destination screen name as the human-facing label; the raw diff is
        # preserved in action_value for debugging and is not exposed in the UI.
        action_label = f"Navigated to {screen_short}"
        action_subject = screen_short
        if fallback_used:
            low_confidence_reasons.append("ocr_phrase_diff_only")

    elif action_type in ("click", "press"):
        action_label = f"Clicked and reached {screen_short}"

    elif action_type in ("submit",):
        action_kind = "submit_form"
        action_label = f"Submitted form → {screen_short}"

    else:
        action_label = f"Reviewed {screen_short}"
        action_kind = "navigate"

    # ── Confidence quality bucket ─────────────────────────────────────────
    if not trigger_control and not (action_type == "navigate" and action_value.startswith("http")):
        low_confidence_reasons.append("no_grounded_control")

    if evidence_confidence >= 0.85:
        action_quality = "strong"
    elif evidence_confidence >= 0.55:
        action_quality = "degraded"
    else:
        action_quality = "weak"

    if action_quality == "weak":
        low_confidence_reasons.append("low_confidence_signal")

    return {
        "action_kind": action_kind,
        "action_label": action_label,
        "action_subject": action_subject,
        "action_value": action_value_out,
        "target_control_type": (trigger_control or {}).get("element_type", ""),
        "target_control_label": (trigger_control or {}).get("label_text", ""),
        "trigger_control_id": (trigger_control or {}).get("control_id"),
        "source_scene_id": from_scene_id,
        "target_scene_id": to_scene_id,
        "resulting_state_key": state_summary.get("page_key", ""),
        "evidence_sources": evidence_sources,
        "action_confidence": round(evidence_confidence, 3),
        "action_quality": action_quality,
        "low_confidence_reasons": low_confidence_reasons,
        "fallback_used": fallback_used,
    }
