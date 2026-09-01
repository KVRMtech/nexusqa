"""
Mouth Engine — Modular Sub-package Tests.

Tests the report generators and HTML renderer modules refactored
from the monolithic mouth-engine/main.py.

All tests exercise actual generator logic with synthetic input data.
"""

import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "engines", "mouth-engine"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "sdk", "nexus-sdk"))


# ─── Traceability Matrix Generator ───────────────────────────


class TestTraceabilityMatrixGenerator:
    """Test TraceabilityMatrixGenerator from app.generators."""

    def test_import(self):
        from app.generators import TraceabilityMatrixGenerator, TraceabilityEntry, CoverageLevel
        assert TraceabilityMatrixGenerator is not None
        assert TraceabilityEntry is not None
        assert CoverageLevel is not None

    def test_coverage_level_values(self):
        from app.generators import CoverageLevel
        assert hasattr(CoverageLevel, "FULL")
        assert hasattr(CoverageLevel, "HIGH")
        assert hasattr(CoverageLevel, "MODERATE")
        assert hasattr(CoverageLevel, "LOW")
        assert hasattr(CoverageLevel, "CRITICAL")

    def test_generate_empty(self):
        from app.generators import TraceabilityMatrixGenerator
        gen = TraceabilityMatrixGenerator()
        result = gen.generate(rules=[], test_cases=[], test_results=[], evidence=[])
        assert "matrix" in result
        assert "summary" in result
        assert result["summary"]["total_rules"] == 0
        assert result["summary"]["total_test_cases"] == 0

    def test_generate_with_data(self):
        from app.generators import TraceabilityMatrixGenerator
        gen = TraceabilityMatrixGenerator()
        rules = [
            {"rule_id": "R1", "description": "Premium > $100"},
            {"rule_id": "R2", "description": "Age 18-65"},
        ]
        test_cases = [
            {"test_case_id": "TC1", "rule_ids": ["R1"]},
            {"test_case_id": "TC2", "rule_ids": ["R1"]},
            {"test_case_id": "TC3", "rule_ids": ["R2"]},
        ]
        test_results = [
            {"test_case_id": "TC1", "status": "passed"},
            {"test_case_id": "TC2", "status": "passed"},
            {"test_case_id": "TC3", "status": "failed"},
        ]
        evidence = [
            {"test_case_id": "TC1", "evidence_id": "EV1"},
        ]
        result = gen.generate(rules, test_cases, test_results, evidence)
        assert result["summary"]["total_rules"] == 2
        assert result["summary"]["total_test_cases"] == 3
        assert len(result["matrix"]) == 2
        # R1: 2 passed, 0 failed → FULL
        r1_entry = result["matrix"][0]
        assert r1_entry["tests_passed"] == 2
        assert r1_entry["tests_failed"] == 0
        assert r1_entry["coverage_status"] == "full"

    def test_traceability_entry_model(self):
        from app.generators import TraceabilityEntry, CoverageLevel
        entry = TraceabilityEntry(
            rule_id="R1",
            rule_description="Must validate premium",
            tests_passed=3,
            tests_failed=0,
            coverage_status=CoverageLevel.FULL,
        )
        d = entry.model_dump()
        assert d["rule_id"] == "R1"
        assert d["tests_passed"] == 3


# ─── Compliance Report Generator ─────────────────────────────


class TestComplianceReportGenerator:
    """Test ComplianceReportGenerator from app.generators."""

    def test_import(self):
        from app.generators import ComplianceReportGenerator, ComplianceItem
        assert ComplianceReportGenerator is not None
        assert ComplianceItem is not None

    def test_regulatory_domains(self):
        from app.generators import ComplianceReportGenerator
        gen = ComplianceReportGenerator()
        assert len(gen.REGULATORY_DOMAINS) == 15
        assert "rate_filing" in gen.REGULATORY_DOMAINS
        assert "privacy" in gen.REGULATORY_DOMAINS

    def test_generate_empty(self):
        from app.generators import ComplianceReportGenerator
        gen = ComplianceReportGenerator()
        result = gen.generate(rules=[], test_cases=[], test_results=[])
        assert "compliance_items" in result
        assert "summary" in result
        assert result["summary"]["total_requirements"] == 15  # 15 domains

    def test_generate_with_compliant_rule(self):
        from app.generators import ComplianceReportGenerator
        gen = ComplianceReportGenerator()
        rules = [{"rule_id": "R1", "description": "Rate filing rule", "category": "rate_filing"}]
        test_cases = [{"test_case_id": "TC1", "rule_ids": ["R1"]}]
        test_results = [{"test_case_id": "TC1", "status": "passed"}]
        result = gen.generate(rules, test_cases, test_results)
        # rate_filing should be compliant
        rate_item = next(
            (i for i in result["compliance_items"] if i["requirement_id"] == "rate_filing"),
            None,
        )
        assert rate_item is not None
        assert rate_item["status"] == "compliant"

    def test_compliance_item_model(self):
        from app.generators import ComplianceItem
        item = ComplianceItem(
            requirement_id="test_req",
            requirement_description="Test requirement",
            status="compliant",
        )
        assert item.requirement_id == "test_req"
        assert item.status == "compliant"

    def test_suggest_remediation(self):
        from app.generators import ComplianceReportGenerator
        gen = ComplianceReportGenerator()
        assert "No action" in gen._suggest_remediation("x", "compliant", [])
        assert "URGENT" in gen._suggest_remediation("x", "non_compliant", ["gap"])
        assert "Map business rules" in gen._suggest_remediation("x", "not_assessed", [])


