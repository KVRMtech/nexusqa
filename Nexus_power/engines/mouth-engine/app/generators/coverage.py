"""
Test Coverage Report Generator.

Analyses per-rule coverage by test type (happy-path, boundary,
negative, edge-case, regression), computes confidence scores,
identifies gaps, and produces actionable recommendations.
"""

from __future__ import annotations


class TestCoverageReportGenerator:
    """
    Generates detailed test coverage analysis.

    For each rule, shows:
    - How many test scenarios cover it
    - Which coverage types exist (happy path, boundary, negative, edge case)
    - Confidence level of the coverage
    - Gaps where additional tests are recommended
    """

    COVERAGE_TYPES: list[str] = [
        "happy_path",
        "boundary",
        "negative",
        "edge_case",
        "regression",
    ]

    def generate(
        self,
        rules: list[dict],
        test_cases: list[dict],
        test_results: list[dict],
    ) -> dict:
        """Generate coverage analysis."""

        coverage_details: list[dict] = []

        for rule in rules:
            rule_id = rule.get("rule_id", "")
            related_tcs = [
                tc for tc in test_cases
                if rule_id in tc.get("rule_ids", [tc.get("rule_id", "")])
            ]

            # Categorize by coverage type
            type_coverage: dict[str, int] = {
                ct: 0 for ct in self.COVERAGE_TYPES
            }
            for tc in related_tcs:
                tc_type = tc.get(
                    "test_type", tc.get("coverage_type", "happy_path"),
                )
                if tc_type in type_coverage:
                    type_coverage[tc_type] += 1
                else:
                    type_coverage["happy_path"] += 1

            # Calculate result stats
            tc_ids = {tc.get("test_case_id") for tc in related_tcs}
            related_results = [
                r for r in test_results
                if r.get("test_case_id") in tc_ids
            ]
            passed = sum(
                1 for r in related_results if r.get("status") == "passed"
            )
            total = len(related_results)

            # Determine gaps
            gaps: list[str] = []
            if type_coverage["happy_path"] == 0:
                gaps.append("Missing happy path test")
            if type_coverage["boundary"] == 0:
                gaps.append("Missing boundary value tests")
            if type_coverage["negative"] == 0:
                gaps.append("Missing negative/invalid input tests")
            if type_coverage["edge_case"] == 0:
                gaps.append("Missing edge case tests")

            # Confidence calculation
            types_covered = sum(1 for v in type_coverage.values() if v > 0)
            type_confidence = types_covered / len(self.COVERAGE_TYPES)
            result_confidence = passed / max(total, 1)
            overall_confidence = round(
                (type_confidence * 0.4 + result_confidence * 0.6) * 100, 1,
            )

            coverage_details.append({
                "rule_id": rule_id,
                "rule_description": rule.get(
                    "description", rule.get("text", ""),
                ),
                "total_test_cases": len(related_tcs),
                "coverage_by_type": type_coverage,
                "types_covered": types_covered,
                "types_total": len(self.COVERAGE_TYPES),
                "tests_passed": passed,
                "tests_total": total,
                "gaps": gaps,
                "confidence_pct": overall_confidence,
                "recommendations": self._recommend(gaps, overall_confidence),
            })

        # Aggregate stats
        total_rules = len(rules)
        rules_with_full_coverage = sum(
            1 for d in coverage_details
            if d["types_covered"] >= 4 and d["confidence_pct"] >= 80
        )
        avg_confidence = round(
            sum(d["confidence_pct"] for d in coverage_details)
            / max(len(coverage_details), 1),
            1,
        )

        return {
            "coverage_details": coverage_details,
            "summary": {
                "total_rules": total_rules,
                "rules_with_full_coverage": rules_with_full_coverage,
                "rules_needing_attention": total_rules - rules_with_full_coverage,
                "average_confidence_pct": avg_confidence,
                "total_test_cases": len(test_cases),
                "total_gaps_found": sum(
                    len(d["gaps"]) for d in coverage_details
                ),
            },
        }

    # ── Private Helpers ────────────────────────────────────────

    def _recommend(self, gaps: list[str], confidence: float) -> list[str]:
        """Generate recommendations based on gaps."""
        recommendations: list[str] = []
        if confidence < 50:
            recommendations.append(
                "CRITICAL: This rule needs immediate test coverage improvement",
            )
        if "Missing happy path test" in gaps:
            recommendations.append(
                "Add a basic positive/happy path test scenario",
            )
        if "Missing boundary value tests" in gaps:
            recommendations.append(
                "Add boundary value analysis for numeric fields "
                "(min, max, min-1, max+1)",
            )
        if "Missing negative/invalid input tests" in gaps:
            recommendations.append(
                "Add negative tests: empty fields, invalid formats, "
                "unauthorized access",
            )
        if "Missing edge case tests" in gaps:
            recommendations.append(
                "Add edge cases: concurrent modifications, timezone "
                "boundaries, leap years",
            )
        return recommendations
