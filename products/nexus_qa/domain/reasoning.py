"""
Insurance Reasoning Extension for Heart Engine.

Extracted from engines/heart-engine/main.py prompt templates.
Defines insurance-specific LLM prompts for rule extraction, test generation,
and flow exploration.
"""

from __future__ import annotations

from nexus_sdk.plugins.extensions import (
    GuardrailRule,
    PromptTemplate,
    ReasoningExtension,
)


# ─── Prompt Templates ─────────────────────────────────────────

_RULE_EXTRACTION_SYSTEM = """You are an expert insurance business analyst AI.
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

_RULE_EXTRACTION_USER = """Transcript from KT session:
---
{transcript}
---

{visual_context}

Extract all business rules, edge cases, contradictions, and questions.
Return as JSON with keys: rules, edge_cases, contradictions, questions_for_sme"""

_TEST_GENERATION_SYSTEM = """You are an expert QA test engineer for insurance applications.
Given business rules, generate comprehensive test cases that cover:

1. HAPPY PATH: The normal successful flow
2. BOUNDARY: Edge values, transitions between categories
3. NEGATIVE: Invalid inputs, error conditions
4. EDGE CASES: Unusual but valid scenarios
5. REGRESSION: Scenarios that commonly break after changes

Each test case must have:
- name: descriptive test name
- description: what the test validates
- steps: list of {{action, expected}} pairs
- priority: critical/high/medium/low
- type: happy_path/boundary/negative/edge_case/regression

Return valid JSON only."""

_TEST_GENERATION_USER = """Business Rules:
---
{rules}
---

Generate test cases covering: {coverage_targets}
Return as JSON with keys: test_cases"""

_EXPLORE_FLOWS_SYSTEM = """You are an expert at discovering all possible paths through a software system.
An SME showed you ONE flow. Your job is to think about EVERY other possible flow.

For each UI element, ask:
- What if the user clicks something DIFFERENT?
- What if validation fails?
- What if the data is in a different state?
- What if permissions are different?
- What about error handling paths?
- What about concurrent access?
- What about edge cases in the data?

Think systematically about:
1. Every decision point → what are ALL the branches?
2. Every input field → what are ALL valid and invalid values?
3. Every state transition → what are ALL possible states?
4. Every integration → what if the external system is down/slow/returns errors?

Return valid JSON only."""

_EXPLORE_FLOWS_USER = """Demonstrated Flow:
---
{demonstrated_flow}
---

Known UI Screens:
{ui_screens}

Known Business Rules:
{known_rules}

Explore ALL possible flows. Return as JSON with keys: explored_flows, new_paths_found, questions"""

_CONTRADICTION_DETECTION_SYSTEM = """You are an expert at detecting contradictions and inconsistencies
in insurance business rules. Given a set of rules extracted from different KT sessions
and documents, identify:

1. DIRECT CONTRADICTIONS: Two rules that cannot both be true
2. IMPLICIT CONFLICTS: Rules that lead to conflicting outcomes in certain scenarios
3. AMBIGUITIES: Rules that are underspecified and could be interpreted differently
4. COVERAGE GAPS: Scenarios that no rule addresses

For each finding, cite the specific rules involved and explain the conflict.
Return valid JSON only."""

_CONTRADICTION_DETECTION_USER = """Business Rules:
---
{rules}
---

Analyze for contradictions, conflicts, ambiguities, and gaps.
Return as JSON with keys: contradictions, conflicts, ambiguities, coverage_gaps"""

_COMPLIANCE_ANALYSIS_SYSTEM = """You are a regulatory compliance expert for insurance.
Given business rules and state regulations, determine:

1. Which rules satisfy which regulations
2. Which regulations have no corresponding business rules
3. Which business rules may violate regulations
4. State-specific variations that need separate handling

Consider NAIC model regulations, state-specific DOI requirements,
and common compliance frameworks.
Return valid JSON only."""

_COMPLIANCE_ANALYSIS_USER = """Business Rules:
---
{rules}
---

State Regulations in Scope:
{regulations}

Target Jurisdictions: {jurisdictions}