# ─── Executive Summary Generator ─────────────────────────────


class TestExecutiveSummaryGenerator:
    """Test ExecutiveSummaryGenerator from app.generators."""

    def test_import(self):
        from app.generators import ExecutiveSummaryGenerator
        assert ExecutiveSummaryGenerator is not None

    def test_generate_healthy(self):
        from app.generators import ExecutiveSummaryGenerator
        gen = ExecutiveSummaryGenerator()
        result = gen.generate(
            session_name="Test Session",
            rules=[{"rule_id": "R1"}],
            test_cases=[{"test_case_id": "TC1"}],
            test_results=[{"test_case_id": "TC1", "status": "passed"}],
            traceability_summary={"overall_coverage_pct": 100},
            compliance_summary={"compliance_rate_pct": 100},
        )
        assert result["overall_health"] == "HEALTHY"
        assert result["key_metrics"]["pass_rate_pct"] == 100.0

    def test_generate_critical(self):
        from app.generators import ExecutiveSummaryGenerator
        gen = ExecutiveSummaryGenerator()
        result = gen.generate(
            session_name="Bad Session",
            rules=[],
            test_cases=[],
            test_results=[
                {"test_case_id": "TC1", "status": "failed", "category": "auth"},
                {"test_case_id": "TC2", "status": "failed", "category": "auth"},
                {"test_case_id": "TC3", "status": "failed", "category": "data"},
            ],
            traceability_summary={"overall_coverage_pct": 0},
            compliance_summary={"compliance_rate_pct": 0},
        )
        assert result["overall_health"] == "CRITICAL"
        assert "Do NOT proceed" in result["recommendation"]
        assert len(result["top_risks"]) >= 1

    def test_sign_off_section(self):
        from app.generators import ExecutiveSummaryGenerator
        gen = ExecutiveSummaryGenerator()
        result = gen.generate(
            session_name="S", rules=[], test_cases=[], test_results=[],
            traceability_summary={}, compliance_summary={},
        )
        assert "sign_off" in result
        assert "qa_lead" in result["sign_off"]
        assert "product_owner" in result["sign_off"]
        assert "compliance_officer" in result["sign_off"]


# ─── Test Coverage Report Generator ──────────────────────────


class TestTestCoverageReportGenerator:
    """Test TestCoverageReportGenerator from app.generators."""

    def test_import(self):
        from app.generators import TestCoverageReportGenerator
        assert TestCoverageReportGenerator is not None

    def test_coverage_types(self):
        from app.generators import TestCoverageReportGenerator
        gen = TestCoverageReportGenerator()
        assert len(gen.COVERAGE_TYPES) == 5
        assert "happy_path" in gen.COVERAGE_TYPES
        assert "boundary" in gen.COVERAGE_TYPES

    def test_generate_empty(self):
        from app.generators import TestCoverageReportGenerator
        gen = TestCoverageReportGenerator()
        result = gen.generate(rules=[], test_cases=[], test_results=[])
        assert result["summary"]["total_rules"] == 0

    def test_generate_with_data(self):
        from app.generators import TestCoverageReportGenerator
        gen = TestCoverageReportGenerator()
        rules = [{"rule_id": "R1", "description": "Test rule"}]
        test_cases = [
            {"test_case_id": "TC1", "rule_ids": ["R1"], "test_type": "happy_path"},
            {"test_case_id": "TC2", "rule_ids": ["R1"], "test_type": "boundary"},
        ]
        test_results = [
            {"test_case_id": "TC1", "status": "passed"},
            {"test_case_id": "TC2", "status": "passed"},
        ]
        result = gen.generate(rules, test_cases, test_results)
        assert result["summary"]["total_rules"] == 1
        detail = result["coverage_details"][0]
        assert detail["total_test_cases"] == 2
        assert detail["coverage_by_type"]["happy_path"] == 1
        assert detail["coverage_by_type"]["boundary"] == 1

    def test_recommend_critical(self):
        from app.generators import TestCoverageReportGenerator
        gen = TestCoverageReportGenerator()
        recs = gen._recommend(["Missing happy path test"], 30.0)
        assert any("CRITICAL" in r for r in recs)
        assert any("happy path" in r.lower() for r in recs)


