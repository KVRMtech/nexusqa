"""
Backbone Engine — Neo4j Graph Store.

Production graph store using Neo4j async driver with Cypher queries.
Same interface as ``InMemoryGraphStore`` for transparent swap.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


class Neo4jGraphStore:
    """
    Production graph store using Neo4j async driver.
    Same interface as InMemoryGraphStore for transparent swap.
    """

    def __init__(
        self,
        uri: str,
        user: str,
        password: str,
        database: str = "neo4j",
    ):
        self._uri = uri
        self._user = user
        self._password = password
        self._database = database
        self._driver = None

    async def connect(self) -> None:
        """Connect to Neo4j."""
        from neo4j import AsyncGraphDatabase

        self._driver = AsyncGraphDatabase.driver(
            self._uri, auth=(self._user, self._password)
        )
        async with self._driver.session(database=self._database) as session:
            await session.run("RETURN 1")
        logger.info("backbone: Neo4j connected at %s", self._uri)

    async def close(self) -> None:
        if self._driver:
            await self._driver.close()

    async def create_node(
        self,
        node_type: str,
        properties: dict,
        tenant_id: str,
        source: Optional[dict] = None,
        tags: list[str] = None,
    ) -> str:
        """Create a node in Neo4j."""
        node_id = str(uuid.uuid4())
        props = {
            "node_id": node_id,
            "tenant_id": tenant_id,
            "source_json": json.dumps(source) if source else "{}",
            "tags": tags or [],
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        for k, v in properties.items():
            if isinstance(v, (str, int, float, bool)):
                props[f"p_{k}"] = v
            else:
                props[f"p_{k}"] = json.dumps(v)

        cypher = (
            f"CREATE (n:`{node_type}` $props) "
            "SET n.node_type = $node_type "
            "RETURN n.node_id AS node_id"
        )
        async with self._driver.session(database=self._database) as session:
            result = await session.run(cypher, props=props, node_type=node_type)
            await result.consume()
        return node_id

    async def get_node(self, node_id: str) -> Optional[dict]:
        cypher = "MATCH (n {node_id: $node_id}) RETURN n, labels(n) AS labels"
        async with self._driver.session(database=self._database) as session:
            result = await session.run(cypher, node_id=node_id)
            record = await result.single()
            if not record:
                return None
            node = dict(record["n"])
            properties = {}
            for k, v in node.items():
                if k.startswith("p_"):
                    try:
                        properties[k[2:]] = json.loads(v)
                    except (json.JSONDecodeError, TypeError):
                        properties[k[2:]] = v
            return {
                "node_id": node.get("node_id"),
                "node_type": node.get("node_type"),
                "properties": properties,
                "tenant_id": node.get("tenant_id"),
                "source": json.loads(node.get("source_json", "{}")),
                "tags": node.get("tags", []),
                "created_at": node.get("created_at", ""),
            }

    async def create_relation(
        self,
        from_id: str,
        to_id: str,
        relation_type: str,
        properties: dict = None,
    ) -> bool:
        props = properties or {}
        props["created_at"] = datetime.now(timezone.utc).isoformat()
        cypher = (
            "MATCH (a {node_id: $from_id}), (b {node_id: $to_id}) "
            f"CREATE (a)-[r:`{relation_type}` $props]->(b) "
            "RETURN type(r) AS rtype"
        )
        async with self._driver.session(database=self._database) as session:
            result = await session.run(
                cypher, from_id=from_id, to_id=to_id, props=props
            )
            record = await result.single()
            return record is not None

    async def get_neighbors(
        self,
        node_id: str,
        relation_type: Optional[str] = None,
    ) -> list[dict]:
        if relation_type:
            cypher = (
                f"MATCH (a {{node_id: $nid}})-[r:`{relation_type}`]-(b) "
                "RETURN b, type(r) AS rel_type, properties(r) AS rel_props"
            )
        else:
            cypher = (
                "MATCH (a {node_id: $nid})-[r]-(b) "
                "RETURN b, type(r) AS rel_type, properties(r) AS rel_props"
            )
        results = []
        async with self._driver.session(database=self._database) as session:
            records = await session.run(cypher, nid=node_id)
            async for record in records:
                node_data = dict(record["b"])
                properties = {
                    k[2:]: v for k, v in node_data.items() if k.startswith("p_")
                }
                results.append({
                    "node_id": node_data.get("node_id"),
                    "node_type": node_data.get("node_type"),
                    "properties": properties,
                    "tenant_id": node_data.get("tenant_id"),
                    "relation": {
                        "type": record["rel_type"],
                        "properties": dict(record["rel_props"]),
                    },
                })
        return results

    async def search_by_type(
        self,
        node_type: str,
        tenant_id: str,
        limit: int = 50,
    ) -> list[dict]:
        cypher = (
            "MATCH (n {node_type: $ntype, tenant_id: $tid}) "
            "RETURN n LIMIT $lim"
        )
        results = []
        async with self._driver.session(database=self._database) as session:
            records = await session.run(
                cypher, ntype=node_type, tid=tenant_id, lim=limit
            )
            async for record in records:
                node_data = dict(record["n"])
                properties = {
                    k[2:]: v for k, v in node_data.items() if k.startswith("p_")
                }
                results.append({
                    "node_id": node_data.get("node_id"),
                    "node_type": node_data.get("node_type"),
                    "properties": properties,
                    "tenant_id": node_data.get("tenant_id"),
                })
        return results

    async def search_text(
        self,
        query: str,
        tenant_id: str,
        node_types: list[str] = None,
        limit: int = 10,
    ) -> list[dict]:
        """Full-text search using Neo4j CONTAINS."""
        cypher = (
            "MATCH (n {tenant_id: $tid}) "
            "WHERE any(k IN keys(n) WHERE n[k] IS :: STRING AND n[k] CONTAINS $q) "
            "RETURN n LIMIT $lim"
        )
        results = []
        async with self._driver.session(database=self._database) as session:
            records = await session.run(
                cypher, tid=tenant_id, q=query, lim=limit
            )
            async for record in records:
                node_data = dict(record["n"])
                props = {
                    k[2:]: v for k, v in node_data.items() if k.startswith("p_")
                }
                nt = node_data.get("node_type", "")
                if node_types and nt not in node_types:
                    continue
                results.append({
                    "node_id": node_data.get("node_id"),
                    "node_type": nt,
                    "properties": props,
                    "similarity": 0.75,
                    "source": None,
                })
        return results

    async def get_stats(self, tenant_id: str) -> dict:
        cypher = (
            "MATCH (n {tenant_id: $tid}) "
            "RETURN n.node_type AS ntype, count(n) AS cnt"
        )
        type_counts: dict[str, int] = {}
        total = 0
        async with self._driver.session(database=self._database) as session:
            records = await session.run(cypher, tid=tenant_id)
            async for record in records:
                nt = record["ntype"] or "unknown"
                c = record["cnt"]
                type_counts[nt] = c
                total += c

            rel_result = await session.run(
                "MATCH (a {tenant_id: $tid})-[r]-() RETURN count(r) AS cnt",
                tid=tenant_id,
            )
            rel_record = await rel_result.single()
            total_rels = rel_record["cnt"] if rel_record else 0

        return {
            "total_nodes": total,
            "total_relations": total_rels,
            "node_types": type_counts,
        }

    async def health_check(self) -> str:
        try:
            async with self._driver.session(database=self._database) as session:
                await session.run("RETURN 1")
            return "connected"
        except Exception as e:
            return f"error: {e}"
