"""
Built-in Consumer Chain: Knowledge Graph Assembly.

Phase 2.4 — Builds an entity graph from the canonical artifact's
visual analysis, linking UI screens, flows, API endpoints, rules,
and test cases into a navigable knowledge structure.

Triggered automatically after canonical-processing completes.
Uses outputs from rule-extraction and test-generation if available.

DAG:
    fetch_artifact ──┬─→ store_session_node
                     ├─→ store_ui_nodes → link_screen_flows
                     └─→ link_rules_to_sessions
"""

from ..schema import (
    ChainDefinition,
    RetryPolicy,
    StageDefinition,
)


def build_knowledge_graph_chain() -> ChainDefinition:
    """Build the knowledge-graph assembly consumer chain."""
    return ChainDefinition(
        chain_id="nexus.knowledge-graph",
        name="Knowledge Graph Assembly",
        description=(
            "Assemble the knowledge graph: fetch artifact → store session node → "
            "create UI screen/flow nodes from visual analysis → link screen flows → "
            "link rules to their source sessions. "
            "Auto-triggered after canonical-processing completes."
        ),
        version="1.0.0",
        tags=["consumer", "knowledge", "graph", "backbone"],
        stages=[
            # ── Stage 1: Fetch canonical artifact ─────────────
            StageDefinition(
                stage_id="fetch_artifact",
                name="Fetch Canonical Artifact",
                description=(
                    "Retrieve the canonical artifact from Spine. Needed for "
                    "visual analysis data (frames, scene transitions, OCR text) "
                    "that feeds into the knowledge graph."
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

            # ── Stage 2: Store session as a KT_SESSION node ──
            StageDefinition(
                stage_id="store_session_node",
                name="Store Session Node",
                description=(
                    "Create a KT_SESSION node in the knowledge graph representing "
                    "this canonical processing run, with metadata about duration, "
                    "participant count, and quality score."
                ),
                engine="backbone",
                endpoint="/api/v1/backbone/nodes",
                input_mapping={
                    "tenant_id": "$workflow.tenant_id",
                    "node_type": "KT_SESSION",
                    "properties": {
                        "session_id": "$workflow.session_id",
                        "artifact_id": "$workflow.input.canonical_artifact_id",
                        "workflow_id": "$workflow.input.canonical_workflow_id",
                        "duration_seconds": "$stages.fetch_artifact.output.artifact.duration_seconds",
                        "safe_transcript_text": "$stages.fetch_artifact.output.artifact.safe_transcript_text",
                        "quality_score": "$stages.fetch_artifact.output.artifact.brain_quality_score",
                    },
                    "source": {
                        "session_id": "$workflow.session_id",
                        "engine": "orchestrator",
                    },
                    "tags": ["canonical", "kt-session"],
                },
                depends_on=["fetch_artifact"],
                timeout_seconds=30,
                retry_policy=RetryPolicy(max_retries=2, backoff_seconds=2.0),
                on_failure="continue",
            ),

            # ── Stage 3: Store UI screen nodes from visual analysis ──
            StageDefinition(
                stage_id="store_ui_nodes",
                name="Store UI Screen Nodes",
                description=(
                    "For each keyframe/scene in the visual analysis, create "
                    "a UI_SCREEN node with OCR text, screen type, and "
                    "application classification."
                ),
                engine="backbone",
                endpoint="/api/v1/backbone/nodes",
                input_mapping={
                    "tenant_id": "$workflow.tenant_id",
                    "node_type": "UI_SCREEN",
                    "properties": "$temp.item",
                    "source": {
                        "session_id": "$workflow.session_id",
                        "engine": "eyes",
                    },
                    "tags": ["visual", "ui-screen"],
                },
                depends_on=["fetch_artifact"],
                condition="$stages.fetch_artifact.output.artifact.full_artifact_json.visual_analysis.frames",
                timeout_seconds=30,
                retry_policy=RetryPolicy(max_retries=1, backoff_seconds=2.0),
                on_failure="continue",
                for_each="$stages.fetch_artifact.output.artifact.full_artifact_json.visual_analysis.frames",
                for_each_item_key="item",
                for_each_concurrency=5,
                # Build consecutive node pairs for link_screen_flows
                output_transform=(
                    "{"
                    "'items': result['items'], "
                    "'count': result['count'], "
                    "'pairs': ["
                    "{'from_node_id': result['items'][i]['node_id'], "
                    "'to_node_id': result['items'][i+1]['node_id'], "
                    "'index': i} "
                    "for i in range(len(result['items'])-1) "
                    "if 'node_id' in result['items'][i] "
                    "and 'node_id' in result['items'][i+1]"
                    "]}"
                ),
            ),

            # ── Stage 4: Link screen flow transitions ─────────
            StageDefinition(
                stage_id="link_screen_flows",
                name="Link Screen Flow Transitions",
                description=(
                    "Create NAVIGATES_TO relations between consecutive UI "
                    "screen nodes based on the visual graph's scene "
                    "transition ordering."
                ),
                engine="backbone",
                endpoint="/api/v1/backbone/relations",
                input_mapping={
                    "tenant_id": "$workflow.tenant_id",
                    "from_node_id": "$temp.item.from_node_id",
                    "to_node_id": "$temp.item.to_node_id",
                    "relation_type": "NAVIGATES_TO",
                    "properties": {
                        "session_id": "$workflow.session_id",
                        "transition_index": "$temp.item.index",
                    },
                },
                depends_on=["store_ui_nodes"],
                condition="$stages.store_ui_nodes.output.pairs",
                timeout_seconds=30,
                on_failure="continue",
                for_each="$stages.store_ui_nodes.output.pairs",
                for_each_item_key="item",
                for_each_concurrency=3,
            ),

            # ── Stage 5: Link rules to session node ───────────
            StageDefinition(
                stage_id="link_rules_to_session",
                name="Link Rules to Session",
                description=(
                    "Create EXTRACTED_FROM relations between stored business "
                    "rules and the KT_SESSION node. Uses prior consumer "
                    "outputs from rule-extraction if available."
                ),
                engine="backbone",
                endpoint="/api/v1/backbone/relations",
                input_mapping={
                    "tenant_id": "$workflow.tenant_id",
                    "from_node_id": "$temp.item.node_id",
                    "to_node_id": "$stages.store_session_node.output.node_id",
                    "relation_type": "EXTRACTED_FROM",
                    "properties": {
                        "session_id": "$workflow.session_id",
                        "engine": "heart",
                    },
                },
                depends_on=["store_session_node"],
                condition="$workflow.input.prior_consumer_outputs.rule_extraction.store_rules.items",
                timeout_seconds=30,
                on_failure="continue",
                for_each="$workflow.input.prior_consumer_outputs.rule_extraction.store_rules.items",
                for_each_item_key="item",
                for_each_concurrency=5,
            ),
        ],
    )
