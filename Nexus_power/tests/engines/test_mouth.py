"""
Mouth Engine — Unit tests.

Tests all 5 report generators, the HTML renderer, enums, and models.
"""

import pytest
import sys
import os
import uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "engines", "mouth-engine"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "sdk", "nexus-sdk"))


# ─── Enums ────────────────────────────────────────────────────


class TestReportType:

    def test_values(self):
        from main import ReportType
        assert ReportType.TRACEABILITY_MATRIX == "traceability_matrix"
        assert ReportType.COMPLIANCE_REPORT == "compliance_report"
        assert ReportType.EXECUTIVE_SUMMARY == "executive_summary"
        assert ReportType.TEST_COVERAGE == "test_coverage"
        assert ReportType.DEFECT_SUMMARY == "defect_summary"

    def test_count(self):
        from main import ReportType
        assert len(ReportType) == 9


class TestReportFormat:

    def test_values(self):
        from main import ReportFormat
        assert ReportFormat.HTML == "html"
        assert ReportFormat.PDF == "pdf"
        assert ReportFormat.JSON == "json"
        assert ReportFormat.CSV == "csv"


class TestCoverageLevel:

    def test_values(self):
        from main import CoverageLevel
        assert CoverageLevel.FULL == "full"
        assert CoverageLevel.HIGH == "high"
        assert CoverageLevel.MODERATE == "moderate"
        assert CoverageLevel.LOW == "low"
        assert CoverageLevel.CRITICAL == "critical"


class TestReportStatus:

    def test_values(self):
        from main import ReportStatus
        assert ReportStatus.QUEUED == "queued"
        assert ReportStatus.GENERATING == "generating"
        assert ReportStatus.COMPLETED == "completed"
        assert ReportStatus.FAILED == "failed"


# ─── Fixtures ──────────────────────────────────────────────────

def _sample_rules():
    return [
        {"rule_id": "R001", "description": "Premium must be >= 0", "priority": "high", "source": "KT Session 1"},
        {"rule_id": "R002", "description": "Age must be 0-99", "priority": "critical", "source": "KT Session 1"},
        {"rule_id": "R003", "description": "Smoker surcharge 1.75x", "priority": "high", "source": "KT Session 2"},
    ]

def _sample_test_cases():
    return [
        {"test_case_id": "TC001", "rule_ids": ["R001"], "name": "Premium positive", "test_type": "happy_path"},
        {"test_case_id": "TC002", "rule_ids": ["R001"], "name": "Premium boundary 0", "test_type": "boundary"},
        {"test_case_id": "TC003", "rule_ids": ["R002"], "name": "Age 0 valid", "test_type": "boundary"},
        {"test_case_id": "TC004", "rule_ids": ["R002"], "name": "Age 100 invalid", "test_type": "negative"},
        {"test_case_id": "TC005", "rule_ids": ["R003"], "name": "Smoker rate", "test_type": "happy_path"},
    ]

def _sample_results():
    return [
        {"test_case_id": "TC001", "status": "passed", "completed_at": "2024-01-01T10:00:00Z"},
        {"test_case_id": "TC002", "status": "passed", "completed_at": "2024-01-01T10:01:00Z"},
        {"test_case_id": "TC003", "status": "passed", "completed_at": "2024-01-01T10:02:00Z"},
        {"test_case_id": "TC004", "status": "failed", "completed_at": "2024-01-01T10:03:00Z",
         "error": "Expected rejection but got acceptance", "error_type": "assertion_failure"},
        {"test_case_id": "TC005", "status": "passed", "completed_at": "2024-01-01T10:04:00Z"},
    ]

def _sample_evidence():
    return [
        {"evidence_id": "EV001", "test_case_id": "TC001", "type": "screenshot"},
        {"evidence_id": "EV002", "test_case_id": "TC003", "type": "log"},
    ]


# ─── TraceabilityMatrixGenerator ──────────────────────────────


