"""Intra-scene step extraction — emit per-frame user actions inside a scene.

A scene represents one logical page (URL + state).  Within a scene, the SME
can perform many user actions: type into a form, open a dropdown, select an
option, click a button.  These actions are the unit that downstream
generators (test cases, automation scripts) need to emit reliable steps.

The visual extractor already captures the visible state every ~0.5-1.0s.
What was missing was the *diff between consecutive frames within the same
scene* — the place where typing, selection, and clicks become observable.

This module reads the ordered frames inside a scene and emits a list of
``frame_action`` dicts, each of which is one observable user action.  No
LLM call, no schema migration: results are stamped into the existing
``scene_state_summary['frame_actions']`` JSON array so the frontend can
render them immediately.

Action kinds are restricted to a fixed taxonomy that matches the canonical
verbs already used by ``control_extractor`` so downstream code can consume
them without a translation table:

    enter_text       — alphanumeric tokens appeared in OCR (form input fill)
    select_option    — short value replaced a placeholder (dropdown choice)
    click            — UI element changed state (button activated, focused)
    open_overlay     — large new chunk of OCR text appeared (modal/dropdown)
    close_overlay    — large chunk disappeared
    scroll_or_repaint — visible content shifted but token-set unchanged
    state_change     — generic fallback when signals disagree but a real
                       difference is observed

All emitted actions carry ``confidence`` ∈ [0, 1] derived from the strength
of the contributing signals.  Low-confidence actions can be filtered by the
frontend or downstream consumer; nothing is dropped silently here.
"""
from __future__ import annotations

import re
from typing import Iterable

# Re-use chrome filter and tokenisers from build_scenes — single source of truth.
from .build_scenes import _structural_fingerprint, _text_fingerprint


# ── Configuration ────────────────────────────────────────────────────────────

# Below this Jaccard, treat the frames as "very different" (likely overlay).
_OVERLAY_JACCARD = 0.55

# Above this Jaccard on chrome-stripped fingerprint, treat as repaint/scroll only.
_REPAINT_JACCARD = 0.92

# Token additions at or above this count = a major content change (overlay open).
_OVERLAY_TOKEN_DELTA = 8

# Token additions in this range likely correspond to a single form-fill step.
_FORM_FILL_MIN_TOKENS = 1
_FORM_FILL_MAX_TOKENS = 6

# Minimum OCR confidence on the *new* frame for the diff to count.  Below this
# the added tokens are likely OCR noise and we suppress the action.
_MIN_OCR_CONFIDENCE = 0.30

