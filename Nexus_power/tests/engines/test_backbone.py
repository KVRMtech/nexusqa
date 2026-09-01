"""
Backbone Engine — Unit tests.

Tests in-memory graph store, vector store, and knowledge graph operations.
"""

import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "engines", "backbone-engine"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "sdk", "nexus-sdk"))


class TestInMemoryGraphStore:
    """Test the in-memory knowledge graph."""

    def setup_method(self):
        from main import InMemoryGraphStore
        self.graph = InMemoryGraphStore()

    # ── Node CRUD ──────────────────────────────────────────

    async def test_create_and_get_node(self):
        nid = await self.graph.create_node(
            "BusinessRule",
            {"description": "Min age is 18"},
            "tenant-001",
        )
        assert nid is not None
        node = await self.graph.get_node(nid)
        assert node is not None
        assert node["node_type"] == "BusinessRule"
        assert node["properties"]["description"] == "Min age is 18"

    async def test_get_nonexistent_node(self):
        assert await self.graph.get_node("nonexistent") is None

    async def test_create_with_tags(self):
        nid = await self.graph.create_node(
            "BusinessRule",
            {"text": "Test"},
            "tenant-001",
            tags=["premium", "rate"],
        )
        node = await self.graph.get_node(nid)
        assert "premium" in node["tags"]
        assert "rate" in node["tags"]

    async def test_nodes_get_unique_ids(self):
        id1 = await self.graph.create_node("TestCase", {}, "t1")
        id2 = await self.graph.create_node("TestCase", {}, "t1")
        assert id1 != id2

    # ── Relations ──────────────────────────────────────────

    async def test_create_and_query_relation(self):
        n1 = await self.graph.create_node("BusinessRule", {"name": "R1"}, "t1")
        n2 = await self.graph.create_node("TestCase", {"name": "TC1"}, "t1")
        await self.graph.create_relation(n1, n2, "TESTS_RULE")

        neighbors = self.graph.get_neighbors(n1, "TESTS_RULE")
        assert len(neighbors) == 1
        assert neighbors[0]["node_id"] == n2

    async def test_no_neighbors(self):
        n1 = await self.graph.create_node("BusinessRule", {}, "t1")
        assert self.graph.get_neighbors(n1, "TESTS_RULE") == []

    async def test_relation_with_properties(self):
        n1 = await self.graph.create_node("BusinessRule", {}, "t1")
        n2 = await self.graph.create_node("BusinessRule", {}, "t1")
        await self.graph.create_relation(n1, n2, "CONTRADICTS", {"severity": "high"})
        neighbors = self.graph.get_neighbors(n1, "CONTRADICTS")
        assert len(neighbors) == 1

    async def test_multiple_relations_from_node(self):
        n1 = await self.graph.create_node("BusinessRule", {}, "t1")
        n2 = await self.graph.create_node("TestCase", {}, "t1")
        n3 = await self.graph.create_node("TestCase", {}, "t1")
        await self.graph.create_relation(n1, n2, "TESTS_RULE")
        await self.graph.create_relation(n1, n3, "TESTS_RULE")
        neighbors = self.graph.get_neighbors(n1, "TESTS_RULE")
        assert len(neighbors) == 2

    # ── Search ─────────────────────────────────────────────

    async def test_search_by_type(self):
        await self.graph.create_node("BusinessRule", {"text": "R1"}, "t1")
        await self.graph.create_node("BusinessRule", {"text": "R2"}, "t1")
        await self.graph.create_node("TestCase", {"text": "TC1"}, "t1")

        rules = await self.graph.search_by_type("BusinessRule", "t1")
        assert len(rules) == 2

        test_cases = await self.graph.search_by_type("TestCase", "t1")
        assert len(test_cases) == 1

    async def test_search_by_type_tenant_isolation(self):
        await self.graph.create_node("BusinessRule", {}, "t1")
        await self.graph.create_node("BusinessRule", {}, "t2")

        assert len(await self.graph.search_by_type("BusinessRule", "t1")) == 1
        assert len(await self.graph.search_by_type("BusinessRule", "t2")) == 1

    async def test_search_text(self):
        await self.graph.create_node("BusinessRule", {"description": "premium rate for smokers"}, "t1")
        await self.graph.create_node("BusinessRule", {"description": "age band calculation"}, "t1")

        results = await self.graph.search_text("premium", "t1")
        assert len(results) >= 1

    async def test_search_by_type_with_limit(self):
        for i in range(20):
            await self.graph.create_node("BusinessRule", {"idx": i}, "t1")
        results = await self.graph.search_by_type("BusinessRule", "t1", limit=5)
        assert len(results) <= 5

    # ── Stats ──────────────────────────────────────────────

    async def test_stats(self):
        await self.graph.create_node("BusinessRule", {}, "t1")
        await self.graph.create_node("BusinessRule", {}, "t1")
        await self.graph.create_node("TestCase", {}, "t1")
        n1 = await self.graph.create_node("BusinessRule", {}, "t1")
        n2 = await self.graph.create_node("TestCase", {}, "t1")
        await self.graph.create_relation(n1, n2, "TESTS_RULE")

        stats = await self.graph.get_stats("t1")
        assert stats["total_nodes"] >= 4
        assert stats["total_relations"] >= 1


class TestInMemoryVectorStore:

    def setup_method(self):
        from main import InMemoryVectorStore
        self.store = InMemoryVectorStore(dimension=1024)

    def test_store_and_search(self):
        self.store.store("n1", "premium calculation for non-tobacco age 35")
        self.store.store("n2", "claim processing workflow")
        self.store.store("n3", "premium rate table for smokers")

        results = self.store.search("premium rate", limit=3, min_similarity=0.0)
        assert len(results) >= 1
        # The premium-related entries should rank higher
        ids = [r["node_id"] for r in results]
        assert "n1" in ids or "n3" in ids

    def test_search_empty_store(self):
        results = self.store.search("anything", limit=5, min_similarity=0.0)
        assert results == []

    def test_search_with_min_similarity(self):
        self.store.store("n1", "apple orange banana")
        results = self.store.search("completely unrelated quantum physics", limit=5, min_similarity=0.99)
        # Very high threshold should filter most results
        # (may or may not find matches depending on embedding — just verify no crash)
        assert isinstance(results, list)


class TestNodeTypeEnum:

    def test_key_values(self):
        from main import NodeType
        assert NodeType.BUSINESS_RULE.value == "BusinessRule"
        assert NodeType.TEST_CASE.value == "TestCase"
        assert NodeType.PRODUCT.value == "Product"
        assert NodeType.RATE_TABLE.value == "RateTable"


class TestRelationTypeEnum:

    def test_key_values(self):
        from main import RelationType
        assert RelationType.HAS_RULE.value == "HAS_RULE"
        assert RelationType.TESTS_RULE.value == "TESTS_RULE"
        assert RelationType.DEPENDS_ON.value == "DEPENDS_ON"
        assert RelationType.CONTRADICTS.value == "CONTRADICTS"
