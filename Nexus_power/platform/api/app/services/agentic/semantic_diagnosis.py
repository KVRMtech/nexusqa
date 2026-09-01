"""Context — the Cross-Field / Business-Logic Data-Validity Reasoner (the flagship).

Generic + additive + NEVER green-wash. When a step fails on an option-bearing control
and the recorded value is NOT among the control's LIVE options, an LLM reasons across
the recorded SIBLING field values (the controlling context) + the LIVE option set + its
own world knowledge, to explain the inconsistency in plain English and SUGGEST a fix —
e.g. a value chosen earlier constrains this control's valid options, and the recorded
value is invalid for it.

HARD never-green-wash boundaries (inherited from agentic_heal's discipline):
  * INERT unless a genuinely-live option set was captured (no live options -> no claim).
  * the inconsistency is only asserted when the recorded value is GENUINELY ABSENT from
    the live options (a live-confirmed fact, not an LLM guess).
  * any suggested replacement VALUE must appear VERBATIM in the live options or it is
    DROPPED (the LLM cannot fabricate a valid value).
  * it NEVER applies a fix and NEVER flips a verdict to green — it only DIAGNOSES and
    SUGGESTS; the orthogonal oracle still disposes.
  * on ANY error / no-LLM / low-confidence / no-inconsistency -> returns None and the
    deterministic diagnosis stands unchanged.

Domain-agnostic: no hardcoded fields, values, or vocabulary.
"""
from __future__ import annotations

from . import governor
from . import live_options as eyes

_MIN_CONFIDENCE = 0.7


def _tool():
    from ..llm.types import ToolDefinition
    return ToolDefinition(
        name="record_data_validity",
        description=(
            "Record whether the failing control's recorded value is INVALID given another "
            "field's recorded value (a cross-field / business-logic data inconsistency). You "
            "may ONLY propose a suggested_value that appears VERBATIM in LIVE_OPTIONS; if none "
            "fits, leave it empty and propose an upstream fix instead. Never invent a value or "
            "a fact you cannot see in the inputs."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "is_cross_field_inconsistency": {"type": "boolean"},
                "controlling_field": {"type": "string",
                                      "description": "the earlier field whose value constrains this control"},
                "controlling_value": {"type": "string"},
                "invalid_value": {"type": "string", "description": "the recorded value that is invalid here"},
                "suggested_value": {"type": "string",
                                    "description": "a replacement that MUST appear verbatim in LIVE_OPTIONS, else empty"},
                "alternative_upstream_fix": {"type": "string",
                                             "description": "e.g. change the controlling field instead"},
                "human_explanation": {"type": "string",
                                      "description": "one or two plain-English sentences a senior QE analyst would say"},
                "confidence": {"type": "number"},
            },
            "required": ["is_cross_field_inconsistency", "human_explanation", "confidence"],
        },
    )


def _prompt(*, failing_label, failing_value, sibling_fields, options):
    system = (
        "You are a senior QE analyst reviewing why an end-to-end test step failed. You are "
        "given the failing control's recorded label and value, the OTHER fields the user "
        "already filled earlier in the flow (which may constrain this control), and the "
        "LIVE_OPTIONS the control actually offers on the page. Decide whether the recorded "
        "value is INVALID given an earlier field's value (a cross-field data inconsistency).\n"
        "HARD rules: only name a suggested_value that appears VERBATIM in LIVE_OPTIONS; if "
        "none fits, leave suggested_value empty and give an upstream fix. Never invent a value "
        "or a fact you cannot see. If there is no genuine cross-field inconsistency, set "
        "is_cross_field_inconsistency=false. Call record_data_validity exactly once."
    )
    lines = [
        f"FAILING CONTROL: label='{failing_label}', recorded value='{failing_value}'.",
        "OTHER FIELDS ALREADY FILLED (earlier in the flow):",
    ]
    if sibling_fields:
        for s in sibling_fields:
            lines.append(f"  - {s.get('label', '')} = '{s.get('value', '')}'")
    else:
        lines.append("  (none)")
    lines.append("LIVE_OPTIONS the failing control offers on the page:")
    for o in (options or []):
        lines.append(f"  - {o}")
    return system, "\n".join(lines)


async def analyze(*, failing_label, failing_value, sibling_fields, nodes,
                  router=None, tier_name: str = "tier_premium", budget=None,
                  min_confidence: float = _MIN_CONFIDENCE) -> dict | None:
    """Run the cross-field reasoner ONCE. Returns a provenance-stamped semantic dict to
    attach to the diagnosis, or None when it must stay inert (no live options, no budget,
    no LLM, low confidence, ungrounded, or no genuine inconsistency)."""
    # GROUNDING GATE 1 — harvest the live option set; inert if absent.
    options = eyes.live_options(nodes, exclude=failing_value)
    if not options:
        return None
    # GROUNDING GATE 2 — the inconsistency is only real if the recorded value is
    # GENUINELY absent from the live options (a live-confirmed fact, not a guess).
    if eyes.value_in_options(failing_value, options):
        return None
    # BUDGET GATE — dedupe + per-run cap (the cheap deterministic diagnosis already ran).
    if budget is not None and not budget.allow("context", failing_label, failing_value, len(options)):
        return None
    try:
        from ..llm.router import build_router
        from ..llm.types import CompletionRequest, FinishReason
        r = router or build_router()
        system, prompt = _prompt(failing_label=failing_label, failing_value=failing_value,
                                 sibling_fields=sibling_fields, options=options)
        tool = _tool()
        req = CompletionRequest(system=system, prompt=prompt, max_tokens=900, temperature=0.0,
                                request_timeout_s=40.0, tools=(tool,), tool_choice=tool.name,
                                metadata={"task": "context_cross_field"})
        resp = await r.complete_via_tier(tier_name=tier_name, request=req)
        if resp.finish_reason == FinishReason.ERROR or not resp.tool_calls:
            return None
        a = resp.tool_calls[0].arguments or {}
        if not a.get("is_cross_field_inconsistency"):
            return None
        try:
            conf = float(a.get("confidence") or 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        if conf < min_confidence:
            return None
        # ANTI-FABRICATION — a suggested replacement VALUE must be verbatim in live options.
        suggested = (a.get("suggested_value") or "").strip()
        grounded_suggestion = suggested if eyes.value_in_options(suggested, options) else ""
        human = (a.get("human_explanation") or "").strip()
        stamped = governor.stamp(
            human,
            grounding=governor.G_LIVE_CONFIRMED,
            facts=[
                f"recorded value '{failing_value}' is NOT among the {len(options)} live options",
                *([f"a valid live option is '{grounded_suggestion}'"] if grounded_suggestion else []),
            ],
        )
        return {
            "cause": "DATA_VALIDITY_CROSS_FIELD",
            "cause_label": "Cross-field data inconsistency",
            "is_cross_field_inconsistency": True,
            "controlling_field": (a.get("controlling_field") or "").strip(),
            "controlling_value": (a.get("controlling_value") or "").strip(),
            "invalid_value": failing_value,
            "suggested_value": grounded_suggestion,          # verbatim-in-options or ""
            "alternative_upstream_fix": (a.get("alternative_upstream_fix") or "").strip(),
            "confidence": round(conf, 2),
            "auto_fixable": False,                            # NEVER auto-rewrite recorded data
            "provenance": stamped,
            "live_option_count": len(options),
            "model": getattr(resp, "model", ""),
        }
    except Exception:
        return None  # fail-open: the deterministic diagnosis stands unchanged
