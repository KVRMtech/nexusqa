"""
Backbone Engine — In-Memory Graph Store.

Development/test fallback used when Neo4j is not available.
Same interface as ``Neo4jGraphStore`` for transparent swap.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


class InMemoryGraphStore:
    """
    In-memory graph store for development.
    Used when Neo4j is not available.
    """

    def __init__(self):
        self.nodes: dict[str, dict] = {}
        self.relations: list[dict] = []

    async def create_node(
        self,
        node_type: str,
        properties: dict,
        tenant_id: str,
        source: Optional[dict] = None,
        tags: list[str] = None,
    ) -> str:
        """Create a node. Returns node_id."""
        node_id = str(uuid.uuid4())
        self.nodes[node_id] = {
            "node_id": node_id,
            "node_type": node_type,
            "properties": properties,
            "tenant_id": tenant_id,
            "source": source,
            "tags": tags or [],
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        return node_id

    async def get_node(self, node_id: str) -> Optional[dict]:
        return self.nodes.get(node_id)

    async def create_relation(
        self,
        from_id: str,
        to_id: str,
        relation_type: str,
        properties: dict = None,
    ) -> bool:
        if from_id not in self.nodes or to_id not in self.nodes:
            return False
        self.relations.append({
            "from": from_id,
            "to": to_id,
            "type": relation_type,
            "properties": properties or {},
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        return True

    def get_neighbors(
        self,
        node_id: str,
        relation_type: Optional[str] = None,
    ) -> list[dict]:
        """Get all nodes connected to a given node."""
        neighbors = []
        for rel in self.relations:
            target_id = None
            if rel["from"] == node_id:
                target_id = rel["to"]
            elif rel["to"] == node_id:
                target_id = rel["from"]

            if target_id and (relation_type is None or rel["type"] == relation_type):
                node = self.nodes.get(target_id)
                if node:
                    neighbors.append({**node, "relation": rel})

        return neighbors

    async def search_by_type(
        self,
        node_type: str,
        tenant_id: str,
        limit: int = 50,
    ) -> list[dict]:
        results = []
        for node in self.nodes.values():
            if node["node_type"] == node_type and node["tenant_id"] == tenant_id:
                results.append(node)
                if len(results) >= limit:
                    break
        return results

    async def search_text(
        self,
        query: str,
        tenant_id: str,
        node_types: list[str] = None,
        limit: int = 10,
    ) -> list[dict]:
        """Simple text search across node properties."""
        query_lower = query.lower()
        results = []

        for node in self.nodes.values():
            if node["tenant_id"] != tenant_id:
                continue
            if node_types and node["node_type"] not in node_types:
                continue

            props_text = json.dumps(node["properties"]).lower()
            if query_lower in props_text:
                score = len(query_lower) / max(len(props_text), 1)
                results.append({
                    "node_id": node["node_id"],
                    "node_type": node["node_type"],
                    "properties": node["properties"],
                    "similarity": min(score * 10, 0.99),
                    "source": node.get("source"),
                })

        results.sort(key=lambda r: r["similarity"], reverse=True)
        return results[:limit]

    async def get_stats(self, tenant_id: str) -> dict:
        """Get graph statistics for a tenant."""
        tenant_nodes = [
            n for n in self.nodes.values() if n["tenant_id"] == tenant_id
        ]
        tenant_rels = [
            r
            for r in self.relations
            if self.nodes.get(r["from"], {}).get("tenant_id") == tenant_id
        ]

        type_counts: dict[str, int] = {}
        for n in tenant_nodes:
            t = n["node_type"]
            type_counts[t] = type_counts.get(t, 0) + 1

        return {
            "total_nodes": len(tenant_nodes),
            "total_relations": len(tenant_rels),
            "node_types": type_counts,
        }
