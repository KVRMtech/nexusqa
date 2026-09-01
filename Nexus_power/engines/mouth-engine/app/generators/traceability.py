"""
Traceability Matrix Generator.

Builds the critical Rule → Test Case → Test Result → Evidence mapping
required for regulatory compliance and audit readiness.
"""

from __future__ import annotations

import uuid
from enum import Enum
from collections import defaultdict

from pydantic import BaseModel, Field


# ─── Supporting Types ──────────────────────────────────────────

class CoverageLevel(str, Enum):
    FULL = "full"          # 100 % — all rules have passing tests
    HIGH = "high"          # 80-99 %
    MODERATE = "moderate"  # 60-79 %
    LOW = "low"            # 40-59 %
    CRITICAL = "critical"  # < 40 %


class TraceabilityEntry(BaseModel):
    """One row in the traceability matrix."""
    rule_id: str
    rule_description: str
    rule_source: str = ""
    rule_priority: str = "medium"
    test_case_ids: list[str] = Field(default_factory=list)
    test_case_count: int = 0
    tests_passed: int = 0
    tests_failed: int = 0
    tests_not_run: int = 0
    coverage_status: CoverageLevel = CoverageLevel.CRITICAL
    evidence_ids: list[str] = Field(default_factory=list)
    last_tested: str | None = None
    notes: str = ""


# ─── Generator ─────────────────────────────────────────────────

class TraceabilityMatrixGenerator:
    """
    Generates a traceability matrix linking:
    BusinessRule → TestCase → TestResult → Evidence

    This is THE critical artifact for regulatory compliance.
    Auditors must see: "For every business rule, show me the test that
    validates it, the result of that test, and screenshot evidence."
    """

    def generate(
        self,
        rules: list[dict],
        test_cases: list[dict],
        test_results: list[dict],
        evidence: list[dict],
    ) -> dict:
        """Build the full traceability matrix."""

        # Index test cases by rule
        tc_by_rule: dict[str, list[dict]] = defaultdict(list)
        for tc in test_cases:
            for rule_id in tc.get("rule_ids", [tc.get("rule_id", "unknown")]):
                tc_by_rule[rule_id].append(tc)

        # Index results by test case
        results_by_tc: dict[str, dict] = {}
        for result in test_results:
            tc_id = result.get("test_case_id", "")
            results_by_tc[tc_id] = result

        # Index evidence by test case
        evidence_by_tc: dict[str, list[dict]] = defaultdict(list)
        for ev in evidence:
            tc_id = ev.get("test_case_id", "")
            evidence_by_tc[tc_id].append(ev)

        # Build matrix rows
        entries: list[dict] = []
        total_rules = len(rules)
        covered_rules = 0
        fully_passed_rules = 0

        for rule in rules:
            rule_id = rule.get("rule_id", str(uuid.uuid4())[:8])
            tcs = tc_by_rule.get(rule_id, [])

            passed = 0
            failed = 0
            not_run = 0
            ev_ids: list[str] = []

            for tc in tcs:
                tc_id = tc.get("test_case_id", "")
                result = results_by_tc.get(tc_id)
                if result:
                    status = result.get("status", "not_run")
                    if status == "passed":
                        passed += 1
                    elif status in ("failed", "error"):
                        failed += 1
                    else:
                        not_run += 1
                else:
                    not_run += 1

                # Collect evidence
                for ev in evidence_by_tc.get(tc_id, []):
                    ev_ids.append(ev.get("evidence_id", ""))

            total_tests = passed + failed + not_run
            if total_tests == 0:
                coverage = CoverageLevel.CRITICAL
            elif passed == total_tests:
                coverage = CoverageLevel.FULL
                fully_passed_rules += 1
                covered_rules += 1
            elif (passed / total_tests) >= 0.8:
                coverage = CoverageLevel.HIGH
                covered_rules += 1
            elif (passed / total_tests) >= 0.6:
                coverage = CoverageLevel.MODERATE
                covered_rules += 1
            elif (passed / total_tests) >= 0.4:
                coverage = CoverageLevel.LOW
            else:
                coverage = CoverageLevel.CRITICAL

            entry = TraceabilityEntry(
                rule_id=rule_id,
                rule_description=rule.get(
                    "description", rule.get("text", "No description"),
                ),
                rule_source=rule.get("source", "KT Session"),
                rule_priority=rule.get("priority", "medium"),
                test_case_ids=[tc.get("test_case_id", "") for tc in tcs],
                test_case_count=len(tcs),
                tests_passed=passed,
                tests_failed=failed,
                tests_not_run=not_run,
                coverage_status=coverage,
                evidence_ids=ev_ids,
                last_tested=max(
                    (
                        r.get("completed_at", "")
                        for r in [
                            results_by_tc.get(tc.get("test_case_id", ""), {})
                            for tc in tcs
                        ]
                        if r
                    ),
                    default=None,
                ),
            )
            entries.append(entry.model_dump())

        # Summary statistics
        coverage_pct = (covered_rules / max(total_rules, 1)) * 100
        pass_pct = (fully_passed_rules / max(total_rules, 1)) * 100

        return {
            "matrix": entries,
            "summary": {
                "total_rules": total_rules,
                "rules_with_tests": covered_rules,
                "rules_all_passing": fully_passed_rules,
                "rules_with_failures": sum(
                    1 for e in entries if e["tests_failed"] > 0
                ),
                "rules_no_tests": sum(
                    1 for e in entries if e["test_case_count"] == 0
                ),
                "overall_coverage_pct": round(coverage_pct, 1),
                "overall_pass_pct": round(pass_pct, 1),
                "total_test_cases": len(test_cases),
                "total_evidence_items": len(evidence),
            },
        }