class TestTraceabilityMatrix:

    def setup_method(self):
        from main import TraceabilityMatrixGenerator
        self.gen = TraceabilityMatrixGenerator()

    def test_generate_basic(self):
        result = self.gen.generate(
            _sample_rules(), _sample_test_cases(), _sample_results(), _sample_evidence(),
        )
        assert "matrix" in result
        assert "summary" in result
        assert len(result["matrix"]) == 3  # 3 rules

    def test_summary_counts(self):
        result = self.gen.generate(
            _sample_rules(), _sample_test_cases(), _sample_results(), _sample_evidence(),
        )
        summary = result["summary"]
        assert summary["total_rules"] == 3
        assert summary["total_test_cases"] == 5
        assert summary["total_evidence_items"] == 2

    def test_rule_with_all_passing(self):
        result = self.gen.generate(
            _sample_rules(), _sample_test_cases(), _sample_results(), _sample_evidence(),
        )
        # R001 has TC001 (passed) and TC002 (passed) → FULL coverage
        r001 = next(e for e in result["matrix"] if e["rule_id"] == "R001")
        assert r001["tests_passed"] == 2
        assert r001["tests_failed"] == 0
        assert r001["coverage_status"] == "full"

    def test_rule_with_failure(self):
        result = self.gen.generate(
            _sample_rules(), _sample_test_cases(), _sample_results(), _sample_evidence(),
        )
        # R002 has TC003 (passed) and TC004 (failed) → not full
        r002 = next(e for e in result["matrix"] if e["rule_id"] == "R002")
        assert r002["tests_passed"] == 1
        assert r002["tests_failed"] == 1

    def test_no_rules_produces_empty_matrix(self):
        result = self.gen.generate([], [], [], [])
        assert result["matrix"] == []
        assert result["summary"]["total_rules"] == 0

    def test_rule_with_no_tests(self):
        rules = [{"rule_id": "R999", "description": "Orphan rule"}]
        result = self.gen.generate(rules, [], [], [])
        entry = result["matrix"][0]
        assert entry["test_case_count"] == 0
        assert entry["coverage_status"] == "critical"


# ─── ComplianceReportGenerator ────────────────────────────────


class TestComplianceReport:

    def setup_method(self):
        from main import ComplianceReportGenerator
        self.gen = ComplianceReportGenerator()

    def test_generate_basic(self):
        result = self.gen.generate(
            _sample_rules(), _sample_test_cases(), _sample_results(),
        )
        assert "compliance_items" in result
        assert "summary" in result
        assert len(result["compliance_items"]) > 0

    def test_summary_fields(self):
        result = self.gen.generate(
            _sample_rules(), _sample_test_cases(), _sample_results(),
        )
        summary = result["summary"]
        assert "total_requirements" in summary
        assert "assessed" in summary
        assert "compliant" in summary
        assert "compliance_rate_pct" in summary

    def test_state_filter(self):
        result = self.gen.generate(
            _sample_rules(), _sample_test_cases(), _sample_results(),
            state_filter="NY",
        )
        assert result["summary"]["jurisdiction"] == "NY"
        for item in result["compliance_items"]:
            assert item["jurisdiction"] == "NY"

    def test_regulatory_domains_coverage(self):
        from main import ComplianceReportGenerator
        domains = ComplianceReportGenerator.REGULATORY_DOMAINS
        assert "rate_filing" in domains
        assert "suitability" in domains
        assert "free_look" in domains


# ─── ExecutiveSummaryGenerator ─────────────────────────────────


class TestExecutiveSummary:

    def setup_method(self):
        from main import ExecutiveSummaryGenerator
        self.gen = ExecutiveSummaryGenerator()

    def test_generate_healthy(self):
        result = self.gen.generate(
            session_name="KT Session 1",
            rules=_sample_rules(),
            test_cases=_sample_test_cases(),
            test_results=[{"status": "passed"} for _ in range(20)],
            traceability_summary={"overall_coverage_pct": 95},
            compliance_summary={"compliance_rate_pct": 90},
        )
        assert result["overall_health"] == "HEALTHY"
        assert result["health_color"] == "green"
        assert result["key_metrics"]["pass_rate_pct"] == 100.0

    def test_generate_critical(self):
        result = self.gen.generate(
            session_name="Bad Session",
            rules=_sample_rules(),
            test_cases=_sample_test_cases(),
            test_results=[{"status": "failed"} for _ in range(10)],
            traceability_summary={"overall_coverage_pct": 10},
            compliance_summary={"compliance_rate_pct": 5},
        )
        assert result["overall_health"] == "CRITICAL"
        assert "Do NOT proceed" in result["recommendation"]

    def test_sign_off_section(self):
        result = self.gen.generate(
            session_name="X",
            rules=[], test_cases=[], test_results=[],
            traceability_summary={}, compliance_summary={},
        )
        assert "sign_off" in result
        assert "qa_lead" in result["sign_off"]
        assert "compliance_officer" in result["sign_off"]

    def test_top_risks(self):
        result = self.gen.generate(
            session_name="Risk Session",
            rules=_sample_rules(),
            test_cases=_sample_test_cases(),
            test_results=[
                {"status": "failed", "category": "underwriting"} for _ in range(5)
            ] + [
                {"status": "failed", "category": "billing"} for _ in range(2)
            ],
            traceability_summary={},
            compliance_summary={},
        )
        assert len(result["top_risks"]) >= 1
        categories = [r["area"] for r in result["top_risks"]]
        assert "underwriting" in categories


