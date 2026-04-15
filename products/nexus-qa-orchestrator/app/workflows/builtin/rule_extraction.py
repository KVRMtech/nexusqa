"""
Built-in Consumer Chain: Rule Extraction.

Phase 2.2 — Extracts structured business rules from a completed
canonical artifact and stores them in the knowledge graph.

Triggered automatically after canonical-processing completes.
Receives canonical_artifact_id in workflow input.

DAG:
    fetch_artifact → extract_rules → store_rules
"""

from ..schema import (
    ChainDefinition,
    RetryPolicy,
    StageDefinition,
)


def build_rule_extraction_chain() -> ChainDefinition:
    """Build the rule-extraction consumer chain."""
    return ChainDefinition(
        chain_id="nexus.rule-extraction",
        name="Rule Extraction",
        description=(
            "Extract business rules from a canonical artifact: fetch artifact "
            "from Spine → extract rules via Heart → store in Backbone knowledge graph. "
            "Auto-triggered after canonical-processing completes."
        ),
        version="1.0.0",
        tags=["consumer", "rules", "extraction", "heart", "backbone"],
        stages=[
            # ── Stage 1: Fetch the canonical artifact ─────────
            StageDefinition(
                stage_id="fetch_artifact",
                name="Fetch Canonical Artifact",
                description=(
                    "Retrieve the canonical artifact (safe transcript + visual "
                    "analysis) from Spine using the session_id provided by the "
                    "canonical-processing completion trigger."
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

            # ── Stage 2: Extract business rules via Heart ─────
            StageDefinition(
                stage_id="extract_rules",
                name="Business Rule Extraction",
                description=(
                    "Heart engine analyses the safe transcript and visual "
                    "context to extract structured business rules with "
                    "conditions, exceptions, categories, and confidence scores."
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
                timeout_seconds=300,
                retry_policy=RetryPolicy(max_retries=2, backoff_seconds=5.0),
                on_failure="fail",
            ),

            # ── Stage 3: Store rules in Backbone knowledge graph ──
            StageDefinition(
                stage_id="store_rules",
                name="Knowledge Graph Storage",
                description=(
                    "Persist each extracted rule as a node in the Backbone "
                    "knowledge graph with semantic deduplication. Duplicate "
                    "rules (similarity >= 0.95) get a CONFIRMED_BY edge; "
                    "related rules (0.80-0.95) get RELATED_TO edges."
                ),
                engine="backbone",
                endpoint="/api/v1/backbone/rules",
                input_mapping={
                    "tenant_id": "$workflow.tenant_id",
                    "session_id": "$workflow.session_id",
                    "rule": "$temp.item",
                },
                depends_on=["extract_rules"],
                condition="$stages.extract_rules.output.rules",
                timeout_seconds=30,
                retry_policy=RetryPolicy(max_retries=2, backoff_seconds=2.0),
                on_failure="continue",
                for_each="$stages.extract_rules.output.rules",
                for_each_item_key="item",
                for_each_concurrency=5,
            ),
        ],
    )
