"""
Backbone Engine — Modular Sub-package Tests.

Tests the graph and vector modules refactored from the
monolithic backbone-engine/main.py.

Tests use the in-memory stores only (no Neo4j / Milvus required).
"""

import pytest
import sys
import os
import asyncio

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "engines", "backbone-engine"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "sdk", "nexus-sdk"))


# ─── In-Memory Graph Store ─────────────────────────────────────


class TestInMemoryGraphStore:
    """Test InMemoryGraphStore from app.graph."""

    def test_import(self):
        from app.graph import InMemoryGraphStore
        assert InMemoryGraphStore is not None

    @pytest.mark.asyncio
    async def test_create_and_get_node(self):
        from app.graph import InMemoryGraphStore
        store = InMemoryGraphStore()
        node_id = await store.create_node(
            node_type="BusinessRule",
            properties={"rule_text": "Test rule"},
            tenant_id="t1",
        )
        assert node_id is not None
        node = await store.get_node(node_id)
        assert node is not None
        assert node["node_type"] == "BusinessRule"
        assert node["properties"]["rule_text"] == "Test rule"
        assert node["tenant_id"] == "t1"

    @pytest.mark.asyncio
    async def test_get_missing_node(self):
        from app.graph import InMemoryGraphStore
        store = InMemoryGraphStore()
        node = await store.get_node("nonexistent")
        assert node is None

    @pytest.mark.asyncio
    async def test_create_relation(self):
        from app.graph import InMemoryGraphStore
        store = InMemoryGraphStore()
        id1 = await store.create_node("A", {"name": "node1"}, "t1")
        id2 = await store.create_node("B", {"name": "node2"}, "t1")
        ok = await store.create_relation(id1, id2, "HAS_RULE", {"weight": 1})
        assert ok is True

    @pytest.mark.asyncio
    async def test_create_relation_missing_node(self):
        from app.graph import InMemoryGraphStore
        store = InMemoryGraphStore()
        id1 = await store.create_node("A", {"name": "n"}, "t1")
        ok = await store.create_relation(id1, "missing", "RELATED_TO", {})
        assert ok is False

    @pytest.mark.asyncio
    async def test_get_neighbors(self):
        from app.graph import InMemoryGraphStore
        store = InMemoryGraphStore()
        id1 = await store.create_node("A", {"name": "n1"}, "t1")
        id2 = await store.create_node("B", {"name": "n2"}, "t1")
        id3 = await store.create_node("C", {"name": "n3"}, "t1")
        await store.create_relation(id1, id2, "HAS_RULE", {})
        await store.create_relation(id1, id3, "RELATED_TO", {})
        neighbors = store.get_neighbors(id1)
        assert len(neighbors) == 2

    @pytest.mark.asyncio
    async def test_get_neighbors_filtered(self):
        from app.graph import InMemoryGraphStore
        store = InMemoryGraphStore()
        id1 = await store.create_node("A", {"name": "n1"}, "t1")
        id2 = await store.create_node("B", {"name": "n2"}, "t1")
        id3 = await store.create_node("C", {"name": "n3"}, "t1")
        await store.create_relation(id1, id2, "HAS_RULE", {})
        await store.create_relation(id1, id3, "RELATED_TO", {})
        neighbors = store.get_neighbors(id1, relation_type="HAS_RULE")
        assert len(neighbors) == 1

    @pytest.mark.asyncio
    async def test_search_by_type(self):
        from app.graph import InMemoryGraphStore
        store = InMemoryGraphStore()
        await store.create_node("BusinessRule", {"r": "1"}, "t1")
        await store.create_node("BusinessRule", {"r": "2"}, "t1")
        await store.create_node("TestCase", {"t": "1"}, "t1")
        results = await store.search_by_type("BusinessRule", "t1", limit=10)
        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_search_text(self):
        from app.graph import InMemoryGraphStore
        store = InMemoryGraphStore()
        await store.create_node("BusinessRule", {"rule_text": "Premium must exceed $100"}, "t1")
        await store.create_node("BusinessRule", {"rule_text": "Deductible applies to claims"}, "t1")
        results = await store.search_text("premium", "t1")
        assert len(results) >= 1

    @pytest.mark.asyncio
    async def test_get_stats(self):
        from app.graph import InMemoryGraphStore
        store = InMemoryGraphStore()
        await store.create_node("A", {}, "t1")
        await store.create_node("B", {}, "t1")
        stats = await store.get_stats("t1")
        assert isinstance(stats, dict)
        # Should have node count info
        total_nodes = stats.get("total_nodes", stats.get("nodes", 0))
        assert total_nodes >= 2


