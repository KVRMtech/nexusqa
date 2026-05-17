"""
Decision Engine — LLM-powered reasoning for cross-engine coordination.

Takes enriched context from multiple engines and makes intelligent
decisions about workflow routing, quality gates, and next actions.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)


class DecisionType(str, Enum):
    """Types of decisions the Brain can make."""
    ROUTE = "route"                     # Which engine(s) to invoke next
    QUALITY_GATE = "quality_gate"       # Pass/fail quality check
    CONFIDENCE_REVIEW = "confidence"    # Low-confidence flag for human review
    ESCALATION = "escalation"           # Escalate to SME / human
    RETRY = "retry"                     # Retry with different parameters
    MERGE = "merge"                     # Merge results from parallel engines
    SUMMARIZE = "summarize"             # Summarize cross-engine findings
    CONTRADICTION = "contradiction"     # Cross-session rule contradiction analysis


@dataclass
class DecisionContext:
    """Input context for a Brain decision."""
    session_id: str
    tenant_id: str
    decision_type: DecisionType
    engine_results: dict[str, Any] = field(default_factory=dict)
    rules_extracted: list[dict] = field(default_factory=list)
    test_cases: list[dict] = field(default_factory=list)
    confidence_scores: dict[str, float] = field(default_factory=dict)
    visual_context: dict[str, Any] = field(default_factory=dict)
    transcript_summary: str = ""
    user_query: str = ""
    constraints: dict[str, Any] = field(default_factory=dict)


@dataclass
class Decision:
    """Output decision from the Brain."""
    decision_id: str
    decision_type: DecisionType
    action: str                          # What to do next
    reasoning: str                       # LLM explanation of why
    confidence: float                    # 0.0-1.0
    recommended_engines: list[str] = field(default_factory=list)
    parameters: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    requires_human: bool = False


# ── Prompt templates ──────────────────────────────────────────

DECISION_SYSTEM_PROMPT = """\
You are the Brain of the Nexus QA platform — an intelligent coordinator
that makes decisions about quality assurance workflows.

Your role:
1. Analyze results from multiple AI engines (Shield, Ears, Eyes, Heart, etc.)
2. Identify gaps, contradictions, and quality issues
3. Decide what actions to take next
4. Route work to the appropriate engine(s)
5. Flag items that need human review

You have access to:
- Extracted business rules (from Heart engine)
- PII analysis results (from Shield engine)
- Visual/UI analysis (from Eyes engine)
- Transcription data (from Ears engine)
- Knowledge graph context (from Backbone engine)
- Test cases and execution results

Always respond with valid JSON matching this schema:
{
  "action": "string — what to do next",
  "reasoning": "string — detailed explanation",
  "confidence": 0.0-1.0,
  "recommended_engines": ["engine_name", ...],
  "parameters": {},
  "warnings": ["string", ...],
  "requires_human": false
}
"""

QUALITY_GATE_PROMPT = """\
Evaluate the quality of this QA session's outputs.

Rules extracted: {rules_count}
Test cases generated: {tests_count}
Confidence scores: {confidence_scores}
Engine results summary: {results_summary}

Assess:
1. Rule completeness — are there obvious gaps?
2. Test coverage — do tests cover happy path, negative, boundary, edge cases?
3. Consistency — do rules contradict each other?
4. Confidence — are low-confidence items flagged?
5. Overall readiness — is this ready for review or needs more work?

Respond with JSON:
{{
  "action": "approve" | "needs_review" | "needs_more_data" | "reject",
  "reasoning": "detailed explanation",
  "confidence": 0.0-1.0,
  "gaps": ["gap description", ...],
  "warnings": ["warning", ...],
  "requires_human": true/false,
  "recommended_engines": ["engine to re-run if needed"]
}}
"""

MERGE_RESULTS_PROMPT = """\
Merge and reconcile outputs from multiple engines for session {session_id}.

Results from engines:
{engine_results}

Your task:
1. Identify overlapping or conflicting information
2. Resolve conflicts using confidence scores and source quality
3. Produce a unified view
4. Flag any irreconcilable contradictions for human review

Respond with JSON:
{{
  "action": "merged",
  "reasoning": "how you resolved conflicts",
  "confidence": 0.0-1.0,
  "merged_rules": [...],
  "contradictions": [...],
  "warnings": [...],
  "requires_human": true/false
}}
"""

ROUTING_PROMPT = """\
Given the current session state, decide which engines should be invoked next.

Current state:
- Completed engines: {completed_engines}
- Pending data: {pending_data}
- User request: {user_query}
- Session goals: {constraints}

