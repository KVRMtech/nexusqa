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

    # ── Phase 9: Visual-strict test generation ────────────────

    async def generate_from_visual_proof(
        self,
        graph: dict,
        tenant_id: str,
        evidence_mode: str = "visual_strict",
        approved_edge_ids: list[str] | None = None,
    ) -> dict:
        """Generate test cases grounded purely in the visual evidence graph.

        Each returned step carries the full evidence anchor (scene_id, control_id,
        edge_id, proof_confidence) so the persister can write them straight into
        ``test_case_steps``.

        Returns:
            dict with keys: test_cases, unproven_steps, coverage_report
        """
        scenes: list[dict] = graph.get("scenes", [])
        controls_by_scene_raw: dict[str, list[dict]] = graph.get("controls_by_scene", {})
        controls: list[dict] = [
            c for clist in controls_by_scene_raw.values() for c in clist
        ]
        edges: list[dict] = graph.get("edges", [])

        if approved_edge_ids:
            edges = [e for e in edges if e.get("edge_id") in approved_edge_ids]

        scene_by_id = {s["scene_id"]: s for s in scenes}
        ctrl_by_scene: dict[str, list[dict]] = {
            sid: list(clist) for sid, clist in controls_by_scene_raw.items()
        }

        if evidence_mode == "visual_strict":
            working_edges = [e for e in edges if e.get("edge_type") == "action_confirmed_transition"]
            unconfirmed_edges = [e for e in edges if e.get("edge_type") != "action_confirmed_transition"]
        else:
            working_edges = edges
            unconfirmed_edges = []

        auto_ready_count = sum(1 for c in controls if c.get("automation_ready"))

        # Build prompt
        if evidence_mode == "visual_strict":
            system_prompt = (
                "You are a QA automation engineer producing grounded Playwright test cases. "
                "RULES:\n"
                "- Each test step MUST reference a scene_id from the evidence graph.\n"
                "- Each action step MUST reference a control_id with automation_ready=true.\n"
                "- Expected results MUST quote OCR-confirmed text from the linked to_scene.\n"
                "- Steps without a grounded scene_id AND control_id go to unproven_steps with explicit reason.\n"
                "- Do NOT infer steps from context. Only generate steps for which evidence exists.\n"
                "Return ONLY valid JSON: "
                "{\"test_cases\": [{\"title\": str, \"steps\": ["
                "{\"action\": str, \"target_element\": str, \"expected_output\": str, "
                "\"evidence_scene_id\": str, \"evidence_control_id\": str, "
                "\"evidence_edge_id\": str, \"proof_confidence\": float}]}], "
                "\"unproven_steps\": [{\"description\": str, \"reason\": str}]}"
            )
        else:
            system_prompt = (
                "You are an expert QA automation engineer. Generate E2E test cases from "
                "the visual evidence. Return ONLY valid JSON: "
                "{\"test_cases\": [{\"title\": str, \"steps\": ["
                "{\"action\": str, \"target_element\": str, \"expected_output\": str, "
                "\"proof_confidence\": float}]}], \"unproven_steps\": []}"
            )

        digest_lines: list[str] = [
            f"Scenes: {len(scenes)}, confirmed transitions: {len(working_edges)}, "
            f"automation-ready controls: {auto_ready_count}.",
            "",
        ]
        for i, edge in enumerate(working_edges[:30], 1):
            from_s = scene_by_id.get(edge.get("from_scene_id", ""), {})
            to_s = scene_by_id.get(edge.get("to_scene_id", ""), {})
            digest_lines.append(
                f"  {i}. edge_id={edge.get('edge_id', '')} "
                f"{from_s.get('screen_name') or 'screen'} "
                f"—[{edge.get('action_type') or 'navigate'} {edge.get('action_value') or ''}]→ "
                f"{to_s.get('screen_name') or 'screen'}"
            )
            ready_ctrls = [c for c in ctrl_by_scene.get(edge.get("from_scene_id", ""), []) if c.get("automation_ready")]
            for c in ready_ctrls[:3]:
                digest_lines.append(
                    f"     control_id={c.get('control_id', '')} label={c.get('label_text') or c.get('element_type')} "
                    f"sel={c.get('playwright_selector')}"
                )
            to_ocr = (to_s.get("ocr_text") or "")[:100]
            if to_ocr:
                digest_lines.append(f"     to_scene ocr={to_ocr!r}")

        try:
            response_text = await self.llm.generate(system_prompt, "\n".join(digest_lines))
            parsed = self._safe_parse_json(response_text)
            test_cases = (parsed or {}).get("test_cases", [])
            llm_unproven = (parsed or {}).get("unproven_steps", [])
        except Exception:
            test_cases = []
            llm_unproven = []

        # Append unconfirmed edge stubs
        unproven_steps = list(llm_unproven)
        for e in unconfirmed_edges:
            from_s = scene_by_id.get(e.get("from_scene_id", ""), {})
            to_s = scene_by_id.get(e.get("to_scene_id", ""), {})
            unproven_steps.append({
                "description": f"{from_s.get('screen_name') or 'scene'} → {to_s.get('screen_name') or 'scene'}",
                "reason": "transition observed but action not confirmed",
                "edge_id": e.get("edge_id", ""),
                "evidence_confidence": e.get("evidence_confidence", 0.0),
            })

        proven_steps = sum(
            1 for tc in test_cases
            for step in tc.get("steps", [])
            if step.get("evidence_scene_id") or step.get("proof_confidence", 0) > 0
        )
        automation_ready_pct = round(auto_ready_count / max(len(controls), 1) * 100, 1) if controls else 0.0

        return {
            "test_cases": test_cases,
            "unproven_steps": unproven_steps,
            "coverage_report": {
                "total_edges": len(edges),
                "proven_steps": proven_steps,
                "unproven_steps": len(unproven_steps),
                "automation_ready_pct": automation_ready_pct,
            },
        }

