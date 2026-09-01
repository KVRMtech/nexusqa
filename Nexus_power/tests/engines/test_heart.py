"""
Heart Engine — Unit tests.

Tests HeartLLM stub generation, prompt templates, and request/response models.
"""

import pytest
import sys
import os
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "engines", "heart-engine"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "sdk", "nexus-sdk"))


# ─── Config ────────────────────────────────────────────────────


class TestHeartConfig:

    def test_defaults(self):
        from main import HeartConfig
        cfg = HeartConfig()
        assert cfg.engine_name == "heart"
        assert cfg.engine_port == 8004
        assert cfg.llm_temperature == 0.1
        assert cfg.llm_max_tokens == 4096
        assert cfg.llm_context_window == 32768

    def test_service_scoped_model_overrides_global(self, monkeypatch):
        from main import HeartConfig
        monkeypatch.setenv("OLLAMA_MODEL", "wrong-global")
        monkeypatch.setenv("HEART_OLLAMA_MODEL", "heart-local")
        cfg = HeartConfig()
        assert cfg.ollama_model == "heart-local"

    def test_guardrails_defaults(self):
        from main import HeartConfig
        cfg = HeartConfig()
        assert cfg.max_rules_per_extraction == 50
        assert cfg.min_confidence_threshold == 0.7
        assert cfg.require_source_reference is True
        assert cfg.max_edge_cases_per_rule == 10
        assert cfg.max_test_cases_per_rule == 20


# ─── HeartLLM Stub ────────────────────────────────────────────


class TestStubGenerate:
    """Test the development stub that returns structured JSON."""

    def setup_method(self):
        from main import HeartLLM, HeartConfig
        self.llm = HeartLLM(HeartConfig())

    def test_rule_extraction_stub_returns_valid_json(self):
        result = self.llm._stub_generate(
            "Extract business rules from transcript",
            "The premium rate is 1.75x for smokers",
        )
        parsed = json.loads(result)
        assert "rules" in parsed
        assert isinstance(parsed["rules"], list)
        assert len(parsed["rules"]) > 0

    def test_rule_extraction_stub_has_required_fields(self):
        result = self.llm._stub_generate(
            "Extract rules from this session",
            "Transcript text...",
        )
        parsed = json.loads(result)
        rule = parsed["rules"][0]
        assert "description" in rule
        assert "condition" in rule
        assert "expected_result" in rule
        assert "domain" in rule
        assert "priority" in rule
        assert "confidence" in rule

    def test_rule_extraction_stub_has_edge_cases(self):
        result = self.llm._stub_generate(
            "Extract rules from this data",
            "Anything",
        )
        parsed = json.loads(result)
        assert "edge_cases" in parsed
        assert isinstance(parsed["edge_cases"], list)

    def test_rule_extraction_stub_has_sme_questions(self):
        result = self.llm._stub_generate(
            "Extract rules from this report",
            "Anything",
        )
        parsed = json.loads(result)
        assert "questions_for_sme" in parsed
        assert isinstance(parsed["questions_for_sme"], list)

    def test_test_generation_stub(self):
        result = self.llm._stub_generate(
            "Generate test cases for the given rules",
            "BusinessRule: premium calculation",
        )
        parsed = json.loads(result)
        assert "test_cases" in parsed
        assert len(parsed["test_cases"]) > 0

    def test_test_generation_stub_has_steps(self):
        result = self.llm._stub_generate(
            "Generate test cases from requirements",
            "Business rules here",
        )
        parsed = json.loads(result)
        tc = parsed["test_cases"][0]
        assert "name" in tc
        assert "steps" in tc
        assert isinstance(tc["steps"], list)
        assert all("action" in s and "expected" in s for s in tc["steps"])

    def test_explore_flows_stub(self):
        result = self.llm._stub_generate(
            "Explore all possible flows for the UI",
            "demonstrated_flow: apply → underwrite → approve",
        )
        parsed = json.loads(result)
        assert "explored_flows" in parsed
        assert "new_paths_found" in parsed
        assert parsed["new_paths_found"] > 0

    def test_generic_stub_fallback(self):
        """Unknown prompt type should return a generic answer."""
        result = self.llm._stub_generate(
            "Summarize this document",
            "Some unrecognised prompt",
        )
        parsed = json.loads(result)
        assert "answer" in parsed
        assert parsed["confidence"] == 0.0


# ─── Prompt Templates ─────────────────────────────────────────


class TestPromptTemplates:

    def test_rule_extraction_system_prompt_exists(self):
        from main import RULE_EXTRACTION_SYSTEM
        assert "business rule" in RULE_EXTRACTION_SYSTEM.lower()
        assert "JSON" in RULE_EXTRACTION_SYSTEM

    def test_rule_extraction_user_has_placeholders(self):
        from main import RULE_EXTRACTION_USER
        assert "{transcript}" in RULE_EXTRACTION_USER
        assert "{visual_context}" in RULE_EXTRACTION_USER

    def test_test_generation_system_prompt(self):
        from main import TEST_GENERATION_SYSTEM
        assert "test case" in TEST_GENERATION_SYSTEM.lower()
        assert "HAPPY PATH" in TEST_GENERATION_SYSTEM or "happy_path" in TEST_GENERATION_SYSTEM

    def test_test_generation_user_has_placeholders(self):
        from main import TEST_GENERATION_USER
        assert "{rules}" in TEST_GENERATION_USER
        assert "{coverage_targets}" in TEST_GENERATION_USER

    def test_explore_flows_system_prompt(self):
        from main import EXPLORE_FLOWS_SYSTEM
        assert "flow" in EXPLORE_FLOWS_SYSTEM.lower()

    def test_explore_flows_user_has_placeholders(self):
        from main import EXPLORE_FLOWS_USER
        assert "{demonstrated_flow}" in EXPLORE_FLOWS_USER
        assert "{ui_screens}" in EXPLORE_FLOWS_USER
        assert "{known_rules}" in EXPLORE_FLOWS_USER


# ─── Request/Response Models ──────────────────────────────────


class TestRequestModels:

    def test_extract_rules_request_required_fields(self):
        from main import ExtractRulesRequest
        req = ExtractRulesRequest(
            tenant_id="t1",
            transcript="Transcript text",
            session_id="sess-001",
        )
        assert req.transcript == "Transcript text"
        assert req.session_id == "sess-001"
        assert req.visual_context is None
        assert req.existing_rules == []

    def test_generate_tests_request_defaults(self):
        from main import GenerateTestsRequest
        from nexus_sdk.models import BusinessRule
        rule = BusinessRule(
            rule_id="r1",
            tenant_id="t1",
            category="underwriting",
            rule_text="IF age > 18 THEN allow",
            conditions=["age > 18"],
        )
        req = GenerateTestsRequest(tenant_id="t1", rules=[rule])
        assert len(req.coverage_targets) == 4
        assert "happy_path" in req.coverage_targets

    def test_explore_flows_request(self):
        from main import ExploreFlowsRequest
        req = ExploreFlowsRequest(
            tenant_id="t1",
            demonstrated_flow={"steps": ["Step 1", "Step 2"]},
        )
        assert req.demonstrated_flow["steps"][0] == "Step 1"
        assert req.ui_screens == []
        assert req.known_rules == []

    def test_ask_heart_request(self):
        from main import AskHeartRequest
        req = AskHeartRequest(
            tenant_id="t1",
            question="What is the smoker surcharge?",
        )
        assert req.question == "What is the smoker surcharge?"
        assert req.context is None
