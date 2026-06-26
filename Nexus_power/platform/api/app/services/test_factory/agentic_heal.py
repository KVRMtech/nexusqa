"""Agentic Auto-Heal — grounded LLM reasoning bounded by proof (never green-wash).

When the deterministic recipes can't resolve a failing step and the loop would
escalate to a human ("Analyze & fix"), this layer lets an LLM reason about the
failure LIKE A HUMAN — but it NEVER writes Playwright and NEVER declares green:

  1. INPUT is grounded only: the recorded step (label / kind / value / expected),
     the live error, and the LIVE accessibility controls captured from the page
     (heal_capture ``nodes`` — {name, role}).
  2. The agent PROPOSES one fix per failing step, choosing a target control BY
     NAME FROM THE LIVE SNAPSHOT. A pick whose name is not in the snapshot is
     DROPPED at validation — the agent cannot invent a selector.
  3. Each accepted proposal maps to an EXISTING additive override channel:
        rebind -> reanchors {name, kind}  — rename + control-kind in one move
                  (e.g. a value mistakenly bound as a checkbox -> the real
                  'Plan tier' chooser). The frozen compiler threads {name, kind}
                  through ``_action_lines`` so it re-points AND re-kinds the step.
        wait   -> waits  — a condition-based wait/scope recipe (built
                  deterministically by ``wait_scope_resolver``, never a sleep).
        refuse -> escalate honestly.
     The deterministic compiler emits the code; the step's OWN orthogonal oracle
     + the 2x green confirm still gate green. The agent proposes; the oracle disposes.

The differentiator: autonomous, human-like reasoning that is still PROVEN, not
green-washed. With no LLM configured, a provider error, or no tool call, it
grounds NOTHING and the caller escalates exactly as before — no fabrication, no
crash. The agent is also forbidden (by the caller) from touching the REFUSE
families (real regression / auth / data / variant): those are correct honest
refusals it must never paper over.
"""
from __future__ import annotations

from typing import Any

# ARIA role (live node) -> the compiler's control "kind" vocabulary.
_ROLE_TO_KIND = {
    "combobox": "select", "listbox": "select", "menu": "select",
    "radio": "radio", "radiogroup": "radio",
    "checkbox": "checkbox", "switch": "toggle",
    "button": "button", "link": "link",
    "textbox": "", "searchbox": "", "spinbutton": "",
}
_VALID_KINDS = {"select", "radio", "checkbox", "toggle", "button", "link", "textbox", ""}

# Causes the agent must NEVER try to "fix" — they are correct honest refusals.
# The caller also enforces this, but we keep the set here for callers/tests.
REFUSE_CAUSES = frozenset({
    "REAL_REGRESSION", "AUTH_PRECONDITION", "DATA_PRECONDITION_UNMET", "VARIANT_SUSPECTED",
})


def _norm(s: str) -> str:
    return " ".join((s or "").strip().lower().split())


def _node_name(n: Any) -> str:
    if isinstance(n, dict):
        return n.get("name") or n.get("label") or n.get("accessible_name") or ""
    return ""


def _node_role(n: Any) -> str:
    if isinstance(n, dict):
        return (n.get("role") or n.get("kind") or "").strip().lower()
    return ""


def _live_index(nodes) -> dict:
    """Normalized live control name -> (raw_name, role)."""
    idx: dict = {}
    for n in (nodes or []):
        nm = _node_name(n)
        if nm:
            idx.setdefault(_norm(nm), (nm, _node_role(n)))
    return idx


def propose_tool():
    from ..llm.types import ToolDefinition
    return ToolDefinition(
        name="propose_step_fixes",
        description=(
            "Propose a grounded fix for each failing E2E step. You may ONLY choose a "
            "target_name that appears VERBATIM in the provided LIVE CONTROLS list. If no live "
            "control matches the step's intent, or the failure looks like a real application "
            "defect (the recorded outcome was contradicted), use kind='refuse'. Never invent a "
            "control name."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "fixes": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "step_number": {"type": "integer"},
                            "kind": {"type": "string", "enum": ["rebind", "wait", "refuse"]},
                            "target_name": {"type": "string",
                                            "description": "Exact live-control accessible name to bind to (rebind only)."},
                            "control_kind": {"type": "string",
                                             "enum": ["select", "radio", "checkbox", "toggle", "button", "link", "textbox", ""],
                                             "description": "The live control's kind (rebind only)."},
                            "rationale": {"type": "string"},
                            "confidence": {"type": "number"},
                        },
                        "required": ["step_number", "kind", "rationale", "confidence"],
                    },
                }
            },
            "required": ["fixes"],
        },
    )