# ─── TestCoverageReportGenerator ───────────────────────────────


class TestCoverageReport:

    def setup_method(self):
        from main import TestCoverageReportGenerator
        self.gen = TestCoverageReportGenerator()

    def test_generate_basic(self):
        result = self.gen.generate(
            _sample_rules(), _sample_test_cases(), _sample_results(),
        )
        assert "coverage_details" in result
        assert "summary" in result
        assert len(result["coverage_details"]) == 3

    def test_gaps_detected(self):
        rules = [{"rule_id": "R100", "description": "Test rule"}]
        # Only a happy_path test, no boundary/negative/edge_case
        tcs = [{"test_case_id": "TC100", "rule_ids": ["R100"], "test_type": "happy_path"}]
        trs = [{"test_case_id": "TC100", "status": "passed"}]

        result = self.gen.generate(rules, tcs, trs)
        detail = result["coverage_details"][0]
        assert "Missing boundary value tests" in detail["gaps"]
        assert "Missing negative/invalid input tests" in detail["gaps"]

    def test_full_coverage_no_gaps(self):
        rules = [{"rule_id": "R200", "description": "Well-covered rule"}]
        tcs = [
            {"test_case_id": f"TC200-{i}", "rule_ids": ["R200"], "test_type": t}
            for i, t in enumerate(["happy_path", "boundary", "negative", "edge_case", "regression"])
        ]
        trs = [{"test_case_id": tc["test_case_id"], "status": "passed"} for tc in tcs]

        result = self.gen.generate(rules, tcs, trs)
        detail = result["coverage_details"][0]
        assert detail["gaps"] == []
        assert detail["types_covered"] == 5

    def test_confidence_calculation(self):
        rules = [{"rule_id": "R300", "description": "High confidence"}]
        tcs = [
            {"test_case_id": f"TC300-{i}", "rule_ids": ["R300"], "test_type": t}
            for i, t in enumerate(["happy_path", "boundary", "negative", "edge_case", "regression"])
        ]
        trs = [{"test_case_id": tc["test_case_id"], "status": "passed"} for tc in tcs]

        result = self.gen.generate(rules, tcs, trs)
        detail = result["coverage_details"][0]
        assert detail["confidence_pct"] == 100.0


# ─── DefectSummaryGenerator ───────────────────────────────────


