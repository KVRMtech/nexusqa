"""Visual locate — VLM pixel-center proposal for canvas / no-DOM controls (P5-FULL).

When a failing step targets a control that has NO accessibility node and NO DOM
(``CANVAS_NO_DOM`` — a <canvas>, WebGL, Flutter-web, or a fully custom-painted
widget), the deterministic resolvers (similo over the a11y tree) and the agentic
re-anchor (which also binds BY NAME from the live a11y snapshot) have nothing to
bind to. The only remaining signal is the *pixels*.

This module asks a VISION-capable LLM tier for the on-screen pixel CENTER of the
described control over a base64 page screenshot, and returns
``{x, y, confidence, rationale}`` or ``None``.

CRITICAL — this module is ONLY a PROPOSER. It is NEVER an oracle.
========================================================================
A returned coordinate is a *hypothesis*, not a heal. The caller (the auto-heal
loop's CANVAS branch) must:
  1. be opt-in gated (``NEXUS_VISUAL_HEAL_ENABLED=1``, DEFAULT OFF), and
  2. compile the coordinate into a ``page.mouse.click(x, y)`` step that STILL
     emits the step's own outcome oracle, and
  3. RE-RUN and only accept it if it PROVES GREEN past that orthogonal oracle.

A wrong VLM coordinate therefore clicks empty pixels / the wrong widget, the
step's own outcome assertion fails RED, and the loop REFUSES — falling back to
the honest ``CANVAS_NO_DOM`` diagnosis. The VLM can never green-wash because it
never gets to declare green. That is the whole design: the existing prove-green
gate is the oracle; this is opt-in, bounded, fail-closed proposal only.

Fail-closed contract — ``locate(...)`` returns ``None`` (=> caller REFUSES) on:
  * no LLM configured / provider error / router exception,
  * no tool call from the model,
  * a parse / schema / type failure,
  * confidence below ``min_confidence``,
  * a coordinate outside the reported viewport (a hallucinated point),
  * a missing or empty screenshot.

It NEVER raises into the heal loop and NEVER returns a coordinate it is unsure
about. One bounded LLM call, deterministic prompt (temperature 0.0), forced
tool-use so the output is schema-validated JSON, not prose.
"""
from __future__ import annotations

import base64
import binascii
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Default acceptance floor. A canvas mis-click is the #1 false-heal we are
# guarding against, so the bar is deliberately high. The oracle (prove-green)
# is the real gate, but we refuse outright below this rather than spend a
# re-run on a coordinate the model itself isn't sure about.
_DEFAULT_MIN_CONFIDENCE = 0.75

# Bound the single call hard — this runs inside the heal loop.
_DEFAULT_MAX_TOKENS = 500
_DEFAULT_TIMEOUT_S = 40.0

# Default vision tier. tier_premium is the product's vision-capable reasoning
# tier (see services/llm/config.py example + page_visit/agentic_heal callers).
_DEFAULT_TIER = "tier_premium"


def locate_tool():
    """Forced-tool schema: the model MUST return a structured locate result.

    Forcing tool-use (rather than parsing prose) means the output is already
    JSON-shaped and validated by the provider against this schema. ``found``
    lets the model honestly say "I cannot see this control" instead of being
    pushed into hallucinating a coordinate.
    """
    from ..llm.types import ToolDefinition

    return ToolDefinition(
        name="report_control_location",
        description=(
            "Report the on-screen pixel CENTER of the single described UI control "
            "in the provided screenshot. If the control is not visibly present, set "
            "found=false and do NOT guess a coordinate. Coordinates are in CSS "
            "pixels measured from the TOP-LEFT of the screenshot (x rightward, y "
            "downward), and MUST fall inside the given viewport."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "found": {
                    "type": "boolean",
                    "description": "True only if you can clearly see the described control.",
                },
                "x": {
                    "type": "number",
                    "description": "Pixel x of the control's center (0 = left edge).",
                },
                "y": {
                    "type": "number",
                    "description": "Pixel y of the control's center (0 = top edge).",
                },
                "confidence": {
                    "type": "number",
                    "description": "0.0-1.0 — how sure you are this is the exact control.",
                },
                "rationale": {
                    "type": "string",
                    "description": "One short sentence: what you saw and where.",
                },
            },
            "required": ["found", "confidence", "rationale"],
        },
    )


def build_prompt(*, description: str, value: str, viewport: dict) -> tuple[str, str]:
    """Deterministic system + user prompt for the locate call."""
    vw = _coerce_int((viewport or {}).get("width"))
    vh = _coerce_int((viewport or {}).get("height"))
    system = (
        "You are a precise visual UI locator for an automated end-to-end test. You are "
        "given ONE screenshot of an application page and a description of a single control "
        "that has no accessibility metadata (it is painted on a canvas / WebGL / custom "
        "widget). Find that control in the image and report the pixel coordinate of its "
        "CENTER.\n\n"
        "HARD RULES:\n"
        "- Coordinates are CSS pixels from the TOP-LEFT corner: x increases to the right, "
        "y increases downward.\n"
        f"- The screenshot viewport is {vw} wide by {vh} tall. Your coordinate MUST be "
        "inside it.\n"
        "- Aim for the visual CENTER of the clickable target, not its edge or label margin.\n"
        "- If you cannot clearly see the described control, set found=false and do not "
        "invent a coordinate. A wrong coordinate is worse than admitting you can't see it.\n"
        "- Report honest confidence: only use >0.75 when you are sure this is the exact "
        "control the description refers to.\n"
        "Call report_control_location exactly once."
    )
    lines = [
        "Locate this control in the screenshot:",
        f"  description: {description or '(unspecified)'}",
    ]
    if value:
        lines.append(
            f"  intended value/target: {value}  "
            "(the test wants to act on the control that produces/selects this)"
        )
    lines.append(f"  viewport: {vw} x {vh} px")
    lines.append(
        "\nReturn the pixel CENTER of that control, or found=false if it is not visible."
    )
    return system, "\n".join(lines)


