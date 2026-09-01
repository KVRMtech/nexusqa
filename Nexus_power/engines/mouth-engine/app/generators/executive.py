"""
Executive Summary Generator.

Produces C-suite-level one-page overviews: what was tested, what
passed, what's at risk, and what needs attention — with zero
technical jargon.
"""

from __future__ import annotations

from datetime import datetime, timezone
from collections import defaultdict


class ExecutiveSummaryGenerator:
    """
    Generates C-suite-level executive summaries.

    One-page overview: What was tested, what passed, what's at risk,
    what needs attention — with zero technical jargon.
    """

    def generate(
        self,
        session_name: str,
        rules: list[dict],
        test_cases: list[dict],
        test_results: list[dict],
        traceability_summary: dict,
        compliance_summary: dict,
    ) -> dict:
        """Generate executive summary."""

        total_tests = len(test_results)
        passed = sum(1 for r in test_results if r.get("status") == "passed")
        failed = sum(
            1 for r in test_results
            if r.get("status") in ("failed", "error")
        )
        not_run = total_tests - passed - failed

        pass_rate = round((passed / max(total_tests, 1)) * 100, 1)

        # Determine overall health
        if pass_rate >= 95:
            health = "HEALTHY"
            health_color = "green"
            recommendation = (
                "System is performing within acceptable parameters. "
                "Ready for release consideration."
            )
        elif pass_rate >= 80:
            health = "NEEDS ATTENTION"
            health_color = "yellow"
            recommendation = (
                "Some test failures detected. Review failing areas "
                "before release."
            )
        elif pass_rate >= 60:
            health = "AT RISK"
            health_color = "orange"
            recommendation = (
                "Significant test failures. Remediation required "
                "before release."
            )
        else:
            health = "CRITICAL"
            health_color = "red"
            recommendation = (
                "Major quality issues detected. Do NOT proceed to "
                "production without remediation."
            )

        # Identify top risks
        risks: list[dict] = []
        failed_results = [
            r for r in test_results
            if r.get("status") in ("failed", "error")
        ]
        risk_categories: dict[str, int] = defaultdict(int)
        for r in failed_results:
            category = r.get("category", r.get("test_type", "uncategorized"))
            risk_categories[category] += 1

        for category, count in sorted(
            risk_categories.items(), key=lambda x: -x[1],
        )[:5]:
            risks.append({
                "area": category,
                "failed_tests": count,
                "severity": (
                    "high" if count > 5
                    else "medium" if count > 2
                    else "low"
                ),
            })

        return {
            "session_name": session_name,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "overall_health": health,
            "health_color": health_color,
            "key_metrics": {
                "business_rules_extracted": len(rules),
                "test_cases_generated": len(test_cases),
                "tests_executed": total_tests,
                "tests_passed": passed,
                "tests_failed": failed,
                "tests_not_run": not_run,
                "pass_rate_pct": pass_rate,
                "rule_coverage_pct": traceability_summary.get(
                    "overall_coverage_pct", 0,
                ),
                "compliance_rate_pct": compliance_summary.get(
                    "compliance_rate_pct", 0,
                ),
            },
            "top_risks": risks,
            "recommendation": recommendation,
            "sign_off": {
                "qa_lead": {"name": "", "signed": False, "date": ""},
                "product_owner": {"name": "", "signed": False, "date": ""},
                "compliance_officer": {
                    "name": "", "signed": False, "date": "",
                },
            },
        }
