"""
QA Report Type Extension for Mouth Engine.

Extracted from engines/mouth-engine/main.py ReportType enum and report generation logic.
Defines QA/insurance-specific report types with sections and required inputs.
"""

from __future__ import annotations

from nexus_sdk.plugins.extensions import ReportExtension, ReportTypeDefinition


def build_report_extension() -> ReportExtension:
    """Build the QA report extension for Mouth engine."""
    return ReportExtension(
        domain="insurance",
        report_types=[
            ReportTypeDefinition(
                name="traceability_matrix",
                display_name="Traceability Matrix",
                description=(
                    "Full traceability from business rule → test case → test result → evidence. "
                    "Maps every extracted rule to its verification status."
                ),
                category="compliance",
                supported_formats=["html", "pdf", "json", "csv"],
                template_name="traceability_matrix.html.j2",
                required_inputs=["rules", "test_cases", "test_results"],
                sections=[
                    "executive_summary",
                    "rule_coverage_overview",
                    "detailed_traceability",
                    "untested_rules",
                    "coverage_metrics",
                    "recommendations",
                ],
            ),
            ReportTypeDefinition(
                name="compliance_report",
                display_name="Compliance Report",
                description=(
                    "State filing readiness assessment. Maps business rules to regulatory "
                    "requirements and identifies gaps by jurisdiction."
                ),
                category="compliance",
                supported_formats=["html", "pdf", "json"],
                template_name="compliance_report.html.j2",
                required_inputs=["rules", "test_results", "jurisdictions"],
                sections=[
                    "executive_summary",
                    "regulatory_framework",
                    "state_by_state_analysis",
                    "gap_analysis",
                    "remediation_plan",
                    "sign_off_section",
                ],
            ),
            ReportTypeDefinition(
                name="executive_summary",
                display_name="Executive Summary",
                description=(
                    "High-level dashboard for C-suite and stakeholders. "
                    "Key metrics, risk indicators, and go/no-go recommendation."
                ),
                category="management",
                supported_formats=["html", "pdf"],
                template_name="executive_summary.html.j2",
                required_inputs=["rules", "test_cases", "test_results"],
                sections=[
                    "key_metrics_dashboard",
                    "risk_indicators",
                    "coverage_summary",
                    "timeline_progress",
                    "go_no_go_recommendation",
                ],
            ),
            ReportTypeDefinition(
                name="test_coverage",
                display_name="Test Coverage Report",
                description=(
                    "Detailed test coverage analysis: rule coverage percentage, "
                    "coverage by domain, coverage by priority, gap identification."
                ),
                category="testing",
                supported_formats=["html", "pdf", "json"],
                template_name="test_coverage.html.j2",
                required_inputs=["rules", "test_cases"],
                sections=[
                    "overall_coverage",
                    "coverage_by_domain",
                    "coverage_by_priority",
                    "uncovered_rules",
                    "coverage_trend",
                    "recommendations",
                ],
            ),
            ReportTypeDefinition(
                name="defect_summary",
                display_name="Defect Summary Report",
                description=(
                    "Failed test analysis with root cause categorization, "
                    "impact assessment, and resolution recommendations."
                ),
                category="testing",
                supported_formats=["html", "pdf", "json"],
                template_name="defect_summary.html.j2",
                required_inputs=["test_results", "evidence"],
                sections=[
                    "defect_overview",
                    "severity_distribution",
                    "root_cause_analysis",
                    "impacted_rules",
                    "defect_details",
                    "resolution_recommendations",
                ],
            ),
            ReportTypeDefinition(
                name="audit_package",
                display_name="Audit-Ready Package",
                description=(
                    "Immutable evidence package with digital signatures. "
                    "Complete audit trail suitable for regulatory examination."
                ),
                category="compliance",
                supported_formats=["html", "pdf"],
                template_name="audit_package.html.j2",
                required_inputs=[
                    "rules", "test_cases", "test_results", "evidence",
                    "session_metadata",
                ],
                sections=[
                    "package_manifest",
                    "methodology_description",
                    "complete_traceability",
                    "evidence_index",
                    "test_execution_log",
                    "data_integrity_verification",
                    "attestation_sign_off",
                ],
            ),
            ReportTypeDefinition(
                name="regulatory_gap",
                display_name="Regulatory Gap Analysis",
                description=(
                    "Identifies which regulatory requirements lack test coverage "
                    "by state and product, with risk-ranked remediation priorities."
                ),
                category="compliance",
                supported_formats=["html", "pdf", "json"],
                template_name="regulatory_gap.html.j2",
                required_inputs=["rules", "test_cases", "jurisdictions", "products"],
                sections=[
                    "gap_summary",
                    "state_product_matrix",
                    "critical_gaps",
                    "remediation_priorities",
                    "estimated_effort",
                ],
            ),
            ReportTypeDefinition(
                name="rate_validation",
                display_name="Rate Validation Report",
                description=(
                    "Expected vs actual premium comparison across rate tables. "
                    "Validates that system-calculated premiums match filed rates."
                ),
                category="actuarial",
                supported_formats=["html", "pdf", "json", "csv"],
                template_name="rate_validation.html.j2",
                required_inputs=["rate_tables", "calculated_premiums", "tolerances"],
                sections=[
                    "validation_summary",
                    "pass_fail_overview",
                    "product_by_product",
                    "variance_analysis",
                    "boundary_case_results",
                    "state_specific_findings",
                ],
            ),
            ReportTypeDefinition(
                name="full_session_report",
                display_name="Full Session Report",
                description=(
                    "Complete end-to-end report for an entire KT + QA session. "
                    "Combines all individual reports into one comprehensive document."
                ),
                category="management",
                supported_formats=["html", "pdf"],
                template_name="full_session_report.html.j2",
                required_inputs=[
                    "session_metadata", "rules", "test_cases",
                    "test_results", "evidence",
                ],
                sections=[
                    "session_overview",
                    "knowledge_capture_summary",
                    "rule_extraction_results",
                    "test_generation_results",
                    "test_execution_results",
                    "coverage_analysis",
                    "compliance_assessment",
                    "recommendations",
                    "appendices",
                ],
            ),
        ],
        branding={
            "platform_name": "Nexus QA Platform",
            "report_footer": "Generated by Nexus QA — Confidential",
            "color_primary": "#1E40AF",
            "color_secondary": "#3B82F6",
            "color_success": "#10B981",
            "color_warning": "#F59E0B",
            "color_danger": "#EF4444",
        },
    )
