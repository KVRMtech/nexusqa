"""
Defect Summary Generator.

Groups test failures by category and severity, classifies root
causes based on error patterns, and helps QA leads prioritise
remediation work.
"""

from __future__ import annotations

from collections import defaultdict


class DefectSummaryGenerator:
    """
    Generates defect/failure summary report.

    Groups failures by category, severity, and root cause pattern.
    Helps QA leads prioritize remediation work.
    """

    def generate(
        self,
        test_cases: list[dict],
        test_results: list[dict],
    ) -> dict:
        """Generate defect summary."""

        failures: list[dict] = []
        by_category: dict[str, list[dict]] = defaultdict(list)
        by_severity: dict[str, int] = defaultdict(int)

        # Build test case index
        tc_index = {tc.get("test_case_id"): tc for tc in test_cases}

        for result in test_results:
            if result.get("status") not in ("failed", "error"):
                continue

            tc_id = result.get("test_case_id", "")
            tc = tc_index.get(tc_id, {})

            failure = {
                "test_case_id": tc_id,
                "test_name": tc.get(
                    "name", tc.get("description", "Unknown test"),
                ),
                "category": tc.get("category", "uncategorized"),
                "severity": self._classify_severity(result, tc),
                "error_type": result.get("error_type", "assertion_failure"),
                "error_message": result.get(
                    "error", result.get("message", "No details"),
                ),
                "step_failed": result.get("step_failed", ""),
                "expected": result.get("expected", ""),
                "actual": result.get("actual", ""),
                "screenshot_ref": result.get("screenshot_id", ""),
                "suggested_root_cause": self._suggest_root_cause(result),
            }

            failures.append(failure)
            by_category[failure["category"]].append(failure)
            by_severity[failure["severity"]] += 1

        # Sort by severity
        severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        failures.sort(key=lambda f: severity_order.get(f["severity"], 99))

        return {
            "defects": failures,
            "by_category": {
                cat: {
                    "count": len(items),
                    "critical": sum(
                        1 for i in items if i["severity"] == "critical"
                    ),
                    "high": sum(
                        1 for i in items if i["severity"] == "high"
                    ),
                }
                for cat, items in by_category.items()
            },
            "by_severity": dict(by_severity),
            "summary": {
                "total_failures": len(failures),
                "critical": by_severity.get("critical", 0),
                "high": by_severity.get("high", 0),
                "medium": by_severity.get("medium", 0),
                "low": by_severity.get("low", 0),
                "top_failing_category": (
                    max(by_category, key=lambda k: len(by_category[k]))
                    if by_category
                    else "none"
                ),
            },
        }

    # ── Private Helpers ────────────────────────────────────────

    def _classify_severity(self, result: dict, tc: dict) -> str:
        """Classify defect severity based on context."""
        error_type = result.get("error_type", "")
        tc_priority = tc.get("priority", "medium")

        if "security" in error_type.lower() or "pii" in error_type.lower():
            return "critical"
        if tc_priority == "critical":
            return "critical"
        if tc_priority == "high" or "data_loss" in error_type.lower():
            return "high"
        if "ui" in error_type.lower() or "display" in error_type.lower():
            return "low"
        return "medium"

    def _suggest_root_cause(self, result: dict) -> str:
        """Suggest root cause based on error patterns."""
        error = (
            result.get("error", "") + result.get("message", "")
        ).lower()

        if "timeout" in error:
            return "Performance issue — operation exceeded timeout threshold"
        if "not found" in error or "404" in error:
            return "Missing element/endpoint — UI or API may have changed"
        if "permission" in error or "401" in error or "403" in error:
            return "Authorization/permission issue — check role assignments"
        if "null" in error or "none" in error or "undefined" in error:
            return "Null reference — missing data or uninitialized field"
        if "mismatch" in error or "expected" in error:
            return "Assertion failure — actual behavior differs from expected"
        if "connection" in error:
            return "Connectivity issue — service may be down or unreachable"
        return "Review error details and test steps for root cause"