Available engines and their capabilities:
- Shield: PII detection, data redaction, compliance analysis
- Ears: Audio transcription, speaker diarization
- Eyes: Screenshot/video analysis, UI element detection, OCR
- Heart: Business rule extraction, test generation, reasoning
- Backbone: Knowledge graph storage, semantic search
- Hands: Synthetic test data generation
- Legs: Browser-based test execution
- Spine: Document parsing, ingestion
- Mouth: Report generation (HTML, PDF, CSV)
- Nerves: External integrations (Jira, Slack, GitHub)

Respond with JSON:
{{
  "action": "route",
  "reasoning": "why these engines",
  "confidence": 0.0-1.0,
  "recommended_engines": ["engine1", "engine2"],
  "parameters": {{"engine1": {{...}}, "engine2": {{...}}}},
  "warnings": [...],
  "requires_human": false
}}
"""

CONTRADICTION_ANALYSIS_PROMPT = """\
Analyze pairs of business rules from different KT sessions for contradictions.

You are comparing rules extracted from DIFFERENT sessions of the same tenant.
Contradictions arise when two rules make incompatible claims about the same
business process, threshold, condition, or requirement.

Rules from current session:
{current_rules}

Candidate conflicting rules from other sessions:
{candidate_rules}

For each pair, determine:
1. Are they truly contradictory, or just different aspects of the same process?
2. If contradictory, what is the severity? (high = blocks production, medium = causes confusion, low = cosmetic)
3. Which rule is likely more recent/authoritative?
4. Suggested resolution

Respond with JSON:
{{
  "action": "contradictions_analyzed",
  "reasoning": "overall analysis summary",
  "confidence": 0.0-1.0,
  "contradictions": [
    {{
      "rule_a_text": "text of rule from current session",
      "rule_b_text": "text of conflicting rule from another session",
      "rule_a_session": "session_id",
      "rule_b_session": "session_id",
      "description": "what the contradiction is",
      "severity": "high|medium|low",
      "suggested_resolution": "which rule to prefer and why",
      "confidence": 0.0-1.0
    }}
  ],
  "warnings": [...],
  "requires_human": true
}}
"""


class DecisionEngine:
    """
    LLM-powered decision engine for cross-engine coordination.

    Uses the Brain's tiered LLM provider to make intelligent decisions
    about workflow routing, quality gates, and result merging.
    """

    def __init__(self, llm_generate_fn):
        """
        Args:
            llm_generate_fn: Async callable(system_prompt, user_prompt) -> str
        """
        self._generate = llm_generate_fn
        self._decision_history: list[Decision] = []

    async def decide(self, context: DecisionContext) -> Decision:
        """Make a decision based on the given context."""
        decision_id = f"brain-decision-{uuid.uuid4().hex[:12]}"
        start = time.monotonic()

        if context.decision_type == DecisionType.QUALITY_GATE:
            result = await self._quality_gate_decision(context)
        elif context.decision_type == DecisionType.MERGE:
            result = await self._merge_decision(context)
        elif context.decision_type == DecisionType.ROUTE:
            result = await self._routing_decision(context)
        elif context.decision_type == DecisionType.SUMMARIZE:
            result = await self._summarize_decision(context)
        elif context.decision_type == DecisionType.CONTRADICTION:
            result = await self._contradiction_decision(context)
        else:
            result = await self._generic_decision(context)

        elapsed_ms = (time.monotonic() - start) * 1000

        decision = Decision(
            decision_id=decision_id,
            decision_type=context.decision_type,
            action=result.get("action", "unknown"),
            reasoning=result.get("reasoning", ""),
            confidence=float(result.get("confidence", 0.5)),
            recommended_engines=result.get("recommended_engines", []),
            parameters=result.get("parameters", {}),
            warnings=result.get("warnings", []),
            requires_human=result.get("requires_human", False),
        )

        self._decision_history.append(decision)

        logger.info(
            "brain.decision.made",
            extra={
                "decision_id": decision_id,
                "type": context.decision_type.value,
                "action": decision.action,
                "confidence": decision.confidence,
                "elapsed_ms": round(elapsed_ms, 1),
            },
        )
        return decision

    async def _quality_gate_decision(self, ctx: DecisionContext) -> dict:
        """Evaluate quality of session outputs."""
        prompt = QUALITY_GATE_PROMPT.format(
            rules_count=len(ctx.rules_extracted),
            tests_count=len(ctx.test_cases),
            confidence_scores=json.dumps(ctx.confidence_scores),
            results_summary=json.dumps(
                {k: str(v)[:200] for k, v in ctx.engine_results.items()},
                indent=2,
            ),
        )
        return await self._call_llm(DECISION_SYSTEM_PROMPT, prompt)

    async def _merge_decision(self, ctx: DecisionContext) -> dict:
        """Merge results from multiple engines."""
        prompt = MERGE_RESULTS_PROMPT.format(
            session_id=ctx.session_id,
            engine_results=json.dumps(
                {k: str(v)[:500] for k, v in ctx.engine_results.items()},
                indent=2,
            ),
        )
        return await self._call_llm(DECISION_SYSTEM_PROMPT, prompt)

    async def _routing_decision(self, ctx: DecisionContext) -> dict:
        """Decide which engines to invoke next."""
        completed = [k for k, v in ctx.engine_results.items() if v]
        prompt = ROUTING_PROMPT.format(
            completed_engines=", ".join(completed) if completed else "none",
            pending_data=json.dumps(ctx.constraints.get("pending", {})),
            user_query=ctx.user_query or "Continue QA workflow",
            constraints=json.dumps(ctx.constraints),
        )
        return await self._call_llm(DECISION_SYSTEM_PROMPT, prompt)

    async def _summarize_decision(self, ctx: DecisionContext) -> dict:
        """Summarize cross-engine session findings."""
        prompt = f"""Summarize the QA session findings across all engines.