# ─── Defect Summary Generator ────────────────────────────────


class TestDefectSummaryGenerator:
    """Test DefectSummaryGenerator from app.generators."""

    def test_import(self):
        from app.generators import DefectSummaryGenerator
        assert DefectSummaryGenerator is not None

    def test_generate_no_failures(self):
        from app.generators import DefectSummaryGenerator
        gen = DefectSummaryGenerator()
        result = gen.generate(
            test_cases=[{"test_case_id": "TC1", "name": "Test"}],
            test_results=[{"test_case_id": "TC1", "status": "passed"}],
        )
        assert result["summary"]["total_failures"] == 0
        assert len(result["defects"]) == 0

    def test_generate_with_failures(self):
        from app.generators import DefectSummaryGenerator
        gen = DefectSummaryGenerator()
        result = gen.generate(
            test_cases=[
                {"test_case_id": "TC1", "name": "Login test", "category": "auth", "priority": "high"},
                {"test_case_id": "TC2", "name": "Display test", "category": "ui", "priority": "low"},
            ],
            test_results=[
                {"test_case_id": "TC1", "status": "failed", "error": "Timeout waiting for element"},
                {"test_case_id": "TC2", "status": "error", "error_type": "display", "error": "Mismatch in layout"},
            ],
        )
        assert result["summary"]["total_failures"] == 2
        assert "auth" in result["by_category"]
        # Severity ordering: critical/high first
        assert result["defects"][0]["severity"] in ("critical", "high")

    def test_classify_severity_security(self):
        from app.generators import DefectSummaryGenerator
        gen = DefectSummaryGenerator()
        assert gen._classify_severity({"error_type": "security_breach"}, {}) == "critical"
        assert gen._classify_severity({"error_type": "pii_exposure"}, {}) == "critical"

    def test_suggest_root_cause(self):
        from app.generators import DefectSummaryGenerator
        gen = DefectSummaryGenerator()
        timeout_cause = gen._suggest_root_cause({"error": "Timeout exceeded"})
        assert "timeout" in timeout_cause.lower() or "performance" in timeout_cause.lower()
        null_cause = gen._suggest_root_cause({"error": "NullPointerException"})
        assert "null" in null_cause.lower()
        notfound_cause = gen._suggest_root_cause({"error": "Element not found"})
        assert "missing" in notfound_cause.lower() or "not found" in notfound_cause.lower()


# ─── HTML Renderer ────────────────────────────────────────────


class TestHTMLReportRenderer:
    """Test HTMLReportRenderer from app.renderers."""

    def test_import(self):
        from app.renderers import HTMLReportRenderer
        assert HTMLReportRenderer is not None

    def test_colors(self):
        from app.renderers import HTMLReportRenderer
        renderer = HTMLReportRenderer()
        assert "primary" in renderer.COLORS
        assert "success" in renderer.COLORS
        assert "danger" in renderer.COLORS
        assert len(renderer.COLORS) == 8

    def test_render_traceability_matrix(self):
        from app.renderers import HTMLReportRenderer
        renderer = HTMLReportRenderer()
        data = {
            "matrix": [
                {
                    "rule_id": "R1",
                    "rule_description": "Test rule",
                    "rule_priority": "high",
                    "test_case_count": 2,
                    "tests_passed": 2,
                    "tests_failed": 0,
                    "tests_not_run": 0,
                    "coverage_status": "full",
                },
            ],
            "summary": {
                "total_rules": 1,
                "rules_with_tests": 1,
                "overall_coverage_pct": 100.0,
                "overall_pass_pct": 100.0,
            },
        }
        html = renderer.render_traceability_matrix(data, {"title": "Test Matrix", "session_id": "S1"})
        assert "<!DOCTYPE html>" in html
        assert "Test Matrix" in html
        assert "R1" in html

    def test_render_executive_summary(self):
        from app.renderers import HTMLReportRenderer
        renderer = HTMLReportRenderer()
        data = {
            "session_name": "Test Session",
            "generated_at": "2024-01-01T00:00:00",
            "overall_health": "HEALTHY",
            "recommendation": "All good.",
            "key_metrics": {
                "business_rules_extracted": 10,
                "test_cases_generated": 50,
                "pass_rate_pct": 95.0,
                "compliance_rate_pct": 90.0,
            },
            "top_risks": [],
        }
        html = renderer.render_executive_summary(data, {"title": "Executive"})
        assert "HEALTHY" in html
        assert "Test Session" in html

    def test_render_compliance_report(self):
        from app.renderers import HTMLReportRenderer
        renderer = HTMLReportRenderer()
        data = {
            "compliance_items": [
                {
                    "requirement_id": "rate_filing",
                    "requirement_description": "Rate filing",
                    "jurisdiction": "ALL",
                    "status": "compliant",
                    "gaps": [],
                    "remediation": "No action needed.",
                },
            ],
            "summary": {
                "total_requirements": 1,
                "compliant": 1,
                "non_compliant": 0,
                "compliance_rate_pct": 100.0,
                "jurisdiction": "ALL",
                "assessment_date": "2024-01-01T00:00:00",
            },
        }
        html = renderer.render_compliance_report(data, {"title": "Compliance"})
        assert "rate_filing" in html
        assert "COMPLIANT" in html

    def test_render_defect_summary(self):
        from app.renderers import HTMLReportRenderer
        renderer = HTMLReportRenderer()
        data = {
            "defects": [
                {
                    "test_case_id": "TC-001",
                    "test_name": "Login test failed",
                    "severity": "high",
                    "error_message": "Element not found",
                    "suggested_root_cause": "UI changed",
                },
            ],
            "summary": {
                "total_failures": 1,
                "critical": 0,
                "high": 1,
                "medium": 0,
                "low": 0,
            },
        }
        html = renderer.render_defect_summary(data, {"title": "Defects"})
        assert "TC-001" in html
        assert "Login test failed" in html

    def test_metric_card(self):
        from app.renderers import HTMLReportRenderer
        renderer = HTMLReportRenderer()
        card = renderer._metric_card("Tests", 42, "#1a365d")
        assert "42" in card
        assert "Tests" in card

    def test_wrap_html_structure(self):
        from app.renderers import HTMLReportRenderer
        renderer = HTMLReportRenderer()
        html = renderer._wrap_html("Title", "Sub", "<p>Body</p>", {"report_id": "RPT-1"})
        assert "<!DOCTYPE html>" in html
        assert "Title" in html
        assert "RPT-1" in html
        assert "<p>Body</p>" in html