Analyze compliance coverage and gaps.
Return as JSON with keys: satisfied, unaddressed, potential_violations, state_variations"""


# ─── Builder Function ─────────────────────────────────────────

def build_reasoning_extension() -> ReasoningExtension:
    """Build the insurance reasoning extension for Heart engine."""
    return ReasoningExtension(
        domain="insurance",
        prompt_templates=[
            PromptTemplate(
                name="insurance_rule_extraction",
                task="extract_rules",
                system_prompt=_RULE_EXTRACTION_SYSTEM,
                user_prompt_template=_RULE_EXTRACTION_USER,
                output_schema={
                    "type": "object",
                    "properties": {
                        "rules": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "description": {"type": "string"},
                                    "condition": {"type": "string"},
                                    "expected_result": {"type": "string"},
                                    "domain": {"type": "string"},
                                    "priority": {"type": "string"},
                                    "confidence": {"type": "string"},
                                },
                            },
                        },
                        "edge_cases": {"type": "array"},
                        "contradictions": {"type": "array"},
                        "questions_for_sme": {"type": "array"},
                    },
                },
                temperature=0.1,
                max_tokens=4096,
            ),
            PromptTemplate(
                name="insurance_test_generation",
                task="generate_tests",
                system_prompt=_TEST_GENERATION_SYSTEM,
                user_prompt_template=_TEST_GENERATION_USER,
                output_schema={
                    "type": "object",
                    "properties": {
                        "test_cases": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string"},
                                    "description": {"type": "string"},
                                    "steps": {"type": "array"},
                                    "priority": {"type": "string"},
                                    "type": {"type": "string"},
                                },
                            },
                        },
                    },
                },
                temperature=0.2,
                max_tokens=4096,
            ),
            PromptTemplate(
                name="insurance_flow_exploration",
                task="explore_flows",
                system_prompt=_EXPLORE_FLOWS_SYSTEM,
                user_prompt_template=_EXPLORE_FLOWS_USER,
                output_schema={
                    "type": "object",
                    "properties": {
                        "explored_flows": {"type": "array"},
                        "new_paths_found": {"type": "integer"},
                        "questions": {"type": "array"},
                    },
                },
                temperature=0.3,
                max_tokens=4096,
            ),
            PromptTemplate(
                name="insurance_contradiction_detection",
                task="detect_contradictions",
                system_prompt=_CONTRADICTION_DETECTION_SYSTEM,
                user_prompt_template=_CONTRADICTION_DETECTION_USER,
                output_schema={
                    "type": "object",
                    "properties": {
                        "contradictions": {"type": "array"},
                        "conflicts": {"type": "array"},
                        "ambiguities": {"type": "array"},
                        "coverage_gaps": {"type": "array"},
                    },
                },
                temperature=0.1,
                max_tokens=4096,
            ),
            PromptTemplate(
                name="insurance_compliance_analysis",
                task="analyze_compliance",
                system_prompt=_COMPLIANCE_ANALYSIS_SYSTEM,
                user_prompt_template=_COMPLIANCE_ANALYSIS_USER,
                output_schema={
                    "type": "object",
                    "properties": {
                        "satisfied": {"type": "array"},
                        "unaddressed": {"type": "array"},
                        "potential_violations": {"type": "array"},
                        "state_variations": {"type": "array"},
                    },
                },
                temperature=0.1,
                max_tokens=4096,
            ),
        ],
        supported_tasks=[
            "extract_rules",
            "generate_tests",
            "explore_flows",
            "detect_contradictions",
            "analyze_compliance",
        ],
        guardrail_rules=[
            GuardrailRule(
                name="no_real_pii",
                description="Reject or redact any real PII in prompts before sending to LLM",
                severity="critical",
                check_pattern=r"\b\d{3}-\d{2}-\d{4}\b",
            ),
            GuardrailRule(
                name="insurance_domain_relevance",
                description="Flag if extracted rules do not relate to insurance domain",
                severity="warning",
                check_pattern=None,
            ),
            GuardrailRule(
                name="rule_testability",
                description="Flag rules that cannot be translated to testable conditions",
                severity="info",
                check_pattern=None,
            ),
            GuardrailRule(
                name="jurisdiction_coverage",
                description="Warn if rules only cover a subset of target jurisdictions",
                severity="warning",
                check_pattern=None,
            ),
        ],
    )
