"""
Heart Engine — Rule Extraction Module.

Extracts structured business rules from free-text transcripts and
documents using LLM prompts. Converts raw LLM output into validated
``BusinessRule`` SDK models with source references.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Optional

from nexus_sdk.models import BusinessRule, SourceReference, Confidence

logger = logging.getLogger(__name__)

#: Label → score for the confidence word the extraction prompt asks the model for.
_CONFIDENCE_SCORES = {"critical": 1.0, "high": 0.9, "medium": 0.6, "low": 0.3}


def _confidence(raw) -> Confidence:
    """Build a :class:`Confidence` from whatever the model returned.

    ``Confidence`` is a MODEL (``score``/``reason``), not an enum or a scalar, so
    the previous ``Confidence(r.get("confidence", "medium"))`` raised
    ``TypeError: BaseModel.__init__() takes 1 positional argument but 2 were
    given`` — every rule-extraction call died at the point of building its
    result. The prompt asks for a WORD ("high"/"medium"/…), but an LLM will
    sometimes answer with a number, so both are accepted and anything
    unrecognised degrades to the neutral middle rather than throwing.
    """
    if isinstance(raw, Confidence):
        return raw
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        score = float(raw)
        # Tolerate a 0-100 scale as well as 0-1.
        if score > 1.0:
            score = score / 100.0
        return Confidence(score=max(0.0, min(1.0, score)), reason="model-reported score")
    label = str(raw or "").strip().lower()
    if label in _CONFIDENCE_SCORES:
        return Confidence(score=_CONFIDENCE_SCORES[label], reason=f"model-reported '{label}'")
    try:
        return Confidence(score=max(0.0, min(1.0, float(label))), reason="model-reported score")
    except ValueError:
        return Confidence(score=0.6, reason=f"unrecognised confidence {raw!r}; defaulted")


# ─── Prompt Templates ──────────────────────────────────────────

RULE_EXTRACTION_SYSTEM = """You are an expert insurance business analyst AI.
Your job is to extract precise, testable business rules from knowledge transfer session transcripts.

For each rule you extract:
1. Write a clear natural language DESCRIPTION
2. Write the CONDITION in IF/THEN format
3. Write the EXPECTED_RESULT
4. Assign a DOMAIN (underwriting, rating, claims, billing, compliance, etc.)
5. Assign a PRIORITY (critical, high, medium, low)
6. Assign a CONFIDENCE level (high, medium, low) based on how clearly the SME stated the rule

Also identify:
- EDGE CASES: scenarios the SME didn't explicitly cover but are logically implied
- CONTRADICTIONS: rules that conflict with each other
- QUESTIONS: things that need SME clarification

Return valid JSON only."""

RULE_EXTRACTION_USER = """Transcript from KT session:
---
{transcript}
---

{visual_context}