def build_prompt(*, failing, recorded_by_step, nodes) -> tuple[str, str]:
    system = (
        "You are an autonomous test-repair engineer for an end-to-end UI test. A few steps "
        "failed on a re-run. For each failing step you are given what the recording intended "
        "(the control's label, kind, value, expected outcome), the live error, and the LIST OF "
        "LIVE CONTROLS actually present on the page (accessible name + ARIA role).\n\n"
        "Reason like a human debugging the test, but obey these HARD rules:\n"
        "- You may ONLY bind to a control whose accessible name appears VERBATIM in LIVE "
        "CONTROLS. Never invent or guess a name.\n"
        "- If the recorded control was mis-bound or renamed (e.g. a VALUE was mistakenly used as "
        "a checkbox name, when the real control is a 'Plan tier' chooser), use kind='rebind' "
        "with the correct live target_name and its control_kind.\n"
        "- If the control is simply not present YET (a timing/render race), use kind='wait'.\n"
        "- If the live page shows the recorded OUTCOME was contradicted (a real app defect), or "
        "no live control can satisfy the step, use kind='refuse' — do NOT force a pass.\n"
        "Call propose_step_fixes exactly once."
    )
    lines = ["LIVE CONTROLS on the failure page (name — role):"]
    seen = set()
    for n in (nodes or []):
        nm = _node_name(n)
        if not nm or _norm(nm) in seen:
            continue
        seen.add(_norm(nm))
        lines.append(f"  - {nm} — {_node_role(n) or 'unknown'}")
    lines.append("")
    lines.append("FAILING STEPS:")
    for f in (failing or []):
        sn = f.get("step_number")
        rec = (recorded_by_step or {}).get(sn, {}) or {}
        lines.append(
            f"  step {sn}: recorded action on '{rec.get('label', '')}' "
            f"(kind={rec.get('kind', '')}, value='{rec.get('value', '')}'); "
            f"expected: {rec.get('expected', '')}"
        )
        lines.append(f"    live error: {(f.get('error_message', '') or '')[:300]}")
    return system, "\n".join(lines)


def validate_fixes(raw_fixes, *, nodes, min_confidence: float = 0.7) -> dict:
    """Keep only GROUNDED, confident fixes and map each to an override-channel descriptor.

    Returns ``{"applied": [{step_number, channel, payload, rationale, confidence}],
    "refused": [...]}``. A ``rebind`` whose ``target_name`` is not in the live snapshot
    is DROPPED into ``refused`` and NEVER applied — this is the anti-green-wash boundary
    (the agent cannot fabricate a selector).
    """
    idx = _live_index(nodes)
    applied: list = []
    refused: list = []
    for fx in (raw_fixes or []):
        if not isinstance(fx, dict):
            continue
        sn = fx.get("step_number")
        kind = (fx.get("kind") or "").strip().lower()
        try:
            conf = float(fx.get("confidence") or 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        rat = (fx.get("rationale") or "").strip()
        if kind == "refuse":
            refused.append({"step_number": sn, "rationale": rat, "confidence": conf})
            continue
        if conf < min_confidence:
            refused.append({"step_number": sn, "rationale": f"low confidence ({conf:.2f}): {rat}",
                            "confidence": conf})
            continue
        if kind == "wait":
            applied.append({"step_number": sn, "channel": "waits", "payload": {},
                            "rationale": rat, "confidence": conf})
            continue
        if kind == "rebind":
            tgt = _norm(fx.get("target_name") or "")
            if not tgt or tgt not in idx:
                refused.append({"step_number": sn,
                                "rationale": f"ungrounded target '{fx.get('target_name', '')}' "
                                             "not in live controls",
                                "confidence": conf})
                continue
            raw_name, role = idx[tgt]
            ck = (fx.get("control_kind") or "").strip().lower()
            # Omitted ("") or unrecognised -> infer the kind from the live control's ARIA
            # role (combobox -> select, radio -> radio, ...). "" is itself a valid kind
            # (a plain textbox), so we must treat OMITTED separately from a real empty.
            if not ck or ck not in _VALID_KINDS:
                ck = _ROLE_TO_KIND.get(role, "")
            payload = {"name": raw_name}
            if ck:
                payload["kind"] = ck
            applied.append({"step_number": sn, "channel": "reanchors", "payload": payload,
                            "rationale": rat, "confidence": conf})
            continue
        # unknown kind -> ignore (never apply)
    return {"applied": applied, "refused": refused}


async def propose(*, failing, recorded_by_step, nodes, router=None,
                  tier_name: str = "tier_premium", min_confidence: float = 0.7,
                  timeout_s: float = 40.0) -> dict:
    """Run the agent once and return ``validate_fixes(...)`` plus ``{ok, error, model}``.

    On ANY failure (no live controls, no LLM configured, provider error, no tool call)
    returns ``ok=False`` with empty ``applied`` so the caller escalates exactly as before
    — never fabricate, never crash.
    """
    if not nodes:
        return {"applied": [], "refused": [], "ok": False, "error": "no live controls captured"}
    try:
        from ..llm.router import build_router
        from ..llm.types import CompletionRequest, FinishReason
        r = router or build_router()
        system, prompt = build_prompt(failing=failing, recorded_by_step=recorded_by_step, nodes=nodes)
        tool = propose_tool()
        req = CompletionRequest(
            system=system, prompt=prompt, max_tokens=1500, temperature=0.0,
            request_timeout_s=timeout_s, tools=(tool,), tool_choice=tool.name,
            metadata={"task": "agentic_heal"},
        )
        resp = await r.complete_via_tier(tier_name=tier_name, request=req)
        if resp.finish_reason == FinishReason.ERROR or not resp.tool_calls:
            return {"applied": [], "refused": [], "ok": False,
                    "error": (resp.error_detail or "no tool call from agent")}
        raw = (resp.tool_calls[0].arguments or {}).get("fixes") or []
        out = validate_fixes(raw, nodes=nodes, min_confidence=min_confidence)
        out["ok"] = True
        out["model"] = getattr(resp, "model", "")
        return out
    except Exception as e:  # never let the agent crash the heal loop
        return {"applied": [], "refused": [], "ok": False, "error": f"{type(e).__name__}: {e}"}
