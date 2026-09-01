"""Co-Architect conversation encoding + structured-output parsing (P4).

HeartLLM.generate(system_prompt, user_prompt) is single-turn, so multi-turn
conversation is encoded into the user_prompt as a transcript with explicit
turn markers.  The LLM is instructed (via system prompt) to respond only as
the FINAL ``Assistant:`` turn.

Parsing handles three failure modes the LLM can produce:
  1. Plain prose when JSON was expected — wrapped into a response-only object
  2. JSON inside markdown fences — fences stripped
  3. Malformed JSON — single repair attempt at the call site (parser only
     reports failure here)
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable


# ─── Data shapes ─────────────────────────────────────────────────────────


@dataclass
class ProposedScenarioStep:
    step_number: int
    action: str
    input_data: str
    expected_output: str
    evidence_scene_id: str
    evidence_control_id: str
    evidence_edge_id: str
    proof_confidence: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_number": self.step_number,
            "action": self.action,
            "input_data": self.input_data,
            "expected_output": self.expected_output,
            "evidence_scene_id": self.evidence_scene_id,
            "evidence_control_id": self.evidence_control_id,
            "evidence_edge_id": self.evidence_edge_id,
            "proof_confidence": self.proof_confidence,
        }


@dataclass
class ProposedScenario:
    title: str
    rationale: str
    strategy: str
    steps: list[ProposedScenarioStep]

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "rationale": self.rationale,
            "strategy": self.strategy,
            "steps": [s.to_dict() for s in self.steps],
        }


@dataclass
class ChatTurnResponse:
    response: str
    proposed_scenarios: list[ProposedScenario] = field(default_factory=list)
    parse_warning: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "response": self.response,
            "proposed_scenarios": [sc.to_dict() for sc in self.proposed_scenarios],
            "parse_warning": self.parse_warning,
        }


# ─── Conversation encoding ───────────────────────────────────────────────


def encode_conversation(messages: Iterable[dict]) -> str:
    """Encode a list of {role, content} dicts into a transcript the
    single-turn LLM can consume.

    The trailing ``Assistant:`` marker primes the model to produce only the
    next assistant turn.
    """
    out: list[str] = []
    out.append("--- CONVERSATION ---")
    for m in messages:
        role = (m.get("role") or "user").lower().strip()
        content = (m.get("content") or "").rstrip()
        if not content:
            continue
        if role == "user":
            out.append(f"User: {content}")
        elif role == "assistant":
            out.append(f"Assistant: {content}")
        elif role == "system":
            # Inline system messages stay out of the transcript; they should
            # be applied to the system prompt instead.
            continue
        else:
            out.append(f"{role.capitalize()}: {content}")
    out.append("Assistant:")
    return "\n\n".join(out)


# ─── JSON parsing (mirrors test_generator._safe_parse_json) ──────────────


def _strip_fences(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        # Drop the opening fence (with optional language tag)
        first_newline = cleaned.find("\n")
        if first_newline != -1:
            cleaned = cleaned[first_newline + 1:]
        else:
            cleaned = cleaned[3:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3].rstrip()
    return cleaned


def _try_json(text: str) -> dict | None:
    if not text:
        return None
    cleaned = _strip_fences(text)
    try:
        parsed = json.loads(cleaned)
        return parsed if isinstance(parsed, dict) else None
    except (json.JSONDecodeError, TypeError):
        return None


def _coerce_step(raw: dict, step_index: int) -> ProposedScenarioStep | None:
    """Validate a step dict from the LLM. Returns None when the step lacks
    the mandatory scene_id + control_id grounding."""
    scene_id = (raw.get("evidence_scene_id") or "").strip()
    control_id = (raw.get("evidence_control_id") or "").strip()
    if not scene_id or not control_id:
        return None
    try:
        proof_confidence = float(raw.get("proof_confidence") or 0.0)
    except (TypeError, ValueError):
        proof_confidence = 0.0
    return ProposedScenarioStep(
        step_number=int(raw.get("step_number") or step_index + 1),
        action=str(raw.get("action") or "").strip(),
        input_data=str(raw.get("input_data") or "").strip(),
        expected_output=str(raw.get("expected_output") or "").strip(),
        evidence_scene_id=scene_id,
        evidence_control_id=control_id,
        evidence_edge_id=str(raw.get("evidence_edge_id") or "").strip(),
        proof_confidence=max(0.0, min(1.0, proof_confidence)),
    )


def _coerce_scenario(raw: dict) -> ProposedScenario | None:
    """Validate a scenario dict. Returns None when no steps survive."""
    raw_steps = raw.get("steps") or []
    if not isinstance(raw_steps, list):
        return None
    steps: list[ProposedScenarioStep] = []
    for i, s in enumerate(raw_steps):
        if not isinstance(s, dict):
            continue
        coerced = _coerce_step(s, i)
        if coerced is not None:
            steps.append(coerced)
    if not steps:
        return None
    return ProposedScenario(
        title=str(raw.get("title") or "Untitled scenario").strip(),
        rationale=str(raw.get("rationale") or "").strip(),
        strategy=(str(raw.get("strategy") or "co_architect").strip() or "co_architect"),
        steps=steps,
    )


def parse_chat_response(
    text: str, *, propose_scenarios: bool,
) -> ChatTurnResponse:
    """Parse the LLM output into a :class:`ChatTurnResponse`.

    Handles plain-prose responses, JSON-wrapped responses, and partial
    structured output. When ``propose_scenarios`` is False, the parser will
    NOT attempt JSON parsing — it returns the entire text as ``response``.
    """
    text = (text or "").strip()
    if not propose_scenarios:
        return ChatTurnResponse(response=text or "(empty response)")

    parsed = _try_json(text)
    if parsed is None:
        # LLM didn't produce JSON — preserve the prose as the response.
        return ChatTurnResponse(
            response=text or "(empty response)",
            proposed_scenarios=[],
            parse_warning="Expected JSON but received plain text; no scenarios extracted.",
        )

    response_text = str(parsed.get("response") or "").strip()
    raw_scenarios = parsed.get("proposed_scenarios") or []
    proposed: list[ProposedScenario] = []
    dropped = 0
    if isinstance(raw_scenarios, list):
        for raw in raw_scenarios:
            if not isinstance(raw, dict):
                dropped += 1
                continue
            coerced = _coerce_scenario(raw)
            if coerced is None:
                dropped += 1
                continue
            proposed.append(coerced)

    warning: str | None = None
    if dropped:
        warning = f"Dropped {dropped} ungrounded scenario(s) from the LLM output."

    return ChatTurnResponse(
        response=response_text or "(empty response)",
        proposed_scenarios=proposed,
        parse_warning=warning,
    )
