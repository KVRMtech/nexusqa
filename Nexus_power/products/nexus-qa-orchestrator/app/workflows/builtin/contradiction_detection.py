"""
Built-in Consumer Chain: Contradiction Detection.

Phase 2.5 — Identifies contradictions between business rules extracted
from different KT sessions within the same tenant.

Triggered automatically after canonical-processing completes.
Uses rules from the prior rule-extraction consumer chain and
searches the Backbone knowledge graph for potentially conflicting
rules from other sessions.

DAG:
    fetch_artifact → search_related_rules → detect_contradictions → persist_contradictions
"""

from ..schema import (
    ChainDefinition,
    RetryPolicy,
    StageDefinition,
)


def build_contradiction_detection_chain() -> ChainDefinition:
    """Build the contradiction-detection consumer chain."""
    return ChainDefinition(
        chain_id="nexus.contradiction-detection",
        name="Contradiction Detection",
        description=(
            "Cross-session contradiction analysis: fetch artifact → search "
            "Backbone for related rules from other sessions → Brain LLM "
            "analyses rule pairs for contradictions → persist findings. "
            "Auto-triggered after canonical-processing completes."
        ),
        version="1.0.0",
        tags=["consumer", "contradiction", "cross-session", "brain"],
        stages=[
            # ── Stage 1: Fetch canonical artifact ─────────────
            StageDefinition(
                stage_id="fetch_artifact",
                name="Fetch Canonical Artifact",
                description=(
                    "Retrieve the canonical artifact to access the safe "
                    "transcript and session metadata."
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

            # ── Stage 2: Search for related rules from other sessions ──
            StageDefinition(
                stage_id="search_related_rules",
                name="Search Related Rules",
                description=(
                    "Query the Backbone knowledge graph for business rules "
                    "from OTHER sessions that are semantically similar to "
                    "the current session's transcript. These are candidates "
                    "for contradiction analysis."
                ),
                engine="backbone",
                endpoint="/api/v1/backbone/search",
                method="POST",
                input_mapping={
                    "tenant_id": "$workflow.tenant_id",
                    # Use the transcript as the search query to find semantically similar rules
                    "query": "$stages.fetch_artifact.output.artifact.safe_transcript_text",
                    "node_types": ["BusinessRule"],
                    "limit": 50,
                    "min_similarity": 0.3,
                },
                depends_on=["fetch_artifact"],
                condition="$stages.fetch_artifact.output.artifact.safe_transcript_text",
                timeout_seconds=30,
                retry_policy=RetryPolicy(max_retries=2, backoff_seconds=2.0),
                on_failure="fail",
            ),

            # ── Stage 3: Brain LLM contradiction analysis ─────
            StageDefinition(
                stage_id="detect_contradictions",
                name="Brain Contradiction Analysis",
                description=(
                    "Brain engine uses LLM reasoning to compare rules from "
                    "the current session against candidate rules from other "
                    "sessions. Identifies true contradictions, assigns severity, "
                    "and suggests resolutions."
                ),
                engine="brain",
                endpoint="/api/v1/brain/detect-contradictions",
                input_mapping={
                    "tenant_id": "$workflow.tenant_id",
                    "session_id": "$workflow.session_id",
                    # Current session's rules from prior rule-extraction chain
                    # Alias: "nexus.rule-extraction" → "rule_extraction"
                    "current_rules": "$workflow.input.prior_consumer_outputs.rule_extraction.extract_rules.rules",
                    # Candidate rules from Backbone search
                    "candidate_rules": "$stages.search_related_rules.output.results",
                },
                depends_on=["search_related_rules"],
                condition="$stages.search_related_rules.output.results",
                timeout_seconds=120,
                retry_policy=RetryPolicy(max_retries=2, backoff_seconds=5.0),
                on_failure="continue",
            ),

            # ── Stage 4: Persist contradictions to database ───
            StageDefinition(
                stage_id="persist_contradictions",
                name="Persist Contradictions",
                description=(
                    "Store detected contradictions in the PostgreSQL "
                    "contradictions table via Spine engine for tracking "
                    "and resolution."
                ),
                engine="spine",
                endpoint="/api/v1/spine/persist-contradictions",
                input_mapping={
                    "tenant_id": "$workflow.tenant_id",
                    "session_id": "$workflow.session_id",
                    "contradictions": "$stages.detect_contradictions.output.contradictions",
                },
                depends_on=["detect_contradictions"],
                condition="$stages.detect_contradictions.output.contradictions",
                timeout_seconds=30,
                retry_policy=RetryPolicy(max_retries=2, backoff_seconds=2.0),
                on_failure="continue",
            ),
        ],
    )