# ─── Neo4j Graph Store (import only) ──────────────────────────


class TestNeo4jGraphStore:
    """Test Neo4jGraphStore can be imported (no live connection needed)."""

    def test_import(self):
        from app.graph import Neo4jGraphStore
        assert Neo4jGraphStore is not None

    def test_init(self):
        from app.graph import Neo4jGraphStore
        store = Neo4jGraphStore("bolt://localhost:7687", "neo4j", "test")
        assert store is not None


# ─── In-Memory Vector Store ────────────────────────────────────


class TestInMemoryVectorStore:
    """Test InMemoryVectorStore from app.vector."""

    def test_import(self):
        from app.vector import InMemoryVectorStore
        assert InMemoryVectorStore is not None

    def test_init_dimensions(self):
        from app.vector import InMemoryVectorStore
        store = InMemoryVectorStore(dimension=128)
        assert store is not None

    def test_store_and_search(self):
        from app.vector import InMemoryVectorStore
        store = InMemoryVectorStore(dimension=384)
        # Store a few items
        store.store("node_1", "Premium must exceed one hundred dollars", {"type": "rule"})
        store.store("node_2", "Deductible applies on all claims", {"type": "rule"})
        store.store("node_3", "Navigate to the login page", {"type": "step"})

        # Search
        results = store.search("premium amount", limit=2, min_similarity=0.0)
        assert len(results) >= 1
        assert results[0]["node_id"] in ("node_1", "node_2", "node_3")

    def test_search_empty_store(self):
        from app.vector import InMemoryVectorStore
        store = InMemoryVectorStore(dimension=384)
        results = store.search("anything", limit=5, min_similarity=0.0)
        assert len(results) == 0

    def test_search_respects_limit(self):
        from app.vector import InMemoryVectorStore
        store = InMemoryVectorStore(dimension=384)
        for i in range(10):
            store.store(f"n_{i}", f"text number {i}", {})
        results = store.search("text", limit=3, min_similarity=0.0)
        assert len(results) <= 3


# ─── Milvus Vector Store (import only) ────────────────────────


class TestMilvusVectorStore:
    """Test MilvusVectorStore can be imported."""

    def test_import(self):
        from app.vector import MilvusVectorStore
        assert MilvusVectorStore is not None

    def test_init(self):
        from app.vector import MilvusVectorStore
        store = MilvusVectorStore(
            host="localhost", port=19530,
            collection_name="test", dimension=384,
        )
        assert store is not None


# ─── Integration: Main module imports from sub-packages ───────


class TestBackboneMainImports:
    """Verify main.py v0.2.0 correctly imports from sub-packages."""

    def test_main_version(self):
        from main import BackboneEngine
        engine = BackboneEngine()
        assert engine.version == "0.2.0"

    def test_main_imports_graph(self):
        from main import InMemoryGraphStore, Neo4jGraphStore
        assert InMemoryGraphStore is not None
        assert Neo4jGraphStore is not None

    def test_main_imports_vector(self):
        from main import InMemoryVectorStore, MilvusVectorStore
        assert InMemoryVectorStore is not None
        assert MilvusVectorStore is not None

    def test_node_type_enum(self):
        from main import NodeType
        assert NodeType.BUSINESS_RULE.value == "BusinessRule"
        assert NodeType.TEST_CASE.value == "TestCase"

    def test_relation_type_enum(self):
        from main import RelationType
        assert RelationType.CONFIRMED_BY.value == "CONFIRMED_BY"
        assert RelationType.RELATED_TO.value == "RELATED_TO"
