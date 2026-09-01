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

import hashlib
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


# ── page Perceiver (U2/G2): enumerate controls + outcomes from a screenshot ─────

PERCEIVE_SYSTEM = (
    "You are a UI perception engine. You receive a SCREENSHOT of a page whose "
    "controls the DOM could not read (canvas / Flutter Web / WebGL). Enumerate the "
    "INTERACTIVE controls you can see, and any DISPLAYED OUTCOME values (a total, a "
    "decision, a reference/policy number). Reply with STRICT JSON only:\n"
    '{"controls":[{"label":"...","role":"button|textbox|checkbox|link",'
    '"bbox":[x,y,w,h],"click_x":<int>,"click_y":<int>}],'
    '"displayed_values":[{"label":"...","text":"..."}]}\n'
    "Coordinates are PAGE pixels. Do NOT invent controls you cannot see; an empty "
    "list is a valid, honest answer."
)


# ── M3.1 / T-VIS-03 · EXACTLY ONE AUTHORITATIVE SYSTEM PROMPT PER TASK ────────
#
# THE CONTRADICTION THIS RESOLVES.  ``/internal/perceive-controls`` sent
# ``system=vision_medic.SYSTEM`` — the MEDIC prompt, which demands
# ``{"action":"click_region","x":…,"y":…}`` — while ``perceive_controls`` built
# ``PERCEIVE_SYSTEM`` (a ``{"controls":[…]}`` contract) into the USER prompt.
# Every perceive call therefore carried two mutually exclusive output contracts,
# one in each channel, and which one the model obeyed was a property of the
# provider rather than of this codebase.  ``parse_perceived`` then quietly
# returned empty lists for any reply that followed the system prompt — so the
# failure mode was "vision found nothing", the single most plausible-looking
# outcome there is.
#
# The resolution is a MAPPING, not a convention: the system prompt is selected
# by task from one frozen table, unknown tasks RAISE rather than defaulting, and
# the user prompt no longer restates any contract.  There is now exactly one
# place where "which prompt does this task use" is answered.

#: The two tasks that reach a vision model.  Same strings ``platform_api``
#: bills against, so the prompt and the cost attribution cannot disagree.
TASK_VISION_MEDIC = "vision_medic"
TASK_VISION_PERCEIVE = "vision_perceive"

_SYSTEM_BY_TASK: dict[str, str] = {
    TASK_VISION_MEDIC: SYSTEM,
    TASK_VISION_PERCEIVE: PERCEIVE_SYSTEM,
}


def system_prompt_for(task: str) -> str:
    """THE authoritative system prompt for ``task``.  Fail-closed.

    An unknown task RAISES.  A default here would be a silent third contract,
    which is the class of bug this function exists to end.
    """
    key = str(task or "").strip()
    if key not in _SYSTEM_BY_TASK:
        raise ValueError(
            "no authoritative vision system prompt for task %r (known: %s)"
            % (key, ", ".join(sorted(_SYSTEM_BY_TASK))))
    return _SYSTEM_BY_TASK[key]


def effective_prompt(task: str) -> dict:
    """What WILL be sent, and its digest — the inspectability half of T-VIS-03.

    Returned on every vision response so an operator can prove which prompt a
    given crawl actually ran under instead of reading the deploy and hoping.
    """
    system = system_prompt_for(task)
    return {"task": str(task), "system_sha256": hashlib.sha256(
        system.encode("utf-8")).hexdigest(), "system_chars": len(system)}


def build_perceive_prompt(page_context: dict) -> str:
    """The USER half of a perceive call: page context only.

    Deliberately carries NO output contract.  The contract lives in exactly one
    place — :data:`PERCEIVE_SYSTEM`, reached through :func:`system_prompt_for` —
    and restating it here is how the two came to disagree.
    """
    ctx = page_context or {}
    hint = str(ctx.get("url") or "")[:200]
    return ("Page: " + hint) if hint else "Enumerate the controls in this screenshot."


def _coerce_int(v: Any) -> Any:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def parse_perceived(raw: Any) -> dict:
    """Tolerantly parse the VLM page-perception → ``{controls, displayed_values}``.

    Clamps/validates each entry; a missing click point defaults to the bbox
    center; malformed output → empties. Never raises.
    """
    if isinstance(raw, dict):
        obj: Any = dict(raw)
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

    controls: list[dict] = []
    for c in (obj.get("controls") or [])[:64]:
        if not isinstance(c, dict):
            continue
        label = str(c.get("label") or "").strip()[:160]
        role = str(c.get("role") or "button").strip().lower()[:40]
        raw_bbox = c.get("bbox") or [0, 0, 0, 0]
        try:
            bbox = [int(raw_bbox[0]), int(raw_bbox[1]), int(raw_bbox[2]), int(raw_bbox[3])]
        except (TypeError, ValueError, IndexError):
            bbox = [0, 0, 0, 0]
        cx, cy = _coerce_int(c.get("click_x")), _coerce_int(c.get("click_y"))
        if cx is None:
            cx = bbox[0] + bbox[2] // 2
        if cy is None:
            cy = bbox[1] + bbox[3] // 2
        if not label and bbox == [0, 0, 0, 0]:
            continue
        controls.append({"label": label, "role": role, "bbox": bbox,
                         "click_x": cx, "click_y": cy})

    values: list[dict] = []
    for v in (obj.get("displayed_values") or [])[:32]:
        if not isinstance(v, dict):
            continue
        label = str(v.get("label") or "").strip()[:160]
        text = str(v.get("text") or v.get("value") or "").strip()[:200]
        if label or text:
            values.append({"label": label, "text": text})

    return {"controls": controls, "displayed_values": values}


async def perceive_controls(
    *, tenant_id: str, screenshot_b64: str, page_context: dict | None = None,
    propose_fn=None,
) -> dict:
    """The page Perceiver (U2): enumerate the interactive controls + displayed
    outcome values on a DOM-opaque page from its screenshot. Returns
    ``{controls, displayed_values}``. Same injectable-``propose_fn`` design as
    ``consult_vision`` (unit-testable with a fake). Never raises; honest
    degradation → empty lists.
    """
    if not screenshot_b64 or propose_fn is None:
        return {"controls": [], "displayed_values": []}
    prompt = build_perceive_prompt(page_context or {})
    try:
        raw = await propose_fn(prompt, screenshot_b64)
    except Exception as exc:
        logger.warning("qec.vision_medic.perceive_failed tenant=%s error=%s",
                       tenant_id, str(exc)[:200])
        return {"controls": [], "displayed_values": []}
    return parse_perceived(raw)


__all__ = [
    "SYSTEM",
    "PERCEIVE_SYSTEM",
    "TASK_VISION_MEDIC",
    "TASK_VISION_PERCEIVE",
    "system_prompt_for",
    "effective_prompt",
    "perceive_controls",
    "parse_perceived",
    "build_perceive_prompt",
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
