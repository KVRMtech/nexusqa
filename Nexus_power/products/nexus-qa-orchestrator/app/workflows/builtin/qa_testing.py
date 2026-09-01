"""
Built-in Chain: Full QA Testing Pipeline.

Product: Nexus QA

The flagship chain — orchestrates all 11 engines for complete
insurance product QA testing from a single KT session.

Requires a completed canonical artifact (produced by the
canonical-processing chain).  Fetches the artifact from Spine
instead of re-processing audio/video.

DAG:
    fetch_artifact ─────────────────────┐
    document_ingestion ─────────────────┤
                                        ├─→ rule_extraction → test_generation ─┐
                                        │                                      ├─→ test_execution → report_generation → notification
                                        │        test_data_generation ─────────┘
                                        └─→ knowledge_storage ────────────────────────────────────┘
"""

from ..schema import (
    ChainDefinition,
    PollingConfig,
    RetryPolicy,
    StageDefinition,
)


def build_qa_testing_chain() -> ChainDefinition:
    return ChainDefinition(
        chain_id="nexus.qa-testing",
        name="Full QA Testing Pipeline",
        description=(
            "End-to-end QA pipeline: fetch canonical artifact → "
            "ingest documents → extract rules → generate tests → "
            "create synthetic data → store knowledge → execute tests → "
            "generate reports → notify team.  "
            "Requires a prior canonical-processing run for the same session."
        ),
        version="2.0.0",
        tags=["qa", "insurance", "full-pipeline"],
        stages=[
            # ── Stage 1: Fetch canonical artifact ──────────────
            StageDefinition(
                stage_id="fetch_artifact",
                name="Fetch Canonical Artifact",
                description=(
                    "Retrieve the pre-computed canonical artifact "
                    "(safe transcript, visual analysis, quality score) "
                    "from Spine engine instead of re-processing media."
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
            # ── Stage 2: Document Ingestion ───────────────────
            StageDefinition(
                stage_id="document_ingestion",
                name="Document Ingestion",
                description="Ingest policy documents, rate tables, guides via Spine engine",
                engine="spine",
                endpoint="/api/v1/spine/ingest",
                request_type="multipart",
                input_mapping={
                    "tenant_id": "$workflow.tenant_id",
                    "session_id": "$workflow.session_id",
                },
                file_mappings={
                    "file": "$temp.item",
                },
                condition="$workflow.input.document_file_ids",
                timeout_seconds=300,
                on_failure="skip",
                for_each="$workflow.input.document_file_ids",
                for_each_item_key="item",
                for_each_concurrency=3,
            ),
            # ── Stage 3: Extract Business Rules ───────────────
            StageDefinition(
                stage_id="rule_extraction",
                name="Business Rule Extraction",
                description=(
                    "Extract business rules from the canonical artifact's "
                    "safe transcript and visual context via Heart engine"
                ),
                engine="heart",
                endpoint="/api/v1/heart/extract-rules",
                input_mapping={
                    "tenant_id": "$workflow.tenant_id",
                    "session_id": "$workflow.session_id",
                    "transcript": "$stages.fetch_artifact.output.artifact.safe_transcript_text",
                    "visual_context": "$stages.fetch_artifact.output.artifact.full_artifact_json.visual_analysis",
                },
                depends_on=["fetch_artifact", "document_ingestion"],
                timeout_seconds=300,
                retry_policy=RetryPolicy(max_retries=2),
                on_failure="fail",
            ),
            # ── Stage 4: Generate Test Cases ──────────────────
            StageDefinition(
                stage_id="test_generation",
                name="Test Case Generation",
                description="Generate comprehensive test cases from extracted business rules via Heart engine",
                engine="heart",
                endpoint="/api/v1/heart/generate-tests",
                input_mapping={
                    "tenant_id": "$workflow.tenant_id",
                    "rules": "$stages.rule_extraction.output.rules",
                    "coverage_targets": [
                        "happy_path",
                        "boundary",
                        "negative",
                        "edge_case",
                    ],
                },
                depends_on=["rule_extraction"],
                timeout_seconds=300,
                retry_policy=RetryPolicy(max_retries=2),
                on_failure="fail",
            ),
            # ── Stage 5: Generate Synthetic Test Data ─────────
            StageDefinition(
                stage_id="test_data_generation",
                name="Synthetic Test Data Generation",
                description="Generate realistic test data profiles for all test scenarios via Hands engine",
                engine="hands",
                endpoint="/api/v1/hands/generate-profiles",
                input_mapping={
                    "tenant_id": "$workflow.tenant_id",
                    "count": 100,
                    "include_boundary_values": True,
                },
                depends_on=["test_generation"],
                timeout_seconds=120,
                on_failure="skip",
            ),
            # ── Stage 6: Store in Knowledge Graph ─────────────
            StageDefinition(
                stage_id="knowledge_storage",
                name="Knowledge Graph Storage",
                description="Store extracted rules in the knowledge graph with dedup via Backbone engine",
                engine="backbone",
                endpoint="/api/v1/backbone/rules",
                input_mapping={
                    "tenant_id": "$workflow.tenant_id",
                    "rule": "$temp.item",
                },
                depends_on=["rule_extraction"],
                timeout_seconds=30,
                on_failure="continue",
                for_each="$stages.rule_extraction.output.rules",
                for_each_item_key="item",
                for_each_concurrency=5,
            ),
            # ── Stage 7: Execute Tests ────────────────────────
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
                for_each_concurrency=5,
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
            # ── Stage 8: Generate Reports ────────────────────
            StageDefinition(
                stage_id="report_generation",
                name="Report Generation",
                description="Generate traceability matrix, compliance, and executive reports via Mouth engine",
                engine="mouth",
                endpoint="/api/v1/mouth/generate",
                input_mapping={
                    "tenant_id": "$workflow.tenant_id",
                    "session_id": "$workflow.session_id",
                    "report_type": "full_session_report",
                    "format": "html",
                    "title": "$workflow.input.session_name",
                    "rules": "$stages.rule_extraction.output.rules",
                    "test_cases": "$stages.test_generation.output.test_cases",
                    "test_results": "$stages.test_execution.output.items",
                },
                depends_on=["test_execution", "knowledge_storage"],
                timeout_seconds=120,
                on_failure="skip",
            ),
            # ── Stage 9: Team Notification ───────────────────
            StageDefinition(
                stage_id="notification",
                name="Team Notification",
                description="Notify the team via Slack/Jira/Teams about pipeline results via Nerves engine",
                engine="nerves",
                endpoint="/api/v1/nerves/execute",
                input_mapping={
                    "tenant_id": "$workflow.tenant_id",
                    "connector": "slack",
                    "action": "send_message",
                    "parameters": {
                        "channel": "#nexus-qa",
                        "text": "Nexus QA pipeline completed for session ${workflow.session_id}",
                    },
                },
                depends_on=["report_generation"],
                condition="$workflow.input.notify",
                timeout_seconds=30,
                on_failure="skip",
            ),
        ],
    )
