"""Shared transition prompt + response parser used by every vision
provider that implements ``_do_analyze_transition``.

Centralising the prompt + parser here gives us:
  - One canonical taxonomy of action_kind values
  - Identical parsing semantics across Anthropic / OpenAI / Ollama
  - One place to evolve the prompt without sweeping 3 provider files

Providers still own the *image-encoding* + *API-payload-shape* parts —
those are necessarily provider-specific. The bookend (prompt + parser)
is the only piece that should be uniform.
"""

from __future__ import annotations

import json
from typing import Any

from ..base import VisionTransitionResponse


# Canonical action_kind taxonomy used by eyes-engine's downstream
# pipeline. Providers must restrict output to these strings; the
# parser collapses everything else to "unknown".
ACTION_KINDS: set[str] = {
    "click_cta", "enter_text", "select_option", "toggle",
    "navigate", "submit_form", "scroll", "unknown",
}


def build_transition_prompt(
    *, ocr_before: str, ocr_after: str, url_changed: bool,
) -> str:
    """Two-image transition prompt — what user action explains the
    change. Output shape is fixed JSON so the parser is deterministic
    across providers."""
    return (
        "You are given TWO screenshots in chronological order.\n"
        "Image 1 (first image) = UI state BEFORE the user's action.\n"
        "Image 2 (second image) = UI state AFTER the user's action.\n\n"
        f"OCR text from before (truncated): {(ocr_before or '')[:1200]}\n"
        f"OCR text from after  (truncated): {(ocr_after or '')[:1200]}\n"
        f"Visible URL or path changed between screens: "
        f"{'yes' if url_changed else 'no'}\n\n"
        "What single user action best explains the transition from "
        "Image 1 to Image 2?\n"
        "Choose action_kind from exactly one of:\n"
        "click_cta, enter_text, select_option, toggle, navigate, "
        "submit_form, scroll, unknown\n\n"
        "Respond ONLY with valid JSON:\n"
        "{\n"
        '  "action_kind": "...",\n'
        '  "action_label": "short phrase e.g. Clicked Continue",\n'
        '  "target_element_label": "button or field name if visible",\n'
        '  "observed_value": "value entered or selected, else empty string",\n'
        '  "confidence": 0.85,\n'
        '  "evidence_text": "one short sentence citing visible UI change"\n'
        "}\n"
    )


def parse_transition_response(
    *, content_str: str, raw: dict[str, Any], model: str,
) -> VisionTransitionResponse:
    """Parse a JSON-mode transition response. Rejects unknown
    action_kind values (collapses to "unknown"). Clamps confidence to
    [0.0, 1.0]. Empty / unparseable → "unknown" with confidence=0."""
    text = (content_str or "").strip()
    if not text:
        return VisionTransitionResponse(
            kind="unknown", confidence=0.0,
            model=model, raw_response=raw,
        )

    # Strip ```json fenced blocks some models emit despite JSON-mode.
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()

    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return VisionTransitionResponse(
            kind="unknown", confidence=0.0,
            evidence_text=text[:200],
            model=model, raw_response=raw,
        )
    if not isinstance(parsed, dict):
        return VisionTransitionResponse(
            kind="unknown", confidence=0.0,
            model=model, raw_response=raw,
        )

    raw_kind = str(parsed.get("action_kind") or "unknown").strip().lower()
    kind = raw_kind if raw_kind in ACTION_KINDS else "unknown"
    confidence = float(parsed.get("confidence") or 0.0)
    confidence = max(0.0, min(1.0, confidence))

    return VisionTransitionResponse(
        kind=kind,
        action_label=str(parsed.get("action_label") or "")[:200],
        target_element_label=str(parsed.get("target_element_label") or "")[:200],
        observed_value=str(parsed.get("observed_value") or "")[:500],
        confidence=confidence,
        evidence_text=str(parsed.get("evidence_text") or "")[:500],
        model=model,
        raw_response=raw,
    )
