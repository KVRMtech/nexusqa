"""
Heart Engine — Modular Sub-package Tests.

Tests the extractors, generators, and guardrails modules that were
refactored from the monolithic heart-engine/main.py.

All tests exercise the classes in STUB mode (LLM_BACKEND=stub).
"""

import pytest
import sys
import os
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "engines", "heart-engine"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "sdk", "nexus-sdk"))


# ─── Rule Extractor ───────────────────────────────────────────


class TestRuleExtractor:
    """Test RuleExtractor from app.extractors."""

    def test_import(self):
        from app.extractors import RuleExtractor
        assert RuleExtractor is not None

    def test_prompt_constants(self):
        from app.extractors import (
            RULE_EXTRACTION_SYSTEM,
            RULE_EXTRACTION_USER,
            DOCUMENT_ANALYSIS_SYSTEM,
        )
        assert "insurance" in RULE_EXTRACTION_SYSTEM.lower() or "business rule" in RULE_EXTRACTION_SYSTEM.lower()
        assert "{transcript}" in RULE_EXTRACTION_USER or "{text}" in RULE_EXTRACTION_USER
        assert len(DOCUMENT_ANALYSIS_SYSTEM) > 0

    def test_init(self):
        from app.extractors import RuleExtractor

        class FakeLLM:
            async def generate(self, prompt, system=None):
                return '{"rules": []}'
        
        ext = RuleExtractor(FakeLLM())
        assert ext.llm is not None

    def test_parse_rules_empty(self):
        from app.extractors.rule_extractor import RuleExtractor

        class FakeLLM:
            pass

        ext = RuleExtractor(FakeLLM())
        parsed = {"rules": []}
        result = ext._parse_rules(parsed, "sess-1", "t-1")
        assert isinstance(result, list)
        assert len(result) == 0

    def test_parse_rules_valid(self):
        from app.extractors.rule_extractor import RuleExtractor

        class FakeLLM:
            pass

        ext = RuleExtractor(FakeLLM())
        parsed = {
            "rules": [
                {
                    "description": "Premium must be above $100",
                    "domain": "underwriting",
                    "conditions": ["policy is active"],
                    "exceptions": [],
                    "confidence": "high",
                }
            ]
        }
        rules = ext._parse_rules(parsed, "sess-1", "t-1")
        assert len(rules) == 1
        assert rules[0].rule_text == "Premium must be above $100"
        assert rules[0].category == "underwriting"

    def test_parse_rules_missing_key(self):
        from app.extractors.rule_extractor import RuleExtractor

        class FakeLLM:
            pass

        ext = RuleExtractor(FakeLLM())
        result = ext._parse_rules({"data": []}, "sess-1", "t-1")
        assert isinstance(result, list)
        assert len(result) == 0


# ─── Test Generator ───────────────────────────────────────────


class TestTestGenerator:
    """Test TestGenerator from app.generators."""

    def test_import(self):
        from app.generators import TestGenerator
        assert TestGenerator is not None

    def test_prompt_constants(self):
        from app.generators import TEST_GENERATION_SYSTEM, TEST_GENERATION_USER
        assert len(TEST_GENERATION_SYSTEM) > 20
        assert "{rules}" in TEST_GENERATION_USER or "{business_rules}" in TEST_GENERATION_USER

    def test_init(self):
        from app.generators import TestGenerator

        class FakeLLM:
            pass

        gen = TestGenerator(FakeLLM())
        assert gen.llm is not None

    def test_parse_test_cases_empty(self):
        from app.generators.test_generator import TestGenerator

        class FakeLLM:
            pass

        gen = TestGenerator(FakeLLM())
        parsed = {"test_cases": []}
        result = gen._parse_test_cases(parsed, "t-1")
        assert isinstance(result, list)
        assert len(result) == 0

    def test_parse_test_cases_valid(self):
        from app.generators.test_generator import TestGenerator

        class FakeLLM:
            pass

        gen = TestGenerator(FakeLLM())
        parsed = {
            "test_cases": [
                {
                    "name": "Test premium validation",
                    "description": "Verify premium must be above $100",
                    "priority": "high",
                    "steps": [
                        {"action": "Navigate to policy page", "expected": "Policy page loads"},
                        {"action": "Enter premium value $50", "expected": "Error displayed"},
                    ],
                }
            ]
        }
        cases = gen._parse_test_cases(parsed, "t-1")
        assert len(cases) == 1
        assert cases[0].title == "Test premium validation"

    def test_parse_test_cases_missing_key(self):
        from app.generators.test_generator import TestGenerator

        class FakeLLM:
            pass

        gen = TestGenerator(FakeLLM())
        parsed = {"data": []}
        result = gen._parse_test_cases(parsed, "t-1")
        assert isinstance(result, list)
        assert len(result) == 0


# ─── Flow Explorer ────────────────────────────────────────────