# Tokens that look like UUIDs / hashes / timestamps and should never count as
# meaningful new text (they are usually generated chrome the user did not
# author).
_NOISE_TOKEN_RE = re.compile(
    r"^("
    r"[0-9a-f]{8,}|"           # long hex strings
    r"\d{1,2}[:.]\d{2}([:.]\d{2})?|"  # times like 12:34 or 1.45
    r")$"
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _filter_meaningful_tokens(tokens: Iterable[str]) -> set[str]:
    """Drop tokens that are pure OCR/clock/UUID noise."""
    return {t for t in tokens if not _NOISE_TOKEN_RE.match(t)}


def _word_jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _ui_element_set(frame: dict) -> set[tuple[str, str]]:
    """Project UI elements to a comparable set of ``(element_type, label)`` pairs.

    Empty/missing labels are dropped — they cannot be diffed reliably.
    """
    raw = frame.get("ui_elements") or frame.get("ui_elements_json") or []
    if isinstance(raw, str):
        # ui_elements_json stored as JSON string in some upstream paths
        try:
            import json
            raw = json.loads(raw)
        except Exception:
            return set()
    out: set[tuple[str, str]] = set()
    for el in raw or []:
        if not isinstance(el, dict):
            continue
        et = (el.get("element_type") or el.get("type") or "").strip().lower()
        lbl = (el.get("text") or el.get("label") or "").strip().lower()
        if et and lbl:
            out.add((et, lbl[:40]))
    return out


def _best_target_label(
    added_tokens: set[str],
    prev_ui: set[tuple[str, str]],
    curr_ui: set[tuple[str, str]],
) -> str:
    """Best guess at *what the user acted on* — typically the label of the UI
    element that newly appeared or whose text changed.  Falls back to the
    longest meaningful added token when no UI signal is available.
    """
    ui_added = curr_ui - prev_ui
    if ui_added:
        # Prefer interactive element types
        priority = ("button", "link", "input", "select", "dropdown", "checkbox", "radio")
        for et in priority:
            for et_curr, lbl in ui_added:
                if et_curr == et and lbl:
                    return lbl
        # Otherwise just take any added element label
        for _, lbl in ui_added:
            if lbl:
                return lbl
    if added_tokens:
        return max(added_tokens, key=len)
    return ""


def _classify_action(
    prev_text: str,
    curr_text: str,
    prev_ui: set[tuple[str, str]],
    curr_ui: set[tuple[str, str]],
    curr_ocr_confidence: float,
) -> tuple[str, str, float, list[str]]:
    """Decide what user action explains the diff between two frames.

    Returns ``(action_kind, delta_text, confidence, evidence_signals)``.
    An empty ``action_kind`` means "no actionable change" — caller should skip.
    """
    prev_struct = _structural_fingerprint(prev_text)
    curr_struct = _structural_fingerprint(curr_text)

    added_struct = _filter_meaningful_tokens(curr_struct - prev_struct)
    removed_struct = _filter_meaningful_tokens(prev_struct - curr_struct)

    # Total content-token change as a proxy for magnitude of the user action.
    n_added = len(added_struct)
    n_removed = len(removed_struct)
    struct_jaccard = _word_jaccard(prev_struct, curr_struct)

    ui_added = curr_ui - prev_ui
    ui_removed = prev_ui - curr_ui

    evidence: list[str] = []

    # Frame contract: this function is invoked on *captured* frames.  The
    # eyes-engine frame extractor only persists frames that exceed the dHash
    # threshold from their neighbour, so reaching this point already means a
    # real visual change happened.  Therefore "no detectable text diff" must
    # be interpreted as "the action was invisible to OCR" — typically a form
    # field fill, dropdown selection, or click that does not alter chrome
    # OCR text — rather than "nothing happened".
    raw_identical = prev_text.strip() == curr_text.strip()
    if raw_identical and not ui_added and not ui_removed:
        return (
            "user_interaction", "", 0.30,
            ["frame_captured", "ocr_blind"],
        )

    # Pure repaint / scroll: structural content unchanged but raw OCR shifted
    # (chrome / scroll bar / cursor position tokens differ).  This is the
    # actual scroll / repaint case.
    if struct_jaccard >= _REPAINT_JACCARD and not ui_added and not ui_removed:
        return "scroll_or_repaint", "", 0.4, ["repaint"]

    # Overlay opened: large jump in new content with low overlap
    if n_added >= _OVERLAY_TOKEN_DELTA and struct_jaccard < _OVERLAY_JACCARD:
        delta = " ".join(sorted(added_struct, key=len, reverse=True)[:6])
        evidence.append("ocr_new_block")
        if ui_added:
            evidence.append("ui_added")
        return "open_overlay", delta, 0.7, evidence

    # Overlay closed: lots of content vanished
    if n_removed >= _OVERLAY_TOKEN_DELTA and struct_jaccard < _OVERLAY_JACCARD:
        delta = " ".join(sorted(removed_struct, key=len, reverse=True)[:6])
        evidence.append("ocr_block_removed")
        if ui_removed:
            evidence.append("ui_removed")
        return "close_overlay", delta, 0.65, evidence

    # Form-fill range: a small handful of new tokens — likely typed value
    if _FORM_FILL_MIN_TOKENS <= n_added <= _FORM_FILL_MAX_TOKENS and n_removed <= 2:
        # If OCR confidence is too low, demote to lower-confidence "state_change"
        if curr_ocr_confidence < _MIN_OCR_CONFIDENCE:
            evidence.append("low_ocr_confidence")
            return "state_change", " ".join(added_struct), 0.35, evidence

        # Heuristic: many short alphanumeric tokens (look like typed values)
        # vs few longer tokens (look like a clicked label that appeared).
        avg_len = sum(len(t) for t in added_struct) / max(1, n_added)
        target = _best_target_label(added_struct, prev_ui, curr_ui)
        delta = " ".join(sorted(added_struct, key=len, reverse=True)[:4])

        evidence.append("ocr_token_added")
        if ui_added:
            evidence.append("ui_added")

        if avg_len <= 4 and not ui_added:
            return "enter_text", delta, 0.6, evidence
        if ui_added:
            return "click", target or delta, 0.7, evidence
        return "select_option", target or delta, 0.55, evidence

    # UI element changed but OCR steady → button activation, focus shift
    if (ui_added or ui_removed) and struct_jaccard >= _REPAINT_JACCARD:
        target = _best_target_label(set(), prev_ui, curr_ui)
        evidence.append("ui_only_change")
        return "click", target, 0.55, evidence

    # Mixed signal, neither overlay nor clean form-fill
    if n_added > 0 or n_removed > 0:
        delta = " ".join(sorted(added_struct, key=len, reverse=True)[:4])
        evidence.append("mixed_signals")
        return "state_change", delta, 0.4, evidence

    # OCR / UI signals are silent but the frame was captured — which means the
    # upstream extractor judged it visually distinct from its neighbour (that
    # is the contract of the frame-diff stage).  Emit a generic interaction so
    # downstream consumers still see the action timeline even when EasyOCR
    # cannot read form-field contents and LLaVA produced no description.  The
    # confidence is intentionally low so the frontend can filter or annotate.
    evidence.append("frame_captured_no_text_signal")
    return "user_interaction", "", 0.25, evidence


# ── Main entry point ─────────────────────────────────────────────────────────

def extract_intra_scene_steps(scene_frames: list[dict]) -> list[dict]:
    """Return per-pair frame actions for one scene's frames, sorted by index.

    Parameters
    ----------
    scene_frames:
        Frames belonging to a single scene, ordered by ``frame_index``.  Each
        dict should carry ``frame_index``, ``timestamp_seconds`` (or
        ``timestamp_ms``), ``extracted_text``, ``ocr_confidence``, and
        optionally ``ui_elements`` / ``ui_elements_json``.

    Returns
    -------
    A list of action dicts, one for each consecutive frame pair where a
    meaningful change was observed.  When no change is detected the pair
    contributes nothing.  The result is suitable for direct insertion into
    ``scene_state_summary['frame_actions']``.
    """
    if not scene_frames or len(scene_frames) < 2:
        return []

    ordered = sorted(scene_frames, key=lambda f: int(f.get("frame_index", 0)))
    actions: list[dict] = []

    for i in range(1, len(ordered)):
        prev = ordered[i - 1]
        curr = ordered[i]

        prev_ocr = prev.get("extracted_text", "") or ""
        curr_ocr = curr.get("extracted_text", "") or ""

        prev_ui = _ui_element_set(prev)
        curr_ui = _ui_element_set(curr)

        try:
            curr_conf = float(curr.get("ocr_confidence", 0.5))
        except (TypeError, ValueError):
            curr_conf = 0.5

        kind, delta, confidence, evidence = _classify_action(
            prev_ocr, curr_ocr, prev_ui, curr_ui, curr_conf,
        )
        if not kind:
            continue

        # Resolve timestamps: prefer seconds, fall back to ms / index.
        def _ts_s(f: dict) -> float:
            v = f.get("timestamp_seconds")
            if v is not None:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    pass
            v = f.get("timestamp_ms")
            if v is not None:
                try:
                    return float(v) / 1000.0
                except (TypeError, ValueError):
                    pass
            return float(f.get("frame_index", 0))

        target = _best_target_label(
            _filter_meaningful_tokens(_structural_fingerprint(curr_ocr)
                                      - _structural_fingerprint(prev_ocr)),
            prev_ui, curr_ui,
        )

        actions.append({
            "from_frame_index": int(prev.get("frame_index", 0)),
            "to_frame_index": int(curr.get("frame_index", 0)),
            "from_timestamp_s": round(_ts_s(prev), 3),
            "to_timestamp_s": round(_ts_s(curr), 3),
            "action_kind": kind,
            "delta_text": delta[:200],
            "target_label": target[:80],
            "confidence": round(confidence, 3),
            "evidence_signals": evidence,
        })

    return actions
