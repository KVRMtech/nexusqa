"""
Heart Engine — Test Case Generation Module.

Converts extracted business rules into comprehensive test cases using
LLM prompts. Produces typed ``TestCase`` / ``TestStep`` SDK models.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Optional

from nexus_sdk.models import TestCase, TestStep

logger = logging.getLogger(__name__)

# ─── Prompt Templates ──────────────────────────────────────────

TEST_GENERATION_SYSTEM = """You are an expert QA test engineer for insurance applications.
Given business rules, generate comprehensive test cases that cover:

1. HAPPY PATH: The normal successful flow
2. BOUNDARY: Edge values, transitions between categories
3. NEGATIVE: Invalid inputs, error conditions
4. EDGE CASES: Unusual but valid scenarios
5. REGRESSION: Scenarios that commonly break after changes

Each test case must have:
- name: descriptive test name
- description: what the test validates
- steps: list of {action, expected} pairs
- priority: critical/high/medium/low
- type: happy_path/boundary/negative/edge_case/regression

Return valid JSON only."""

TEST_GENERATION_USER = """Business Rules:
---
{rules}
---

Generate test cases covering: {coverage_targets}
Return as JSON with keys: test_cases"""


# ─── Test Generator ───────────────────────────────────────────

class TestGenerator:
    """
    Generates structured test cases from business rules via LLM.

    Parameters
    ----------
    llm : object
        HeartLLM instance (or any object with async ``generate(system, user)``).
    prompt_overrides : dict | None
        Optional prompt template overrides from plugins.
    """

    def __init__(
        self,
        llm,
        prompt_overrides: Optional[dict[str, str]] = None,
    ):
        self.llm = llm
        self._overrides = prompt_overrides or {}

    def _get_prompt(self, prompt_id: str, fallback: str) -> str:
        return self._overrides.get(prompt_id, fallback)

    async def generate_tests(
        self,
        rules,
        tenant_id: str,
        coverage_targets: list[str] | None = None,
        context: Optional[dict] = None,
    ) -> dict:
        """
        Generate test cases from business rules.

        Parameters
        ----------
        rules : list[BusinessRule]
            The business rules to generate tests for.
        tenant_id : str
            Tenant ID for the generated test cases.
        coverage_targets : list[str] | None
            Test coverage types to generate.
        context : dict | None
            Additional context (UI flows, etc.).

        Returns
        -------
        dict
            Keys: ``test_cases`` (list[TestCase]), ``coverage_summary`` (dict).
        """
        if coverage_targets is None:
            coverage_targets = ["happy_path", "boundary", "negative", "edge_case"]

        rules_json = json.dumps(
            [r.model_dump(mode="json") for r in rules], indent=2
        )

        system_prompt = self._get_prompt(
            "test_generation", TEST_GENERATION_SYSTEM
        )
        user_prompt_template = self._get_prompt(
            "test_generation_user", TEST_GENERATION_USER
        )

        response = await self.llm.generate(
            system_prompt,
            user_prompt_template.format(
                rules=rules_json,
                coverage_targets=", ".join(coverage_targets),
            ),
        )

        # ── JSON validation with retry ─────────────────────────
        parsed = self._safe_parse_json(response)
        if parsed is None:
            logger.warning(
                "heart.generators.json_parse_failed — retrying with repair prompt",
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
                "heart.generators.json_parse_failed_after_retry",
                extra={"response_preview": response[:200]},
            )
            parsed = {"test_cases": []}

        test_cases = self._parse_test_cases(parsed, tenant_id)

        # Build coverage summary
        type_counts: dict[str, int] = {}
        for tc_data in parsed.get("test_cases", []):
            t = tc_data.get("type", "general")
            type_counts[t] = type_counts.get(t, 0) + 1

        return {
            "test_cases": test_cases,
            "coverage_summary": type_counts,
        }

    # ── Internal helpers ───────────────────────────────────────

    @staticmethod
    def _safe_parse_json(text: str) -> dict | None:
        """Attempt to parse JSON, stripping markdown fences if present."""
        if not text:
            return None
        cleaned = text.strip()
        if cleaned.startswith("```"):
            first_newline = cleaned.index("\n") if "\n" in cleaned else len(cleaned)
            cleaned = cleaned[first_newline + 1:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3].rstrip()
        try:
            return json.loads(cleaned)
        except (json.JSONDecodeError, TypeError):
            return None

    def _parse_test_cases(
        self,
        parsed: dict,
        tenant_id: str,
    ) -> list[TestCase]:
        """Convert raw LLM JSON into TestCase SDK models."""
        test_cases: list[TestCase] = []

        for tc_data in parsed.get("test_cases", []):
            steps: list[TestStep] = []
            for step_data in tc_data.get("steps", []):
                steps.append(TestStep(
                    step_number=len(steps) + 1,
                    action=step_data.get("action", ""),
                    target_system=step_data.get("target_system", "web"),
                    expected_output=step_data.get(
                        "expected", step_data.get("expected_output", "")
                    ),
                ))

            tc = TestCase(
                test_id=f"TC-{uuid.uuid4().hex[:8].upper()}",
                tenant_id=tenant_id,
                title=tc_data.get("name", tc_data.get("title", "Unnamed test")),
                description=tc_data.get("description", ""),
                steps=steps,
                priority=tc_data.get("priority", "medium"),
                tags=[tc_data.get("type", "general")],
            )
            test_cases.append(tc)

        return test_cases
