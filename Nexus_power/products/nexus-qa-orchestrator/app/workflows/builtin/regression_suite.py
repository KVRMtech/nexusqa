"""
Built-in Chain: Regression Test Suite.

Product: Nexus Regression

Takes existing business rules from the knowledge graph and
re-generates + re-executes tests.  Perfect for nightly regression
or post-deployment validation.

DAG:
    fetch_rules ─→ test_generation ─→ test_data_generation ─┐
                                                             ├─→ test_execution ─→ report_generation ─→ notification
"""

from ..schema import (
    ChainDefinition,
    PollingConfig,
    RetryPolicy,
    StageDefinition,
)


def build_regression_suite_chain() -> ChainDefinition:
    return ChainDefinition(
        chain_id="nexus.regression-suite",
        name="Regression Test Suite",
        description=(
            "Re-run tests from existing knowledge: fetch rules from graph → "
            "generate fresh tests → create synthetic data → execute against SUT → "
            "generate report → notify team"
        ),
        version="1.0.0",
        tags=["regression", "nightly", "testing"],
        stages=[
            # ── Stage 1: Fetch existing rules from Backbone ──
            StageDefinition(
                stage_id="fetch_rules",
                name="Fetch Existing Rules",
                description="Retrieve all stored business rules from the knowledge graph via Backbone engine",
                engine="backbone",
                endpoint="/api/v1/backbone/search",
                method="POST",
                input_mapping={
                    "tenant_id": "$workflow.tenant_id",
                    "query": "$workflow.input.search_query",
                    "node_types": ["BusinessRule"],
                    "limit": 100,
                    "min_similarity": 0.0,
                },
                output_transform="[r['properties'] for r in result.get('results', [])]",
                timeout_seconds=30,
                retry_policy=RetryPolicy(max_retries=2),
                on_failure="fail",
            ),
            # ── Stage 2: Generate test cases from rules ──────
            StageDefinition(
                stage_id="test_generation",
                name="Test Case Generation",
                description="Generate test cases from fetched business rules via Heart engine",
                engine="heart",
                endpoint="/api/v1/heart/generate-tests",
                input_mapping={
                    "tenant_id": "$workflow.tenant_id",
                    "rules": "$stages.fetch_rules.output",
                    "coverage_targets": [
                        "happy_path",
                        "boundary",
                        "negative",
                        "edge_case",
                        "regression",
                    ],
                },
                depends_on=["fetch_rules"],
                condition="len($stages.fetch_rules.output) > 0",
                timeout_seconds=300,
                retry_policy=RetryPolicy(max_retries=2),
                on_failure="fail",
            ),
            # ── Stage 3: Generate synthetic test data ────────
            StageDefinition(
                stage_id="test_data_generation",
                name="Synthetic Test Data",
                description="Generate realistic test data for all scenarios via Hands engine",
                engine="hands",
                endpoint="/api/v1/hands/generate-profiles",
                input_mapping={
                    "tenant_id": "$workflow.tenant_id",
                    "count": 200,
                    "include_boundary_values": True,
                },
                depends_on=["test_generation"],
                timeout_seconds=120,
                on_failure="skip",
            ),
            # ── Stage 4: Execute tests ───────────────────────
            StageDefinition(
                stage_id="test_execution",
                name="Test Execution",
                description="Execute generated tests against the system under test via Legs engine",
                engine="legs",
                endpoint="/api/v1/legs/execute",
                input_mapping={
                    "tenant_id": "$workflow.tenant_id",
                    "test_case": "$temp.item",
                    "target_type": "$workflow.input.sut_type",
                    "base_url": "$workflow.input.sut_url",
                    "credentials": "$workflow.input.sut_credentials",
                },
                depends_on=["test_generation", "test_data_generation"],
                condition="$workflow.input.sut_url",
                timeout_seconds=120,
                on_failure="continue",
                for_each="$stages.test_generation.output.test_cases",
                for_each_item_key="item",
                for_each_concurrency=10,
                polling=PollingConfig(
                    enabled=True,
                    job_id_path="job_id",
                    poll_endpoint="/api/v1/legs/jobs/{job_id}",
                    poll_interval_seconds=2.0,
                    max_poll_seconds=300.0,
                    completion_statuses=["passed", "failed"],
                    failure_statuses=["error"],
                    result_path="result",
                    status_path="status",
                ),
            ),
            # ── Stage 5: Generate regression report ──────────
            StageDefinition(
                stage_id="report_generation",
                name="Regression Report",
                description="Generate regression test report with pass/fail summary via Mouth engine",
                engine="mouth",
                endpoint="/api/v1/mouth/generate",
                input_mapping={
                    "tenant_id": "$workflow.tenant_id",
                    "session_id": "$workflow.session_id",
                    "report_type": "test_coverage",
                    "format": "html",
                    "title": "Regression Suite — ${workflow.input.suite_name}",
                    "rules": "$stages.fetch_rules.output",
                    "test_cases": "$stages.test_generation.output.test_cases",
                    "test_results": "$stages.test_execution.output.items",
                },
                depends_on=["test_execution"],
                timeout_seconds=120,
                on_failure="skip",
            ),
            # ── Stage 6: Notify team ─────────────────────────
            StageDefinition(
                stage_id="notification",
                name="Team Notification",
                description="Notify team about regression results via Nerves engine",
                engine="nerves",
                endpoint="/api/v1/nerves/execute",
                input_mapping={
                    "tenant_id": "$workflow.tenant_id",
                    "connector": "slack",
                    "action": "send_message",
                    "parameters": {
                        "channel": "#nexus-qa",
                        "text": "Regression suite '${workflow.input.suite_name}' completed for session ${workflow.session_id}",
                    },
                },
                depends_on=["report_generation"],
                condition="$workflow.input.notify",
                timeout_seconds=30,
                on_failure="skip",
            ),
        ],
    )
