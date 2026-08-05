"""R5 — Vision Medic: multimodal escalation for DOM-opaque controls.

For controls the accessibility tree cannot describe — canvas elements, unlabeled
custom widgets, iframes without accessible names — the vision medic receives a
page SCREENSHOT + the control's bounding box and proposes a click region.

THE SAFETY CONTRACT:

  1. **Flag-gated:** ``QEC_CRAWL_VISION_ENABLED`` OFF by default, per-tenant.
     Enabled crawls carry the flag in their dispatch payload; the explorer's
     oracle factory instantiates the vision oracle ONLY when the flag is True.
  2. **Call-bounded:** hard cap per crawl (``QEC_VISION_MAX_CALLS``, default 10).
     Circuit breaker after consecutive failures (``QEC_VISION_BREAKER``, 3).
  3. **R0 verification:** the explorer clicks the proposed region and R0 verifies
     the resulting intent — a vision pick is accepted ONLY when R0 confirms.
     This module proposes; it never asserts success.
  4. **Refuse without orthogonal oracle:** governed by the ``hard_ui_healing_research``
     law. A vision pick without R0 verification is REFUSED.

CLASSIFICATION:

  A control is a vision candidate when it is DOM-opaque: the accessibility tree
  either names it generically (no accessible name, role=``generic`` / ``none``),
  wraps a ``<canvas>`` or ``<svg>`` that drives interaction, or lives inside an
  ``<iframe>`` whose cross-origin policy blocks inspection.

VOCABULARY (tighter than the text medic — only spatial actions make sense):

  * ``click_region``   — click at the proposed (x, y) within the element bbox.
  * ``display_only``   — the region is read-only / decorative — skip honestly.
  * ``unavailable``    — the vision medic cannot determine an action.

NEVER raises.  Pure functions + an async top-level that calls the LLM.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# ── status vocabulary ────────────────────────────────────────────────────────
STATUS_PROPOSED = "proposed"
STATUS_DISPLAY_ONLY = "display_only"
STATUS_UNAVAILABLE = "unavailable"

ACTION_CLICK_REGION = "click_region"
ACTION_DISPLAY_ONLY = "display_only"

VOCABULARY = frozenset({ACTION_CLICK_REGION, ACTION_DISPLAY_ONLY, "unavailable"})

# ── feature flag defaults ────────────────────────────────────────────────────
DEFAULT_MAX_CALLS = 10
DEFAULT_BREAKER_THRESHOLD = 3

# ── DOM-opaque classification ────────────────────────────────────────────────
_OPAQUE_TAGS = frozenset({"canvas", "svg"})
_OPAQUE_ROLES = frozenset({"generic", "none", "presentation", ""})


SYSTEM = (
    "You are a visual UI interaction specialist. A test crawler encountered a "
    "control it cannot inspect through the accessibility tree — it is visually "
    "rendered but DOM-opaque. You receive a screenshot of the page and the "
    "bounding box of the element.\n\n"
    "Your job: identify what the element IS and propose where to click to "
    "operate it. Reply with STRICT JSON:\n"
    '  {"action": "click_region", "x": <int>, "y": <int>, '
    '"reason": "<what you see>"}\n'
    "or\n"
    '  {"action": "display_only", "reason": "<why it is not interactive>"}\n'
    "or\n"
    '  {"action": "unavailable", "reason": "<why you cannot determine>"}\n\n'
    "Coordinates are RELATIVE to the element's bounding box (0,0 = top-left "
    "of the element). Reply ONLY with the JSON object, no prose."
)


@dataclass(frozen=True)
class VisionDecision:
    """One vision medic consultation outcome.

    ``click_x`` / ``click_y`` are meaningful only when ``status == proposed``
    and ``action == click_region``.  They are relative to the element bbox.
    """
    status: str
    action: str = ""
    click_x: int = 0
    click_y: int = 0
    reason: str = ""


# ── classification ───────────────────────────────────────────────────────────

def is_vision_candidate(control: dict) -> dict:
    """Classify whether a control needs vision escalation.

    Returns ``{candidate: bool, surface_type: str, reason: str}``:

      * ``canvas``    — a ``<canvas>`` element that may be interactive.
      * ``svg``       — an ``<svg>`` element driving interaction.
      * ``iframe``    — cross-origin iframe blocking inspection.
      * ``unlabeled`` — no accessible name AND an opaque role.
      * ``dom``       — a normal DOM control (NOT a vision candidate).
    """
    if not control:
        return {"candidate": False, "surface_type": "dom", "reason": "no control"}

    tag = str(control.get("tag") or "").strip().lower()
    role = str(control.get("role") or "").strip().lower()
    name = str(control.get("name") or "").strip()

    if tag in _OPAQUE_TAGS:
        return {"candidate": True, "surface_type": tag,
                "reason": f"<{tag}> element — DOM-opaque, needs visual inspection"}

    if tag == "iframe":
        cross_origin = bool(control.get("cross_origin"))
        if cross_origin or not name:
            return {"candidate": True, "surface_type": "iframe",
                    "reason": "cross-origin or unnamed iframe — cannot inspect"}

    if not name and role in _OPAQUE_ROLES:
        return {"candidate": True, "surface_type": "unlabeled",
                "reason": f"no accessible name, role={role or 'none'} — DOM-opaque"}

    return {"candidate": False, "surface_type": "dom",
            "reason": "normal DOM control — use ladder/medic"}


# ── prompt building ──────────────────────────────────────────────────────────

def build_vision_prompt(
    *, control: dict, element_bbox: dict, page_context: dict,
) -> str:
    """Build the text portion of the vision medic prompt.

    The screenshot is sent as a separate image attachment (multimodal).
    This returns the text context that accompanies it.
    """
    lines = ["DOM-opaque control that needs visual inspection:"]
    lines.append(f"  Tag: <{control.get('tag', '?')}>")
    if control.get("name"):
        lines.append(f"  Name: {control['name']}")
    if control.get("role"):
        lines.append(f"  Role: {control['role']}")
    if control.get("kind"):
        lines.append(f"  Kind: {control['kind']}")
    if control.get("css_hint"):
        lines.append(f"  CSS: {control['css_hint']}")
    attrs = control.get("attributes") or {}
    for k, v in list(attrs.items())[:8]:
        lines.append(f"  Attr {k}: {v}")
    lines.append("")

    bbox = element_bbox or {}
    if bbox:
        lines.append(f"Element bounding box: x={bbox.get('x', 0)}, "
                      f"y={bbox.get('y', 0)}, "
                      f"width={bbox.get('width', 0)}, "
                      f"height={bbox.get('height', 0)}")
        lines.append("")

    ctx = page_context or {}
    if ctx.get("title"):
        lines.append(f"Page: {ctx['title']}")
    if ctx.get("url"):
        lines.append(f"URL: {str(ctx['url']).split('?')[0]}")
    lines.append("")
    lines.append(
        "Look at the screenshot. The highlighted region is the element. "
        "What is it? If it is interactive, propose where to click "
        "(coordinates relative to the element's top-left corner)."
    )
    return "\n".join(lines)


# ── proposal parsing ─────────────────────────────────────────────────────────

def parse_vision_proposal(raw: Any) -> VisionDecision:
    """Tolerantly parse the LLM vision response.

    Accepts a JSON string or dict.  Extracts ``action``, ``x``, ``y``,
    ``reason``.  Unrecognised actions → ``unavailable``.  Never raises.
    """
    if isinstance(raw, dict):
        obj = dict(raw)
    else:
        text = str(raw or "").strip()
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", text).strip()
        try:
            obj = json.loads(text)
        except Exception:
            m = re.search(r"\{.*\}", text, re.DOTALL)
            try:
                obj = json.loads(m.group(0)) if m else {}
            except Exception:
                obj = {}
    if not isinstance(obj, dict):
        obj = {}

    action = str(obj.get("action") or "").strip().lower()
    reason = str(obj.get("reason") or "")[:300]

    if action == ACTION_CLICK_REGION:
        try:
            x = int(obj.get("x", 0))
            y = int(obj.get("y", 0))
        except (TypeError, ValueError):
            return VisionDecision(status=STATUS_UNAVAILABLE,
                                  reason="invalid coordinates in proposal")
        return VisionDecision(status=STATUS_PROPOSED, action=ACTION_CLICK_REGION,
                              click_x=x, click_y=y, reason=reason)

    if action == ACTION_DISPLAY_ONLY:
        return VisionDecision(status=STATUS_DISPLAY_ONLY, action=ACTION_DISPLAY_ONLY,
                              reason=reason)

    return VisionDecision(status=STATUS_UNAVAILABLE, reason=reason or "unrecognized action")


# ── proposal validation ──────────────────────────────────────────────────────

def validate_proposal(
    proposal: VisionDecision, element_bbox: dict,
) -> dict:
    """Sanity-check a vision proposal against the element's bounding box.

    Returns ``{valid: bool, reason: str}``.  A click outside the element
    bbox is REJECTED (the model hallucinated a region that doesn't exist).
    """
    if proposal.status != STATUS_PROPOSED:
        return {"valid": True, "reason": "non-click action, no validation needed"}

    bbox = element_bbox or {}
    width = int(bbox.get("width", 0))
    height = int(bbox.get("height", 0))

    if width <= 0 or height <= 0:
        return {"valid": False, "reason": "element has zero/negative dimensions"}

    x, y = proposal.click_x, proposal.click_y
    if x < 0 or y < 0:
        return {"valid": False, "reason": f"negative coordinates ({x}, {y})"}
    if x > width:
        return {"valid": False,
                "reason": f"x={x} exceeds element width={width}"}
    if y > height:
        return {"valid": False,
                "reason": f"y={y} exceeds element height={height}"}

    return {"valid": True, "reason": "coordinates within element bounds"}


# ── top-level consultation ───────────────────────────────────────────────────

async def consult_vision(
    *, tenant_id: str, control: dict, screenshot_b64: str,
    element_bbox: dict, page_context: dict,
    propose_fn=None,
) -> VisionDecision:
    """Ask the vision medic how to operate a DOM-opaque control.

    ``propose_fn(prompt, screenshot_b64) → str|dict`` performs the multimodal
    LLM inference.  Injected so the service is unit-testable with a fake.
    When ``None`` or failing, returns ``unavailable`` (honest degradation).

    Never raises.
    """
    if not control:
        return VisionDecision(status=STATUS_UNAVAILABLE, reason="no control")

    classification = is_vision_candidate(control)
    if not classification["candidate"]:
        return VisionDecision(status=STATUS_UNAVAILABLE,
                              reason="not a vision candidate")

    if not screenshot_b64:
        return VisionDecision(status=STATUS_UNAVAILABLE,
                              reason="no screenshot provided")

    prompt = build_vision_prompt(
        control=control, element_bbox=element_bbox,
        page_context=page_context,
    )

    if propose_fn is None:
        return VisionDecision(status=STATUS_UNAVAILABLE,
                              reason="no vision LLM configured")

    try:
        raw = await propose_fn(prompt, screenshot_b64)
    except Exception as exc:
        logger.warning("qec.vision_medic.propose_failed tenant=%s error=%s",
                       tenant_id, str(exc)[:200])
        return VisionDecision(status=STATUS_UNAVAILABLE,
                              reason=f"vision LLM failed: {str(exc)[:200]}")

    proposal = parse_vision_proposal(raw)

    if proposal.status == STATUS_PROPOSED:
        check = validate_proposal(proposal, element_bbox)
        if not check["valid"]:
            logger.info("qec.vision_medic.proposal_rejected tenant=%s reason=%s",
                        tenant_id, check["reason"])
            return VisionDecision(status=STATUS_UNAVAILABLE,
                                  reason=f"proposal rejected: {check['reason']}")

    return proposal


__all__ = [
    "SYSTEM",
    "STATUS_PROPOSED",
    "STATUS_DISPLAY_ONLY",
    "STATUS_UNAVAILABLE",
    "ACTION_CLICK_REGION",
    "ACTION_DISPLAY_ONLY",
    "VOCABULARY",
    "DEFAULT_MAX_CALLS",
    "DEFAULT_BREAKER_THRESHOLD",
    "VisionDecision",
    "is_vision_candidate",
    "build_vision_prompt",
    "parse_vision_proposal",
    "validate_proposal",
    "consult_vision",
]
