"""
Built-in Consumer Chain: Report Generation.

Phase 2.6 — Generates comprehensive QA reports after all upstream
consumer chains have completed.

Triggered last in the consumer chain sequence.  Uses accumulated
outputs from rule-extraction, test-generation, and contradiction-detection
to produce a full session report, traceability matrix, and compliance summary.

DAG:
    fetch_artifact → generate_session_report
                   → generate_traceability_matrix
                   → generate_compliance_summary
"""

from ..schema import (
    ChainDefinition,
    RetryPolicy,
    StageDefinition,
)


def build_report_generation_chain() -> ChainDefinition:
    """Build the report-generation consumer chain."""
    return ChainDefinition(
        chain_id="nexus.report-generation",
        name="Report Generation",
        description=(
            "Generate QA reports: fetch artifact → generate full session "
            "report + traceability matrix + compliance summary via Mouth engine. "
            "Auto-triggered last after all other consumer chains complete."
        ),
        version="1.0.0",
        tags=["consumer", "report", "mouth", "traceability", "compliance"],
        stages=[
            # ── Stage 1: Fetch canonical artifact ─────────────
            StageDefinition(
                stage_id="fetch_artifact",
                name="Fetch Canonical Artifact",
                description=(
                    "Retrieve the canonical artifact for report metadata: "
                    "session info, quality score, processing timeline."
                ),
                engine="spine",
                endpoint="/api/v1/spine/artifacts/{session_id}",
                method="GET",
                input_mapping={
                    "session_id": "$workflow.session_id",
                },
                timeout_seconds=30,
                retry_policy=RetryPolicy(max_retries=2, backoff_seconds=2.0),
                on_failure="fail",
            ),

            # ── Stage 2: Full Session Report ──────────────────
            StageDefinition(
                stage_id="generate_session_report",
                name="Full Session Report",
                description=(
                    "Generate a comprehensive HTML report summarising the "
                    "entire KT session: transcript, rules extracted, test "
                    "cases generated, contradictions found, and quality score."
                ),
                engine="mouth",
                endpoint="/api/v1/mouth/generate",
                input_mapping={
                    "tenant_id": "$workflow.tenant_id",
                    "session_id": "$workflow.session_id",
                    "report_type": "full_session_report",
                    "format": "html",
                    "title": "KT Session Report",
                    "rules": "$workflow.input.prior_consumer_outputs.rule_extraction.extract_rules.rules",
                    "test_cases": "$workflow.input.prior_consumer_outputs.test_generation.generate_tests.test_cases",
                    "evidence": [],
                    "test_results": [],
                },
                depends_on=["fetch_artifact"],
                timeout_seconds=120,
                retry_policy=RetryPolicy(max_retries=2, backoff_seconds=5.0),
                on_failure="continue",
            ),

            # ── Stage 3: Traceability Matrix ──────────────────
            StageDefinition(
                stage_id="generate_traceability_matrix",
                name="Traceability Matrix",
                description=(
                    "Generate a traceability matrix mapping each business "
                    "rule to the test cases that validate it, showing "
                    "coverage percentage and gaps."
                ),
                engine="mouth",
                endpoint="/api/v1/mouth/generate",
                input_mapping={
                    "tenant_id": "$workflow.tenant_id",
                    "session_id": "$workflow.session_id",
                    "report_type": "traceability_matrix",
                    "format": "html",
                    "title": "Rule-to-Test Traceability Matrix",
                    "rules": "$workflow.input.prior_consumer_outputs.rule_extraction.extract_rules.rules",
                    "test_cases": "$workflow.input.prior_consumer_outputs.test_generation.generate_tests.test_cases",
                    "evidence": [],
                    "test_results": [],
                },
                depends_on=["fetch_artifact"],
                timeout_seconds=120,
                retry_policy=RetryPolicy(max_retries=2, backoff_seconds=5.0),
                on_failure="continue",
            ),

            # ── Stage 4: Compliance Summary ───────────────────
            StageDefinition(
                stage_id="generate_compliance_summary",
                name="Compliance Summary",
                description=(
                    "Generate a compliance summary highlighting contradictions, "
                    "untested rules, and regulatory risk areas. Uses contradiction "
                    "data from the contradiction-detection consumer chain."
                ),
                engine="mouth",
                endpoint="/api/v1/mouth/generate",
                input_mapping={
                    "tenant_id": "$workflow.tenant_id",
                    "session_id": "$workflow.session_id",
                    "report_type": "compliance_report",
                    "format": "html",
                    "title": "Compliance & Contradiction Summary",
                    "rules": "$workflow.input.prior_consumer_outputs.rule_extraction.extract_rules.rules",
                    "test_cases": "$workflow.input.prior_consumer_outputs.test_generation.generate_tests.test_cases",
                    "evidence": [],
                    "test_results": [],
                },
                depends_on=["fetch_artifact"],
                timeout_seconds=120,
                retry_policy=RetryPolicy(max_retries=2, backoff_seconds=5.0),
                on_failure="continue",
            ),
        ],
    )