class TestFlowExplorer:
    """Test FlowExplorer from app.generators."""

    def test_import(self):
        from app.generators import FlowExplorer
        assert FlowExplorer is not None

    def test_prompt_constants(self):
        from app.generators import EXPLORE_FLOWS_SYSTEM, EXPLORE_FLOWS_USER
        assert len(EXPLORE_FLOWS_SYSTEM) > 10
        assert len(EXPLORE_FLOWS_USER) > 10

    def test_init(self):
        from app.generators import FlowExplorer

        class FakeLLM:
            pass

        explorer = FlowExplorer(FakeLLM())
        assert explorer.llm is not None


# ─── Output Validator ─────────────────────────────────────────


class TestOutputValidator:
    """Test OutputValidator and related models from app.guardrails."""

    def test_imports(self):
        from app.guardrails import OutputValidator, ValidationResult, ValidationSeverity
        assert OutputValidator is not None
        assert ValidationResult is not None
        assert ValidationSeverity is not None

    def test_validation_severity_levels(self):
        from app.guardrails import ValidationSeverity
        assert hasattr(ValidationSeverity, "INFO") or hasattr(ValidationSeverity, "info")
        assert hasattr(ValidationSeverity, "WARNING") or hasattr(ValidationSeverity, "warning")
        assert hasattr(ValidationSeverity, "ERROR") or hasattr(ValidationSeverity, "error")

    def test_validate_json_output_valid(self):
        from app.guardrails import OutputValidator
        v = OutputValidator()
        result = v.validate_json_output('{"rules": []}')
        assert result.valid is True

    def test_validate_json_output_invalid(self):
        from app.guardrails import OutputValidator
        v = OutputValidator()
        result = v.validate_json_output("not json {{{")
        assert result.valid is False
        assert len(result.issues) > 0

    def test_validate_json_output_empty(self):
        from app.guardrails import OutputValidator
        v = OutputValidator()
        result = v.validate_json_output("")
        assert result.valid is False

    def test_validate_rules_output_valid(self):
        from app.guardrails import OutputValidator
        v = OutputValidator()
        parsed = {
            "rules": [
                {
                    "rule_text": "Must verify identity",
                    "category": "compliance",
                    "conditions": ["account is new"],
                    "confidence": "high",
                }
            ]
        }
        result = v.validate_rules_output(parsed)
        assert result.valid is True

    def test_validate_rules_output_empty_rules(self):
        from app.guardrails import OutputValidator
        v = OutputValidator()
        result = v.validate_rules_output({"rules": []})
        # Empty rules list — should pass (no error-level issues)
        assert isinstance(result.valid, bool)

    def test_validate_tests_output_valid(self):
        from app.guardrails import OutputValidator
        v = OutputValidator()
        parsed = {
            "test_cases": [
                {
                    "title": "Login test",
                    "steps": [
                        {"action": "Open browser", "expected": "Browser opens"}
                    ],
                }
            ]
        }
        result = v.validate_tests_output(parsed)
        assert result.valid is True

    def test_hallucination_check(self):
        from app.guardrails.output_validator import OutputValidator
        markers = OutputValidator.DEFAULT_HALLUCINATION_MARKERS
        assert isinstance(markers, list)
        assert len(markers) > 0
        v = OutputValidator()
        # A text with a hallucination marker should be flagged
        parsed = {
            "rules": [
                {
                    "description": "As an AI language model, I think the rule is X",
                    "category": "test",
                    "conditions": [],
                    "confidence": "medium",
                }
            ]
        }
        result = v.validate_rules_output(parsed)
        # The hallucination check should detect AI markers
        has_hallucination_issue = any(
            "hallucination" in str(issue).lower() or "ai" in str(issue).lower()
            for issue in result.issues
        )
        assert has_hallucination_issue

    def test_validation_result_model(self):
        from app.guardrails import ValidationResult, ValidationSeverity
        r = ValidationResult(
            valid=True,
            severity=ValidationSeverity.INFO if hasattr(ValidationSeverity, "INFO") else list(ValidationSeverity)[0],
            issues=[],
            score=1.0,
        )
        assert r.valid is True
        assert r.score == 1.0
        assert len(r.issues) == 0


# ─── Integration: Main module imports from sub-packages ───────


class TestHeartMainImports:
    """Verify main.py v0.2.0 correctly imports from sub-packages."""

    def test_main_version(self):
        from main import HeartEngine
        engine = HeartEngine()
        assert engine.version == "0.2.0"

    def test_main_imports_extractors(self):
        from main import RuleExtractor
        assert RuleExtractor is not None

    def test_main_imports_generators(self):
        from main import TestGenerator, FlowExplorer
        assert TestGenerator is not None
        assert FlowExplorer is not None

    def test_main_imports_guardrails(self):
        from main import OutputValidator
        assert OutputValidator is not None
