"""
Built-in Chain: Knowledge Capture.

Product: Nexus Knowledge

Captures institutional knowledge from KT sessions WITHOUT
generating or executing tests.  Useful for onboarding,
documentation, and building the knowledge base incrementally.

Requires a completed canonical artifact (produced by the
canonical-processing chain).  Fetches the artifact from Spine
instead of re-processing audio/video.

DAG:
    fetch_artifact ──→ rule_extraction ──→ knowledge_storage
"""

from ..schema import (
    ChainDefinition,
    RetryPolicy,
    StageDefinition,
)


def build_knowledge_capture_chain() -> ChainDefinition:
    return ChainDefinition(
        chain_id="nexus.knowledge-capture",
        name="Knowledge Capture Pipeline",
        description=(
            "Capture institutional knowledge: fetch canonical artifact → "
            "extract rules → store in knowledge graph.  "
            "No test generation or execution.  "
            "Requires a prior canonical-processing run for the same session."
        ),
        version="2.0.0",
        tags=["knowledge", "capture", "onboarding"],
        stages=[
            # ── Stage 1: Fetch canonical artifact ─────────────
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
            # ── Stage 2: Extract business rules ──────────────
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
                depends_on=["fetch_artifact"],
                timeout_seconds=300,
                retry_policy=RetryPolicy(max_retries=2),
                on_failure="fail",
            ),
            # ── Stage 3: Store in knowledge graph ────────────
            StageDefinition(
                stage_id="knowledge_storage",
                name="Knowledge Graph Storage",
                description="Persist extracted rules in the knowledge graph with dedup via Backbone engine",
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
        ],
    )
