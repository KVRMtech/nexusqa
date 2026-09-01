"""
Nexus Backbone Engine v0.2.0 — Knowledge Graph + Vector Store.

The memory of the platform. Every piece of knowledge extracted
from KT sessions gets stored here as structured graph nodes
AND vector embeddings for semantic search.

Two storage backends:
1. Neo4j — for structured relationships (business rules → test cases → UI flows)
2. Milvus / In-memory — for semantic similarity search

Each backend supports a real driver (Neo4j/Milvus) with in-memory
fallback for dev/test environments where those services aren't running.

v0.2.0 — Modular refactor:
  app.graph   → InMemoryGraphStore, Neo4jGraphStore
  app.vector  → InMemoryVectorStore, MilvusVectorStore
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional
from enum import Enum

from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel, Field

from nexus_sdk import NexusEngine, EngineConfig
from nexus_sdk.config import production_guard
from nexus_sdk.models import (
    NexusRequest, NexusResponse, BusinessRule, SourceReference, Confidence,
)
from nexus_sdk.auth import NexusUser, get_current_user
from nexus_sdk.events import NexusEvent

# ── Modular sub-packages ───────────────────────────────────────
from app.graph import InMemoryGraphStore, Neo4jGraphStore
from app.vector import InMemoryVectorStore, MilvusVectorStore

logger = logging.getLogger(__name__)


# ─── Configuration ─────────────────────────────────────────────

class BackboneConfig(EngineConfig):
    engine_name: str = "backbone"
    engine_port: int = 8005

    # Neo4j
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = Field(
        default="change-me",
        description="Neo4j password — set via NEO4J_PASSWORD env var",
    )
    neo4j_database: str = "nexusqa"

    # Milvus
    milvus_host: str = "localhost"
    milvus_port: int = 19530
    milvus_collection: str = "nexus_knowledge"

    # Embedding model (on-prem)
    embedding_model: str = "BAAI/bge-large-en-v1.5"
    embedding_model_path: str = "./models/bge-large-en-v1.5"
    embedding_dimension: int = 1024


# ─── Node Types in the Knowledge Graph ────────────────────────

class NodeType(str, Enum):
    # Domain knowledge
    BUSINESS_RULE = "BusinessRule"
    PRODUCT = "Product"
    COVERAGE = "Coverage"
    RATE_TABLE = "RateTable"
    FORM = "Form"

    # Workflow knowledge
    UI_FLOW = "UIFlow"
    UI_SCREEN = "UIScreen"
    UI_ELEMENT = "UIElement"
    API_ENDPOINT = "APIEndpoint"
    DATABASE_TABLE = "DatabaseTable"
    MAINFRAME_SCREEN = "MainframeScreen"

    # Test artifacts
    TEST_CASE = "TestCase"
    TEST_STEP = "TestStep"
    TEST_RESULT = "TestResult"

    # Source tracking
    KT_SESSION = "KTSession"
    SME_SPEAKER = "SMESpeaker"

    # Insurance-specific
    STATE_REGULATION = "StateRegulation"
    FILING = "Filing"
    ENDORSEMENT = "Endorsement"

    # Knowledge substrate (added by alembic 020 / Phase 1)
    TRANSCRIPT_SEGMENT = "TranscriptSegment"
    KNOWLEDGE_CARD = "KnowledgeCard"


class RelationType(str, Enum):
    # Business domain
    HAS_RULE = "HAS_RULE"
    COVERS = "COVERS"
    USES_RATE = "USES_RATE"
    REQUIRES_FORM = "REQUIRES_FORM"
    REGULATES = "REGULATES"

    # Workflows
    NAVIGATES_TO = "NAVIGATES_TO"
    CONTAINS_ELEMENT = "CONTAINS_ELEMENT"
    CALLS_API = "CALLS_API"
    READS_TABLE = "READS_TABLE"
    WRITES_TABLE = "WRITES_TABLE"

    # Testing
    TESTS_RULE = "TESTS_RULE"
    HAS_STEP = "HAS_STEP"
    PRODUCED_RESULT = "PRODUCED_RESULT"

    # Source tracking
    EXTRACTED_FROM = "EXTRACTED_FROM"
    SPOKEN_BY = "SPOKEN_BY"
    CONFIRMED_BY = "CONFIRMED_BY"

    # General
    DEPENDS_ON = "DEPENDS_ON"
    CONTRADICTS = "CONTRADICTS"
    SUPERSEDES = "SUPERSEDES"
    RELATED_TO = "RELATED_TO"

    # Atlas / substrate (added by Phase 1)
    PART_OF = "PART_OF"
    RESOLVES_TO = "RESOLVES_TO"


# ─── Request/Response Models ──────────────────────────────────

class StoreNodeRequest(NexusRequest):
    node_type: str = Field(..., description="Type from NodeType enum")
    properties: dict = Field(..., description="Node properties")
    source: Optional[SourceReference] = None
    tags: list[str] = Field(default_factory=list)


class StoreNodeResponse(NexusResponse):
    node_id: str
    node_type: str
    embedded: bool = False


class CreateRelationRequest(NexusRequest):
    from_node_id: str
    to_node_id: str
    relation_type: str = Field(..., description="Type from RelationType enum")
    properties: dict = Field(default_factory=dict)


class SearchRequest(NexusRequest):
    query: str = Field(..., description="Natural language search query")
    node_types: list[str] = Field(default_factory=list, description="Filter by node types")
    limit: int = Field(default=10, ge=1, le=100)
    min_similarity: float = Field(default=0.7, ge=0.0, le=1.0)


class SearchResult(BaseModel):
    node_id: str
    node_type: str
    properties: dict
    similarity: float
    source: Optional[dict] = None


class SearchResponse(NexusResponse):
    results: list[SearchResult]
    total: int


class GraphQueryRequest(NexusRequest):
    cypher: str = Field(..., description="Cypher query for Neo4j")
    parameters: dict = Field(default_factory=dict)


class StoreBusinessRuleRequest(NexusRequest):
    rule: BusinessRule
    session_id: Optional[str] = None


class StoreBusinessRuleResponse(NexusResponse):
    rule_id: str
    node_id: str
    related_rules: list[str] = Field(
        default_factory=list, description="IDs of similar existing rules"
    )


# ─── The Backbone Engine v0.2.0 ──────────────────────────────

class BackboneEngine(NexusEngine):
    def __init__(self):
        self.cfg = BackboneConfig()
        super().__init__(
            name="backbone",
            version="0.2.0",
            config=self.cfg,
            description="Knowledge Graph + Vector Store Engine",
        )
        self.graph = InMemoryGraphStore()
        self.vectors = InMemoryVectorStore(dimension=384)
        self._neo4j_store: Optional[Neo4jGraphStore] = None
        self._milvus_store: Optional[MilvusVectorStore] = None

    async def on_startup(self):
        """Connect to Neo4j/Milvus (or fall back), load embedding model."""
        # ── Try Milvus vector store ────────────────────────────
        try:
            milvus = MilvusVectorStore(
                host=self.cfg.milvus_host,
                port=self.cfg.milvus_port,
                collection_name=self.cfg.milvus_collection,
                dimension=384,
            )
            await milvus.connect()
            await milvus.load_model()
            self.vectors = milvus
            self._milvus_store = milvus
            self.health.add_check("milvus", milvus.health_check)
            if milvus._use_real_embeddings:
                self.health.set_mode(
                    "vector_store",
                    f"milvus + sentence-transformers ({milvus._model_name})",
                )
            else:
                self.health.set_mode("vector_store", "milvus (hash fallback)")
            logger.info(
                "backbone: using Milvus vector store at %s:%d",
                self.cfg.milvus_host,
                self.cfg.milvus_port,
            )
        except Exception as exc:
            logger.warning(
                "backbone: Milvus unavailable (%s) — using in-memory vector store",
                exc,
            )
            await self.vectors.load_model()
            if self.vectors._use_real_embeddings:
                self.health.set_mode(
                    "vector_store",
                    f"in-memory + sentence-transformers ({self.vectors._model_name})",
                )
            else:
                self.health.set_mode(
                    "vector_store",
                    "in-memory (hash fallback — install sentence-transformers)",
                )

        # ── Try Neo4j ──────────────────────────────────────────
        try:
            neo4j_pw = self.cfg.neo4j_password
            if neo4j_pw and neo4j_pw != "change-me":
                store = Neo4jGraphStore(
                    self.cfg.neo4j_uri,
                    self.cfg.neo4j_user,
                    neo4j_pw,
                )
                await store.connect()
                self.graph = store
                self._neo4j_store = store
                logger.info("backbone: using Neo4j graph store")
                self.health.add_check("neo4j", store.health_check)
                self.health.set_mode("graph_store", "neo4j")
            else:
                logger.warning(
                    "backbone: Neo4j password not configured – using in-memory graph store"
                )
                self.health.set_mode("graph_store", "in-memory")
        except Exception as exc:
            logger.warning(
                "backbone: Neo4j unavailable (%s) – using in-memory fallback", exc
            )
            self.health.set_mode("graph_store", "in-memory")

        # ── Degradation guard: refuse silent in-memory stores by default ──
        using_inmemory_vector = self._milvus_store is None
        using_inmemory_graph = self._neo4j_store is None
        if using_inmemory_vector or using_inmemory_graph:
            stores = []
            if using_inmemory_vector:
                stores.append("vector (Milvus)")
            if using_inmemory_graph:
                stores.append("graph (Neo4j)")
            production_guard(
                "Backbone persistent stores: " + " and ".join(stores),
                available=False,
            )

        # Load graph schema extensions from domain plugins
        try:
            schema_ext = self.plugin_registry.get_merged_graph_schema()
            if schema_ext:
                if schema_ext.node_types:
                    for nt in schema_ext.node_types:
                        if nt.type_id not in NodeType.__members__:
                            if not hasattr(self, "_dynamic_node_types"):
                                self._dynamic_node_types = set()
                            self._dynamic_node_types.add(nt.type_id)
                if schema_ext.relationship_types:
                    for rt in schema_ext.relationship_types:
                        if rt.type_id not in RelationType.__members__:
                            if not hasattr(self, "_dynamic_relation_types"):
                                self._dynamic_relation_types = set()
                            self._dynamic_relation_types.add(rt.type_id)
        except Exception:
            logger.debug(
                "Plugin schema extensions not available, using hardcoded schema",
                exc_info=True,
            )

        if self.event_bus:
            await self.event_bus.subscribe(
                "heart.rules.extracted", self._handle_rules_extracted
            )
            await self.event_bus.subscribe(
                "eyes.analysis.completed", self._handle_visual_analysis
            )

        # ── Canonical workflow workers (Phase 1) ───────────────
        # Backbone is the join point — runs LAST in every canonical
        # plan. The handler projects upstream checkpoint state into
        # the knowledge graph (Neo4j) + vector store (Milvus). Both
        # operations are I/O-bound, so a CPU lane is sufficient; the
        # GPU lane starts too in case a future vision-embedding step
        # is added.
        self._workflow_workers: list = []
        orchestrator_url = os.environ.get("NEXUS_ORCHESTRATOR_URL", "")
        if orchestrator_url:
            await self._start_canonical_workflow_workers(orchestrator_url)
        else:
            logger.info(
                "backbone.workflow_workers_disabled "
                "reason=NEXUS_ORCHESTRATOR_URL_unset",
            )

    async def _start_canonical_workflow_workers(self, orchestrator_url: str) -> None:
        import asyncio
        from nexus_sdk.workflows import (
            StepKind, WorkerConfig, WorkflowWorker, queue_name,
        )
        from app.workflow_handlers import BackboneWorkflowHandlers

        token = os.environ.get("NEXUS_WORKER_TOKEN", "")
        handlers = BackboneWorkflowHandlers(self)

        # Backbone is I/O-bound (Neo4j + Milvus writes). High concurrency
        # pays off here — most time is spent waiting on the graph store.
        cpu_conc = int(os.environ.get("BACKBONE_WORKER_CONCURRENCY_CPU", "8"))
        gpu_conc = int(os.environ.get("BACKBONE_WORKER_CONCURRENCY_GPU", "2"))
        for kind in (StepKind.CPU, StepKind.GPU):
            lane = queue_name("backbone", kind)
            q = self._build_workflow_lane_queue(lane)
            ok = await q.connect()
            if not ok:
                logger.error(
                    "backbone.workflow_worker_redis_unreachable lane=%s", lane,
                )
                continue
            worker = WorkflowWorker(
                config=WorkerConfig(
                    engine_name="backbone",
                    kind=kind,
                    orchestrator_url=orchestrator_url,
                    auth_token=token,
                    concurrency=cpu_conc if kind == StepKind.CPU else gpu_conc,
                ),
                queue=q,
            )
            handlers.register(worker)
            self._workflow_workers.append(worker)
            asyncio.create_task(
                worker.run(), name=f"workflow_worker.{lane}",
            )
            logger.info(
                "backbone.workflow_worker_started lane=%s orchestrator=%s",
                lane, orchestrator_url,
            )

    def _build_workflow_lane_queue(self, lane: str):
        from nexus_sdk.queue import JobQueue

        return JobQueue(
            engine_name=lane,
            redis_host=os.environ.get("REDIS_HOST", "redis"),
            redis_port=int(os.environ.get("REDIS_PORT", "6379")),
            redis_password=os.environ.get("REDIS_PASSWORD", ""),
            redis_db=int(os.environ.get("REDIS_DB", "3")),
        )

    async def _handle_rules_extracted(self, event: NexusEvent):
        """Store business rules extracted by Heart."""
        rules = event.data.get("rules", [])
        for rule_data in rules:
            node_id = await self.graph.create_node(
                node_type=NodeType.BUSINESS_RULE.value,
                properties=rule_data,
                tenant_id=event.tenant_id,
                source={
                    "session_id": event.session_id,
                    "engine": "heart",
                },
            )
            rule_text = (
                rule_data.get("rule_text", rule_data.get("description", ""))
                + " "
                + " ".join(rule_data.get("conditions", []))
            )
            self.vectors.store(
                node_id,
                rule_text,
                {"type": "BusinessRule", "tenant_id": event.tenant_id},
            )

    async def _handle_visual_analysis(self, event: NexusEvent):
        """Store UI flow information from Eyes."""
        session_id = event.data.get("session_id", "")
        await self.graph.create_node(
            node_type=NodeType.KT_SESSION.value,
            properties={
                "session_id": session_id,
                "frame_count": event.data.get("frame_count", 0),
                "application_types": event.data.get("application_types", []),
            },
            tenant_id=event.tenant_id,
        )

    async def on_shutdown(self):
        """Release Milvus + Neo4j connections."""
        if self._milvus_store:
            await self._milvus_store.close()
        if self._neo4j_store:
            await self._neo4j_store.close()

    def register_routes(self, app):

        # ── Store Node ─────────────────────────────────────────

        @app.post("/api/v1/backbone/nodes", response_model=StoreNodeResponse)
        async def store_node(
            req: StoreNodeRequest,
            user: NexusUser = Depends(get_current_user),
        ):
            """Store a knowledge node in the graph."""
            start = time.monotonic()

            node_id = await self.graph.create_node(
                node_type=req.node_type,
                properties=req.properties,
                tenant_id=req.tenant_id,
                source=req.source.model_dump() if req.source else None,
                tags=req.tags,
            )

            text_content = " ".join(
                str(v) for v in req.properties.values() if isinstance(v, str)
            )
            if text_content.strip():
                self.vectors.store(
                    node_id,
                    text_content,
                    {"type": req.node_type, "node_type": req.node_type, "tenant_id": req.tenant_id},
                )

            elapsed_ms = (time.monotonic() - start) * 1000

            return StoreNodeResponse(
                success=True,
                trace_id=req.trace_id,
                engine="backbone",
                engine_version="0.2.0",
                processing_time_ms=round(elapsed_ms, 2),
                node_id=node_id,
                node_type=req.node_type,
                embedded=bool(text_content.strip()),
            )

        # ── Get Node ──────────────────────────────────────────

        @app.get("/api/v1/backbone/nodes/{node_id}")
        async def get_node(
            node_id: str,
            user: NexusUser = Depends(get_current_user),
        ):
            node = await self.graph.get_node(node_id)
            if not node:
                raise HTTPException(status_code=404, detail="Node not found")
            return node

        # ── Create Relation ────────────────────────────────────

        @app.post("/api/v1/backbone/relations")
        async def create_relation(
            req: CreateRelationRequest,
            user: NexusUser = Depends(get_current_user),
        ):
            """Create a relationship between two nodes."""
            success = await self.graph.create_relation(
                req.from_node_id,
                req.to_node_id,
                req.relation_type,
                req.properties,
            )
            if not success:
                raise HTTPException(
                    status_code=404, detail="One or both nodes not found"
                )
            return {"success": True, "relation_type": req.relation_type}

        # ── Get Node Neighbors ─────────────────────────────────

        @app.get("/api/v1/backbone/nodes/{node_id}/neighbors")
        async def get_neighbors(
            node_id: str,
            relation_type: Optional[str] = Query(default=None),
            user: NexusUser = Depends(get_current_user),
        ):
            result = self.graph.get_neighbors(node_id, relation_type)
            # Support both sync (InMemoryGraphStore) and async (Neo4jGraphStore)
            if hasattr(result, '__await__'):
                result = await result
            return result

        # ── Semantic Search ────────────────────────────────────

        @app.post("/api/v1/backbone/search", response_model=SearchResponse)
        async def search(
            req: SearchRequest,
            user: NexusUser = Depends(get_current_user),
        ):
            """Search knowledge base using natural language."""
            start = time.monotonic()

            vector_results = self.vectors.search(
                req.query,
                limit=req.limit * 2,
                min_similarity=req.min_similarity,
            )

            results = []
            for vr in vector_results:
                node = await self.graph.get_node(vr["node_id"])
                if node and node["tenant_id"] == req.tenant_id:
                    if not req.node_types or node["node_type"] in req.node_types:
                        results.append(
                            SearchResult(
                                node_id=vr["node_id"],
                                node_type=node["node_type"],
                                properties=node["properties"],
                                similarity=vr["similarity"],
                                source=node.get("source"),
                            )
                        )

            if len(results) < req.limit:
                text_results = await self.graph.search_text(
                    req.query, req.tenant_id, req.node_types, req.limit,
                )
                existing_ids = {r.node_id for r in results}
                for tr in text_results:
                    if tr["node_id"] not in existing_ids:
                        results.append(SearchResult(**tr))

            results = results[: req.limit]
            elapsed_ms = (time.monotonic() - start) * 1000

            return SearchResponse(
                success=True,
                trace_id=req.trace_id,
                engine="backbone",
                engine_version="0.2.0",
                processing_time_ms=round(elapsed_ms, 2),
                results=results,
                total=len(results),
            )

        # ── Store Business Rule (high-level with dedup) ────────

        @app.post(
            "/api/v1/backbone/rules",
            response_model=StoreBusinessRuleResponse,
        )
        async def store_business_rule(
            req: StoreBusinessRuleRequest,
            user: NexusUser = Depends(get_current_user),
        ):
            """Store a business rule with deduplication.

            Dedup logic (search BEFORE create):
            - similarity >= 0.95 → duplicate; add CONFIRMED_BY edge
            - 0.80 <= similarity < 0.95 → related; create + RELATED_TO edges
            - similarity < 0.80 → novel; create, no edges
            """
            start = time.monotonic()
            rule = req.rule

            embed_text = (
                f"{rule.rule_text} {' '.join(rule.conditions)} {rule.category}"
            )

            DEDUP_THRESHOLD = 0.95
            RELATED_THRESHOLD = 0.80

            similar = self.vectors.search(
                embed_text, limit=5, min_similarity=RELATED_THRESHOLD
            )

            # Near-duplicate check
            for match in similar:
                if match["similarity"] >= DEDUP_THRESHOLD:
                    existing_id = match["node_id"]
                    existing_node = await self.graph.get_node(existing_id)
                    if (
                        existing_node
                        and existing_node["tenant_id"] == req.tenant_id
                    ):
                        confirmation_props = {
                            "reason": "semantic_dedup",
                            "similarity": match["similarity"],
                            "confirming_rule_id": rule.rule_id,
                            "session_id": req.session_id or "",
                            "confirmed_at": datetime.now(
                                timezone.utc
                            ).isoformat(),
                        }
                        existing_node["properties"].setdefault(
                            "confirmation_count", 0
                        )
                        existing_node["properties"]["confirmation_count"] += 1

                        await self.graph.create_relation(
                            existing_id,
                            existing_id,
                            RelationType.CONFIRMED_BY.value,
                            confirmation_props,
                        )

                        elapsed_ms = (time.monotonic() - start) * 1000

                        if self.event_bus:
                            await self.event_bus.publish(
                                NexusEvent(
                                    event_type="backbone.rule.deduplicated",
                                    tenant_id=req.tenant_id,
                                    trace_id=req.trace_id,
                                    engine="backbone",
                                    data={
                                        "node_id": existing_id,
                                        "rule_id": rule.rule_id,
                                        "existing_rule_id": existing_node[
                                            "properties"
                                        ].get("rule_id"),
                                        "similarity": match["similarity"],
                                    },
                                )
                            )

                        return StoreBusinessRuleResponse(
                            success=True,
                            trace_id=req.trace_id,
                            engine="backbone",
                            engine_version="0.2.0",
                            processing_time_ms=round(elapsed_ms, 2),
                            rule_id=rule.rule_id,
                            node_id=existing_id,
                            related_rules=[existing_id],
                        )

            # ── Not a duplicate — create new node ──────────────
            rule_props = {
                "rule_id": rule.rule_id,
                "rule_text": rule.rule_text,
                "category": rule.category,
                "conditions": rule.conditions,
                "exceptions": rule.exceptions,
                "product": rule.product or "",
                "jurisdiction": rule.jurisdiction or "",
                "confirmation_count": 0,
            }

            node_id = await self.graph.create_node(
                node_type=NodeType.BUSINESS_RULE.value,
                properties=rule_props,
                tenant_id=req.tenant_id,
                source=rule.source.model_dump() if rule.source else None,
                tags=rule.tags,
            )

            self.vectors.store(
                node_id,
                embed_text,
                {
                    "type": "BusinessRule",
                    "tenant_id": req.tenant_id,
                    "rule_id": rule.rule_id,
                },
            )

            related_ids = [
                s["node_id"]
                for s in similar
                if s["node_id"] != node_id
                and s["similarity"] < DEDUP_THRESHOLD
            ]
            for related_id in related_ids:
                await self.graph.create_relation(
                    node_id,
                    related_id,
                    RelationType.RELATED_TO.value,
                    {
                        "reason": "semantic_similarity",
                        "similarity": next(
                            (
                                s["similarity"]
                                for s in similar
                                if s["node_id"] == related_id
                            ),
                            0.0,
                        ),
                    },
                )

            elapsed_ms = (time.monotonic() - start) * 1000

            if self.event_bus:
                await self.event_bus.publish(
                    NexusEvent(
                        event_type="backbone.rule.stored",
                        tenant_id=req.tenant_id,
                        trace_id=req.trace_id,
                        engine="backbone",
                        data={
                            "node_id": node_id,
                            "rule_id": rule.rule_id,
                            "related_count": len(related_ids),
                        },
                    )
                )

            return StoreBusinessRuleResponse(
                success=True,
                trace_id=req.trace_id,
                engine="backbone",
                engine_version="0.2.0",
                processing_time_ms=round(elapsed_ms, 2),
                rule_id=rule.rule_id,
                node_id=node_id,
                related_rules=related_ids,
            )

        # ── List by Type ───────────────────────────────────────

        @app.get("/api/v1/backbone/nodes/type/{node_type}")
        async def list_by_type(
            node_type: str,
            limit: int = Query(default=50, ge=1, le=200),
            user: NexusUser = Depends(get_current_user),
        ):
            return await self.graph.search_by_type(
                node_type, user.tenant_id, limit
            )

        # ── Graph Stats ────────────────────────────────────────

        @app.get("/api/v1/backbone/stats")
        async def get_stats(
            user: NexusUser = Depends(get_current_user),
        ):
            """Get knowledge graph statistics."""
            return await self.graph.get_stats(user.tenant_id)


# ─── Entry Point ──────────────────────────────────────────────

def main():
    engine = BackboneEngine()
    engine.run()


if __name__ == "__main__":
    main()