# ─── Re-exports ───────────────────────────────────────────────


class TestMouthReExports:
    """Verify all sub-package __init__ re-exports work."""

    def test_generators_re_exports(self):
        from app.generators import (
            TraceabilityMatrixGenerator,
            ComplianceReportGenerator,
            ExecutiveSummaryGenerator,
            TestCoverageReportGenerator,
            DefectSummaryGenerator,
            TraceabilityEntry,
            ComplianceItem,
            CoverageLevel,
        )
        assert all([
            TraceabilityMatrixGenerator,
            ComplianceReportGenerator,
            ExecutiveSummaryGenerator,
            TestCoverageReportGenerator,
            DefectSummaryGenerator,
            TraceabilityEntry,
            ComplianceItem,
            CoverageLevel,
        ])

    def test_renderers_re_exports(self):
        from app.renderers import HTMLReportRenderer
        assert HTMLReportRenderer is not None


# ─── Integration: main.py v0.2.0 ─────────────────────────────


class TestMouthMainImports:
    """Verify main.py v0.2.0 correctly imports from sub-packages."""

    def test_main_version(self):
        from main import MouthEngine
        engine = MouthEngine()
        assert engine.version == "0.2.0"

    def test_main_config(self):
        from main import MouthConfig
        cfg = MouthConfig()
        assert cfg.engine_name == "mouth"
        assert cfg.engine_port == 8010

    def test_main_enums(self):
        from main import ReportType, ReportFormat, ReportStatus
        assert len(ReportType) == 9
        assert len(ReportFormat) == 4
        assert len(ReportStatus) == 4

    def test_main_request_model(self):
        from main import GenerateReportRequest, ReportFormat, ReportType
        req = GenerateReportRequest(
            tenant_id="t-1",
            session_id="s-1",
            report_type=ReportType.TRACEABILITY_MATRIX,
            format=ReportFormat.HTML,
        )
        assert req.tenant_id == "t-1"
        assert req.report_type == ReportType.TRACEABILITY_MATRIX

    def test_main_metadata_model(self):
        from main import ReportMetadata, ReportType, ReportFormat
        meta = ReportMetadata(
            report_id="RPT-001",
            report_type=ReportType.EXECUTIVE_SUMMARY,
            format=ReportFormat.JSON,
            title="Test",
            session_id="s-1",
            tenant_id="t-1",
            generated_at="2024-01-01T00:00:00",
            generated_by="test-user",
        )
        assert meta.report_id == "RPT-001"

    def test_main_imports_generators(self):
        from main import MouthEngine
        engine = MouthEngine()
        assert engine.traceability_gen is not None
        assert engine.compliance_gen is not None
        assert engine.executive_gen is not None
        assert engine.coverage_gen is not None
        assert engine.defect_gen is not None

    def test_main_imports_renderer(self):
        from main import MouthEngine
        engine = MouthEngine()
        assert engine.html_renderer is not None
