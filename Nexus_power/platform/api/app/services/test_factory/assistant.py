"""Co-Architect (repointed) — a QA copilot grounded in Pages & Forms.

The original Co-Architect was welded to the visual-evidence graph and errored
when none existed.  This version grounds the assistant in the data that IS
available and rich: the demonstrated flow (page_visits / page_actions / form
values) and the generated test cases.

Anti-hallucination by construction: the model is given ONLY the captured
evidence as context and instructed to answer from it — never to invent screens,
fields, or values.  It does not generate tests itself; it points the user at the
Generate / Negative / Boundary actions.
"""

from __future__ import annotations

import os
from typing import Sequence

from nexus_sdk.models import ProductionTestCase

from ..llm import CompletionRequest, FinishReason, LLMRouter
from .generator import PageActionInput, PageVisitInput, _is_real_value
from .service import _load_current_pages_and_actions, load_active_production_cases

_LLM_TASK = os.environ.get("LLM_TASK_ASSISTANT", "form_snapshot_extraction")
_MAX_VISITS = 40
_MAX_CASES = 60

_SYSTEM_PROMPT = (
    "You are Co-Architect, a QA copilot inside Nexus Test Studio. You help the "
    "user understand and extend the test suite that was generated from THEIR "
    "screen recording (the 'Pages & Forms' evidence below).\n\n"
    "STRICT RULES:\n"
    "- Answer ONLY from the evidence provided below (the demonstrated flow + the "
    "generated test cases). \n"
    "- If the user asks about something not in the evidence, say it was not "
    "captured in this recording — do NOT invent screens, fields, or values.\n"
    "- You do NOT generate tests yourself. If the user wants more coverage, tell "
    "them which button to click: 'Generate' (demonstrated + combinations), "
    "'Negative', 'Boundary', or 'Capture options'.\n"
    "- Be concise, concrete, and cite real URLs/fields/values from the evidence."
)


def _build_context(
    visits: Sequence[PageVisitInput],
    actions: Sequence[PageActionInput],
    cases: Sequence[ProductionTestCase],
) -> str:
    actions_by_visit: dict[str, list[PageActionInput]] = {}
    for a in actions:
        actions_by_visit.setdefault(a.page_visit_id, []).append(a)

    lines: list[str] = ["DEMONSTRATED FLOW (what the user actually did):"]
    shown = 0
    for v in sorted(visits, key=lambda x: x.sequence_index):
        fields = {
            k: val for k, val in (v.form_snapshot or {}).items()
            if _is_real_value(k, val)
        }
        acts = [
            f"{a.verb} '{a.target_label}'"
            for a in sorted(actions_by_visit.get(v.page_visit_id, []), key=lambda x: x.subaction_index)
            if (a.verb or "").lower() in {"click", "type", "select", "press"} and a.target_label
        ]
        if not (v.url_host or fields or acts):
            continue  # skip pure-noise frames
        loc = v.url_path or v.location or "(page)"
        line = f"- {loc}"
        if fields:
            line += " | fields: " + ", ".join(f"{k}={val}" for k, val in fields.items())
        if acts:
            line += " | actions: " + "; ".join(acts[:6])
        lines.append(line)
        shown += 1
        if shown >= _MAX_VISITS:
            break

    lines.append("")
    lines.append("GENERATED TEST CASES:")
    for c in cases[:_MAX_CASES]:
        lines.append(f"- [{c.type or 'functional'}] {c.name} ({len(c.steps or [])} steps)")
    if not cases:
        lines.append("- (none yet — the user can click Generate)")
    return "\n".join(lines)


def _build_prompt(context: str, message: str, history: Sequence[dict]) -> str:
    parts = [context, ""]
    recent = [h for h in history if isinstance(h, dict)][-6:]
    if recent:
        parts.append("CONVERSATION SO FAR:")
        for turn in recent:
            role = "User" if turn.get("role") == "user" else "Assistant"
            parts.append(f"{role}: {str(turn.get('content', ''))[:500]}")
        parts.append("")
    parts.append(f"User question: {message}")
    return "\n".join(parts)


async def answer(
    session,
    *,
    artifact_id: str,
    tenant_id: str,
    message: str,
    history: Sequence[dict],
    router: LLMRouter,
) -> dict:
    """Return a grounded answer to the user's question."""
    visits, actions = await _load_current_pages_and_actions(
        session, artifact_id=artifact_id,
    )
    cases = await load_active_production_cases(session, artifact_id=artifact_id)
    context = _build_context(visits, actions, cases)

    request = CompletionRequest(
        system=_SYSTEM_PROMPT,
        prompt=_build_prompt(context, message, history),
        max_tokens=700,
        temperature=0.2,
        correlation_id=f"{artifact_id}:assistant",
        enable_prompt_cache=True,
        metadata={"task": _LLM_TASK},
    )
    response = await router.complete(task=_LLM_TASK, request=request)

    if response.finish_reason == FinishReason.ERROR or not (response.text or "").strip():
        return {
            "answer": (
                "I couldn't reach the assistant model just now. The evidence is "
                "loaded — please try again."
            ),
            "grounded_on": {"pages": len(visits), "test_cases": len(cases)},
            "model": f"{response.provider}/{response.model}",
            "error": True,
        }

    return {
        "answer": response.text.strip(),
        "grounded_on": {"pages": len(visits), "test_cases": len(cases)},
        "model": f"{response.provider}/{response.model}",
    }