Extract all business rules, edge cases, contradictions, and questions.
Return as JSON with keys: rules, edge_cases, contradictions, questions_for_sme"""

DOCUMENT_ANALYSIS_SYSTEM = (
    "You are an expert document analyst for insurance QA. "
    "Analyze the given text and return JSON with: "
    "summary (string), rules_found (int), "
    "risks (array of {{title, severity, description}}), "
    "compliance_flags (array of {{regulation, issue, severity}}), "
    "key_entities (array of strings)."
)


# ─── Rule Extractor ───────────────────────────────────────────

class RuleExtractor:
    """
    Extracts business rules from transcripts using LLM.

    Parameters
    ----------
    llm : object
        HeartLLM instance (or any object with async ``generate(system, user)``).
    max_rules : int
        Maximum number of rules per extraction (guardrail).
    prompt_overrides : dict | None
        Optional prompt template overrides from plugins.
    """

    def __init__(
        self,
        llm,
        max_rules: int = 50,
        prompt_overrides: Optional[dict[str, str]] = None,
    ):
        self.llm = llm
        self.max_rules = max_rules
        self._overrides = prompt_overrides or {}

    def _get_prompt(self, prompt_id: str, fallback: str) -> str:
        """Get prompt from plugin overrides, falling back to built-in."""
        return self._overrides.get(prompt_id, fallback)

    async def extract_rules(
        self,
        transcript: str,
        session_id: str,
        tenant_id: str,
        visual_context: Optional[dict] = None,
    ) -> dict:
        """
        Extract business rules from a transcript.

        Returns
        -------
        dict
            Keys: ``rules`` (list[BusinessRule]), ``edge_cases``,
            ``contradictions``, ``questions_for_sme``, ``raw_parsed``.
        """
        visual_ctx = ""
        if visual_context:
            visual_ctx = f"Visual Context:\n{json.dumps(visual_context, indent=2)}"

        system_prompt = self._get_prompt(
            "rule_extraction", RULE_EXTRACTION_SYSTEM
        )
        user_prompt_template = self._get_prompt(
            "rule_extraction_user", RULE_EXTRACTION_USER
        )

        response = await self.llm.generate(
            system_prompt,
            user_prompt_template.format(
                transcript=transcript,
                visual_context=visual_ctx or "No visual context available.",
            ),
        )

        # ── JSON validation with retry ─────────────────────────
        parsed = self._safe_parse_json(response)
        if parsed is None:
            # Retry once with a repair prompt
            logger.warning(
                "heart.extractors.json_parse_failed — retrying with repair prompt",
            )
            repair_response = await self.llm.generate(
                "You are a JSON repair assistant. The following text was supposed to be "
                "valid JSON but has syntax errors. Fix the JSON and return ONLY the "
                "corrected JSON, nothing else.",
                response,
            )
            parsed = self._safe_parse_json(repair_response)

        if parsed is None:
            logger.error(
                "heart.extractors.json_parse_failed_after_retry",
                extra={"response_preview": response[:200]},
            )
            parsed = {
                "rules": [],
                "edge_cases": [],
                "contradictions": [],
                "questions_for_sme": [],
            }

        rules = self._parse_rules(parsed, session_id, tenant_id)

        return {
            "rules": rules,
            "edge_cases": parsed.get("edge_cases", []),
            "contradictions": parsed.get("contradictions", []),
            "questions_for_sme": parsed.get("questions_for_sme", []),
            "raw_parsed": parsed,
        }

    async def analyze_document(
        self,
        content: str,
        document_type: Optional[str] = None,
    ) -> dict:
        """
        Analyze a document for rules, risks, and compliance.

        Returns
        -------
        dict
            Keys: ``summary``, ``rules_found``, ``risks``,
            ``compliance_flags``, ``key_entities``.
        """
        system_prompt = self._get_prompt(
            "document_analysis", DOCUMENT_ANALYSIS_SYSTEM
        )
        response = await self.llm.generate(system_prompt, content[:8000])

        parsed = self._safe_parse_json(response)
        if parsed is None:
            logger.warning(
                "heart.extractors.document_json_parse_failed",
                extra={"response_preview": response[:200]},
            )
            parsed = {}

        return {
            "summary": parsed.get("summary", "Analysis complete."),
            "rules_found": parsed.get("rules_found", 0),
            "risks": parsed.get("risks", []),
            "compliance_flags": parsed.get("compliance_flags", []),
            "key_entities": parsed.get("key_entities", []),
        }

    # ── Internal helpers ───────────────────────────────────────

    @staticmethod
    def _safe_parse_json(text: str) -> Optional[dict]:
        """Attempt to parse JSON, stripping markdown fences if present."""
        if not text:
            return None
        # Strip common LLM artifacts: ```json ... ``` wrappers
        cleaned = text.strip()
        if cleaned.startswith("```"):
            # Remove opening fence (```json or ```)
            first_newline = cleaned.index("\n") if "\n" in cleaned else len(cleaned)
            cleaned = cleaned[first_newline + 1:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3].rstrip()
        try:
            return json.loads(cleaned)
        except (json.JSONDecodeError, TypeError):
            return None

    def _parse_rules(
        self,
        parsed: dict,
        session_id: str,
        tenant_id: str,
    ) -> list[BusinessRule]:
        """Convert raw parsed JSON into BusinessRule SDK models."""
        rules: list[BusinessRule] = []

        for r in parsed.get("rules", [])[:self.max_rules]:
            cond = r.get("condition", r.get("conditions", ""))
            conditions = cond if isinstance(cond, list) else ([cond] if cond else [])

            rule = BusinessRule(
                rule_id=f"R-{uuid.uuid4().hex[:8].upper()}",
                tenant_id=tenant_id,
                category=r.get("domain", r.get("category", "general")),
                rule_text=r.get("description", r.get("rule_text", "")),
                conditions=conditions,
                exceptions=r.get("exceptions", []),
                source=SourceReference(
                    session_id=session_id,
                    speaker_name="SME",
                    confidence=_confidence(r.get("confidence", "medium")),
                ),
                tags=[],
            )
            rules.append(rule)

        return rules