Session: {ctx.session_id}
Transcript summary: {ctx.transcript_summary[:1000]}
Rules extracted: {len(ctx.rules_extracted)}
Test cases: {len(ctx.test_cases)}
Engine results: {json.dumps({k: str(v)[:300] for k, v in ctx.engine_results.items()}, indent=2)}

Provide a concise executive summary with key findings, risks, and recommendations.
Respond with JSON: {{"action": "summarized", "reasoning": "the summary", "confidence": 0.0-1.0, "warnings": [...]}}"""
        return await self._call_llm(DECISION_SYSTEM_PROMPT, prompt)

    async def _contradiction_decision(self, ctx: DecisionContext) -> dict:
        """Analyse rules from different sessions for contradictions."""
        current_rules = ctx.rules_extracted
        # Candidate rules from other sessions are passed via engine_results
        candidate_rules = ctx.engine_results.get("candidate_rules", [])

        # Truncate each rule to prevent prompt overflow
        def _summarise(rules: list, max_per_rule: int = 500) -> str:
            parts = []
            for i, r in enumerate(rules[:50]):
                text = r.get("rule_text", str(r))[:max_per_rule]
                sid = r.get("source", {}).get("session_id", "unknown")
                parts.append(f"  [{i+1}] (session {sid}) {text}")
            return "\n".join(parts) if parts else "  (none)"

        prompt = CONTRADICTION_ANALYSIS_PROMPT.format(
            current_rules=_summarise(current_rules),
            candidate_rules=_summarise(candidate_rules),
        )
        result = await self._call_llm(DECISION_SYSTEM_PROMPT, prompt)

        # The LLM returns "contradictions" at the top level, but Decision
        # only maps result["parameters"] → Decision.parameters.  Move the
        # contradictions list into parameters so the endpoint can access it
        # via decision.parameters.get("contradictions").
        if "contradictions" in result:
            params = result.setdefault("parameters", {})
            params["contradictions"] = result.pop("contradictions")

        return result

    async def _generic_decision(self, ctx: DecisionContext) -> dict:
        """Generic decision for unclassified types."""
        prompt = f"""Make a decision for this QA session.
Type: {ctx.decision_type.value}
Context: {json.dumps({k: str(v)[:200] for k, v in ctx.engine_results.items()})}
Query: {ctx.user_query}
Respond with JSON: {{"action": "...", "reasoning": "...", "confidence": 0.0-1.0, "warnings": [...]}}"""
        return await self._call_llm(DECISION_SYSTEM_PROMPT, prompt)

    async def _call_llm(self, system: str, user: str) -> dict:
        """Call LLM and parse JSON response."""
        try:
            raw = await self._generate(system, user)
            return json.loads(raw)
        except json.JSONDecodeError:
            # Try to extract JSON from response
            try:
                start = raw.index("{")
                end = raw.rindex("}") + 1
                return json.loads(raw[start:end])
            except (ValueError, json.JSONDecodeError):
                return {
                    "action": "parse_error",
                    "reasoning": raw[:500] if raw else "Empty response",
                    "confidence": 0.3,
                    "warnings": ["Brain LLM response was not valid JSON"],
                }
        except Exception as e:
            logger.error("brain.decision.llm_error", extra={"error": str(e)})
            return {
                "action": "error",
                "reasoning": f"LLM call failed: {e}",
                "confidence": 0.0,
                "warnings": [str(e)],
            }

    @property
    def history(self) -> list[Decision]:
        return list(self._decision_history)

    def clear_history(self):
        self._decision_history.clear()
