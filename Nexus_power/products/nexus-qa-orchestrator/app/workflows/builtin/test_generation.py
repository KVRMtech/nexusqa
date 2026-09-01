"""
Built-in Consumer Chain: Test Generation.

Phase 2.3 — Generates comprehensive test cases from extracted business
rules and persists them to the database.

Triggered automatically after rule-extraction completes.
Receives prior_consumer_outputs from rule-extraction chain.

DAG:
    fetch_artifact → resolve_rules → generate_tests → persist_tests
"""

from ..schema import (
    ChainDefinition,
    RetryPolicy,
    StageDefinition,
)


def build_test_generation_chain() -> ChainDefinition:
    """Build the test-generation consumer chain."""
    return ChainDefinition(
        chain_id="nexus.test-generation",
        name="Test Generation",
        description=(
            "Generate test cases from business rules: fetch artifact → "
            "resolve rules from prior rule-extraction or re-extract → "
            "generate tests via Heart → persist to database. "
            "Auto-triggered after canonical-processing completes."
        ),
        version="1.0.0",
        tags=["consumer", "tests", "generation", "heart"],
        stages=[
            # ── Stage 1: Fetch canonical artifact ─────────────
            StageDefinition(
                stage_id="fetch_artifact",
                name="Fetch Canonical Artifact",
                description=(
                    "Retrieve the canonical artifact from Spine. Provides "
                    "visual context for test generation and serves as a "
                    "fallback transcript if rules were not pre-extracted."
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

            # ── Stage 2: Extract rules (if not provided by prior chain) ──
            StageDefinition(
                stage_id="extract_rules",
                name="Rule Extraction (fallback)",
                description=(
                    "If the rule-extraction consumer chain did not run or "
                    "failed, re-extract rules from the canonical transcript. "
                    "Skipped when prior_consumer_outputs already contains rules."
                ),
                engine="heart",
                endpoint="/api/v1/heart/extract-rules",
                input_mapping={
                    "tenant_id": "$workflow.tenant_id",
                    "session_id": "$workflow.session_id",
                    "transcript": "$stages.fetch_artifact.output.artifact.safe_transcript_text",
                    "visual_context": "$stages.fetch_artifact.output.artifact.full_artifact_json.visual_analysis",
                },
                depends_on=["fetch_artifact"],
                # Skip if rules already available from prior consumer chain
                # Alias: "nexus.rule-extraction" → "rule_extraction" (dots break path resolution)
                condition="not $workflow.input.prior_consumer_outputs.rule_extraction.extract_rules.rules",
                timeout_seconds=300,
                retry_policy=RetryPolicy(max_retries=2, backoff_seconds=5.0),
                on_failure="fail",
            ),

            # ── Stage 3: Generate test cases via Heart ────────
            StageDefinition(
                stage_id="generate_tests",
                name="Test Case Generation",
                description=(
                    "Heart engine generates comprehensive test cases covering "
                    "happy path, boundary values, negative scenarios, and edge "
                    "cases from the extracted business rules."
                ),
                engine="heart",
                endpoint="/api/v1/heart/generate-tests",
                input_mapping={
                    "tenant_id": "$workflow.tenant_id",
                    # Pipe-fallback: local extract_rules output first, then
                    # prior consumer chain rules when extract_rules was skipped.
                    "rules": "$stages.extract_rules.output.rules|$workflow.input.prior_consumer_outputs.rule_extraction.extract_rules.rules",
                    "coverage_targets": [
                        "happy_path",
                        "boundary",
                        "negative",
                        "edge_case",
                    ],
                },
                depends_on=["extract_rules"],
                # Skip entirely when neither source produced rules
                condition="$stages.extract_rules.output.rules or $workflow.input.prior_consumer_outputs.rule_extraction.extract_rules.rules",
                timeout_seconds=300,
                retry_policy=RetryPolicy(max_retries=2, backoff_seconds=5.0),
                on_failure="fail",
            ),

            # ── Stage 4: Persist test cases to Spine ──────────
            StageDefinition(
                stage_id="persist_tests",
                name="Test Case Persistence",
                description=(
                    "Persist generated test cases to the database via Spine "
                    "engine. Each test case is stored as a TestCaseRow with "
                    "steps, prerequisites, and traceability to source rules."
                ),
                engine="spine",
                endpoint="/api/v1/spine/persist-test-cases",
                input_mapping={
                    "tenant_id": "$workflow.tenant_id",
                    "session_id": "$workflow.session_id",
                    "canonical_artifact_id": "$workflow.input.canonical_artifact_id",
                    "test_cases": "$stages.generate_tests.output.test_cases",
                },
                depends_on=["generate_tests"],
                condition="$stages.generate_tests.output.test_cases",
                timeout_seconds=60,
                retry_policy=RetryPolicy(max_retries=2, backoff_seconds=3.0),
                on_failure="fail",
            ),
        ],
    )