class TestDefectSummary:

    def setup_method(self):
        from main import DefectSummaryGenerator
        self.gen = DefectSummaryGenerator()

    def test_generate_with_failures(self):
        tcs = [
            {"test_case_id": "TC1", "name": "Login test", "category": "auth", "priority": "high"},
            {"test_case_id": "TC2", "name": "Premium calc", "category": "rating", "priority": "critical"},
        ]
        trs = [
            {"test_case_id": "TC1", "status": "failed", "error": "Timeout waiting for element"},
            {"test_case_id": "TC2", "status": "error", "error": "null reference in calculation"},
        ]
        result = self.gen.generate(tcs, trs)
        assert result["summary"]["total_failures"] == 2
        assert "by_category" in result
        assert "by_severity" in result

    def test_no_failures(self):
        trs = [{"test_case_id": "TC1", "status": "passed"}]
        result = self.gen.generate([], trs)
        assert result["summary"]["total_failures"] == 0
        assert result["defects"] == []

    def test_severity_classification(self):
        tcs = [{"test_case_id": "TC1", "name": "PII leak", "priority": "critical"}]
        trs = [{"test_case_id": "TC1", "status": "failed", "error_type": "security_breach"}]
        result = self.gen.generate(tcs, trs)
        assert result["defects"][0]["severity"] == "critical"

    def test_root_cause_suggestion(self):
        tcs = [{"test_case_id": "TC1", "name": "Slow page"}]
        trs = [{"test_case_id": "TC1", "status": "failed", "error": "Request timeout after 30s"}]
        result = self.gen.generate(tcs, trs)
        assert "timeout" in result["defects"][0]["suggested_root_cause"].lower()

    def test_sorted_by_severity(self):
        tcs = [
            {"test_case_id": "TC1", "priority": "low"},
            {"test_case_id": "TC2", "priority": "critical"},
        ]
        trs = [
            {"test_case_id": "TC1", "status": "failed", "error_type": "ui_glitch"},
            {"test_case_id": "TC2", "status": "failed", "error_type": "pii_leak"},
        ]
        result = self.gen.generate(tcs, trs)
        severities = [d["severity"] for d in result["defects"]]
        # critical should come before low
        assert severities.index("critical") < severities.index("low")


# ─── HTMLReportRenderer ───────────────────────────────────────


class TestHTMLReportRenderer:

    def setup_method(self):
        from main import HTMLReportRenderer
        self.renderer = HTMLReportRenderer()

    def _make_metadata(self):
        return {
            "report_id": str(uuid.uuid4()),
            "title": "Test Report",
            "session_id": "sess-001",
            "tenant_id": "t1",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "generated_by": "test@nexus.test",
        }

    def test_render_traceability_matrix(self):
        from main import TraceabilityMatrixGenerator
        gen = TraceabilityMatrixGenerator()
        data = gen.generate(
            _sample_rules(), _sample_test_cases(), _sample_results(), _sample_evidence(),
        )
        html = self.renderer.render_traceability_matrix(data, self._make_metadata())
        assert "<html" in html.lower()
        assert "R001" in html
        assert "Traceability" in html or "traceability" in html

    def test_render_executive_summary(self):
        from main import ExecutiveSummaryGenerator
        gen = ExecutiveSummaryGenerator()
        data = gen.generate(
            session_name="KT Session",
            rules=_sample_rules(),
            test_cases=_sample_test_cases(),
            test_results=_sample_results(),
            traceability_summary={"overall_coverage_pct": 80},
            compliance_summary={"compliance_rate_pct": 75},
        )
        html = self.renderer.render_executive_summary(data, self._make_metadata())
        assert "<html" in html.lower()
        assert "Executive Summary" in html or "KT Session" in html

    def test_html_contains_inline_styles(self):
        """Reports must have inline CSS for standalone viewing."""
        from main import TraceabilityMatrixGenerator
        gen = TraceabilityMatrixGenerator()
        data = gen.generate(
            _sample_rules(), _sample_test_cases(), _sample_results(), _sample_evidence(),
        )
        html = self.renderer.render_traceability_matrix(data, self._make_metadata())
        assert "style=" in html  # Inline styles present


# ─── Request Model ─────────────────────────────────────────────


class TestGenerateReportRequest:

    def test_defaults(self):
        from main import GenerateReportRequest, ReportType, ReportFormat
        req = GenerateReportRequest(
            tenant_id="t1",
            session_id="sess-001",
            report_type=ReportType.TRACEABILITY_MATRIX,
        )
        assert req.format == ReportFormat.HTML
        assert req.rules == []
        assert req.test_cases == []
        assert req.include_evidence is True
        assert req.include_recommendations is True
        assert req.include_sign_off_section is True

    def test_custom_values(self):
        from main import GenerateReportRequest, ReportType, ReportFormat
        req = GenerateReportRequest(
            tenant_id="t1",
            session_id="sess-001",
            report_type=ReportType.COMPLIANCE_REPORT,
            format=ReportFormat.PDF,
            title="Custom Title",
            state_filter="CA",
        )
        assert req.format == ReportFormat.PDF
        assert req.title == "Custom Title"
        assert req.state_filter == "CA"
