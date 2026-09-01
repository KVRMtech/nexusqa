"""
Compliance Report Generator.

Maps business rules to insurance-regulatory domains and assesses
compliance status per jurisdiction.  Provides gap analysis and
remediation suggestions.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field


# ─── Supporting Types ──────────────────────────────────────────

class ComplianceItem(BaseModel):
    """One compliance checkpoint."""
    requirement_id: str
    requirement_description: str
    jurisdiction: str = "ALL"
    status: str = "not_assessed"   # compliant | non_compliant | partial | not_assessed
    evidence_refs: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    remediation: str = ""


# ─── Generator ─────────────────────────────────────────────────

class ComplianceReportGenerator:
    """
    Generates compliance reports for insurance regulatory requirements.

    Maps business rules to regulatory requirements and checks:
    - Is every regulatory requirement covered by at least one rule?
    - Is every rule tested?
    - Are there jurisdiction-specific gaps?
    """

    # Common insurance regulatory domains
    REGULATORY_DOMAINS: dict[str, str] = {
        "rate_filing": "Premium rates filed and approved by state DOI",
        "form_filing": "Policy forms filed and approved",
        "illustration_compliance": "Illustrations meet state requirements",
        "replacement_rules": "Replacement/exchange regulations followed",
        "suitability": "Product suitability requirements met",
        "disclosure": "Required disclosures provided to applicant",
        "free_look": "Free look period properly implemented",
        "grace_period": "Grace period rules correctly applied",
        "nonforfeiture": "Nonforfeiture options properly calculated",
        "beneficiary_rules": "Beneficiary designation rules followed",
        "underwriting": "Underwriting guidelines consistently applied",
        "claims_processing": "Claims handled within statutory timeframes",
        "producer_licensing": "Producer properly licensed in state",
        "anti_rebating": "Anti-rebating rules followed",
        "privacy": "Privacy and data protection rules met",
    }

    def generate(
        self,
        rules: list[dict],
        test_cases: list[dict],
        test_results: list[dict],
        state_filter: Optional[str] = None,
    ) -> dict:
        """Generate compliance report."""

        items: list[dict] = []
        compliant_count = 0
        gap_count = 0

        for domain_id, domain_desc in self.REGULATORY_DOMAINS.items():
            # Find rules related to this domain
            related_rules = [
                r for r in rules
                if domain_id in r.get("category", "").lower()
                or domain_id.replace("_", " ") in r.get("description", "").lower()
                or domain_id.replace("_", " ") in r.get("text", "").lower()
            ]

            # Find tests for those rules
            rule_ids = {r.get("rule_id", "") for r in related_rules}
            related_tests = [
                tc for tc in test_cases
                if any(
                    rid in tc.get("rule_ids", [tc.get("rule_id", "")])
                    for rid in rule_ids
                )
            ]

            # Check test results
            related_results = [
                tr for tr in test_results
                if tr.get("test_case_id") in {
                    tc.get("test_case_id") for tc in related_tests
                }
            ]

            # Determine compliance status
            if not related_rules:
                status = "not_assessed"
                gaps = [f"No business rules mapped to '{domain_desc}'"]
            elif not related_tests:
                status = "non_compliant"
                gaps = [
                    f"{len(related_rules)} rules found but no test cases generated",
                ]
                gap_count += 1
            elif (
                all(r.get("status") == "passed" for r in related_results)
                and related_results
            ):
                status = "compliant"
                gaps = []
                compliant_count += 1
            elif any(r.get("status") == "passed" for r in related_results):
                status = "partial"
                failed = [
                    r for r in related_results if r.get("status") != "passed"
                ]
                gaps = [f"{len(failed)} of {len(related_results)} tests failing"]
                gap_count += 1
            else:
                status = "non_compliant"
                gaps = ["All related tests failing or not executed"]
                gap_count += 1

            item = ComplianceItem(
                requirement_id=domain_id,
                requirement_description=domain_desc,
                jurisdiction=state_filter or "ALL",
                status=status,
                evidence_refs=[
                    r.get("test_case_id", "")
                    for r in related_results
                    if r.get("status") == "passed"
                ],
                gaps=gaps,
                remediation=self._suggest_remediation(domain_id, status, gaps),
            )
            items.append(item.model_dump())

        total = len(items)
        assessed = sum(1 for i in items if i["status"] != "not_assessed")

        return {
            "compliance_items": items,
            "summary": {
                "total_requirements": total,
                "assessed": assessed,
                "compliant": compliant_count,
                "non_compliant": gap_count,
                "not_assessed": total - assessed,
                "compliance_rate_pct": round(
                    (compliant_count / max(assessed, 1)) * 100, 1,
                ),
                "jurisdiction": state_filter or "ALL",
                "assessment_date": datetime.now(timezone.utc).isoformat(),
            },
        }

    # ── Private Helpers ────────────────────────────────────────

    def _suggest_remediation(
        self, domain_id: str, status: str, gaps: list[str],
    ) -> str:
        """Generate remediation suggestions based on compliance gaps."""
        if status == "compliant":
            return "No action needed. All requirements verified."
        elif status == "not_assessed":
            return (
                f"Map business rules to regulatory domain '{domain_id}'. "
                f"Review KT session transcripts for coverage of this area."
            )
        elif status == "non_compliant":
            return (
                f"URGENT: Generate test cases for rules in '{domain_id}' domain. "
                f"Review and retest. Gaps: {'; '.join(gaps)}"
            )
        else:
            return f"Partial compliance. Fix failing tests: {'; '.join(gaps)}"