def _coerce_int(v: Any) -> int:
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return 0


def _coerce_float(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _decode_screenshot(screenshot_b64: str) -> Optional[bytes]:
    """Decode the base64 screenshot to bytes, tolerating a data-URL prefix."""
    if not screenshot_b64 or not isinstance(screenshot_b64, str):
        return None
    raw = screenshot_b64.strip()
    if raw.startswith("data:"):
        # data:image/png;base64,XXXX
        comma = raw.find(",")
        if comma == -1:
            return None
        raw = raw[comma + 1 :]
    try:
        data = base64.b64decode(raw, validate=False)
    except (binascii.Error, ValueError):
        return None
    return data or None


def validate_result(
    raw: dict,
    *,
    viewport: dict,
    min_confidence: float,
) -> Optional[dict]:
    """Turn the model's tool arguments into a trusted ``{x,y,confidence,rationale}``.

    Returns ``None`` (=> REFUSE) on ANY of: not found, missing/non-numeric coords,
    low confidence, or a coordinate outside the viewport (a hallucination). This is
    the schema/anti-hallucination boundary BEFORE the oracle ever runs.
    """
    if not isinstance(raw, dict):
        return None
    if not bool(raw.get("found")):
        return None

    conf = _coerce_float(raw.get("confidence"))
    if conf is None or conf < min_confidence:
        return None

    x = _coerce_float(raw.get("x"))
    y = _coerce_float(raw.get("y"))
    if x is None or y is None:
        return None

    vw = _coerce_int((viewport or {}).get("width"))
    vh = _coerce_int((viewport or {}).get("height"))
    # A coordinate outside the reported viewport is a hallucination — refuse it
    # rather than clicking off-screen. Require a positive, known viewport; if we
    # don't know the bounds we cannot validate, so we refuse (fail-closed).
    if vw <= 0 or vh <= 0:
        return None
    if not (0.0 <= x <= float(vw)) or not (0.0 <= y <= float(vh)):
        return None

    rationale = (raw.get("rationale") or "").strip()[:300]
    return {
        "x": int(round(x)),
        "y": int(round(y)),
        "confidence": round(conf, 4),
        "rationale": rationale,
    }


async def locate(
    *,
    screenshot_b64: str,
    description: str,
    value: str = "",
    viewport: dict | None = None,
    router=None,
    tier_name: str = _DEFAULT_TIER,
    min_confidence: float = _DEFAULT_MIN_CONFIDENCE,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
) -> Optional[dict]:
    """Ask the vision LLM for the pixel center of the described canvas control.

    Returns ``{x, y, confidence, rationale}`` (a PROPOSAL the caller must still
    prove green), or ``None`` to REFUSE. Never raises into the heal loop.

    Fail-closed on: no screenshot, no LLM configured, provider error, no tool
    call, parse/schema failure, low confidence, or out-of-viewport coordinate.
    """
    viewport = viewport or {}
    image_bytes = _decode_screenshot(screenshot_b64)
    if not image_bytes:
        logger.info("visual_locate.refuse", extra={"reason": "no_screenshot"})
        return None

    try:
        from ..llm.router import build_router
        from ..llm.types import CompletionRequest, FinishReason, ImageContent

        r = router or build_router()
        system, prompt = build_prompt(
            description=description, value=value, viewport=viewport
        )
        tool = locate_tool()
        media_type = _sniff_media_type(image_bytes)
        req = CompletionRequest(
            system=system,
            prompt=prompt,
            max_tokens=max_tokens,
            temperature=0.0,
            request_timeout_s=timeout_s,
            images=(ImageContent(data=image_bytes, media_type=media_type),),
            tools=(tool,),
            tool_choice=tool.name,
            metadata={"task": "visual_locate"},
        )
        resp = await r.complete_via_tier(tier_name=tier_name, request=req)
        if resp.finish_reason == FinishReason.ERROR or not resp.tool_calls:
            logger.info(
                "visual_locate.refuse",
                extra={"reason": "no_tool_call", "detail": (resp.error_detail or "")[:200]},
            )
            return None
        raw = resp.tool_calls[0].arguments or {}
        result = validate_result(raw, viewport=viewport, min_confidence=min_confidence)
        if result is None:
            logger.info(
                "visual_locate.refuse",
                extra={"reason": "low_confidence_or_out_of_bounds",
                       "found": bool(raw.get("found")),
                       "confidence": raw.get("confidence")},
            )
            return None
        result["model"] = getattr(resp, "model", "")
        logger.info(
            "visual_locate.proposed",
            extra={"x": result["x"], "y": result["y"],
                   "confidence": result["confidence"], "model": result["model"]},
        )
        return result
    except Exception as exc:  # never crash the heal loop
        logger.warning(
            "visual_locate.error",
            extra={"error": f"{type(exc).__name__}: {str(exc)[:200]}"},
        )
        return None


def _sniff_media_type(data: bytes) -> str:
    """Best-effort image MIME sniff (PNG vs JPEG vs WEBP), defaulting to PNG.

    page.screenshot() emits PNG by default, but we sniff so a JPEG-typed
    screenshot is declared correctly to the provider (some reject a mismatched
    media_type) rather than silently failing the vision call.
    """
    if len(data) >= 3 and data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if len(data) >= 6 and data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return "image/png"


__all__ = ["locate", "locate_tool", "build_prompt", "validate_result"]
