"""
Backbone Engine — Milvus Vector Store.

Production vector store backed by Milvus 2.x with pymilvus.
Supports IVF_FLAT indexing with COSINE metric and sentence-transformer
embeddings.  Falls back to hash-based embeddings when the model is
not available.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class MilvusVectorStore:
    """
    Production vector store backed by Milvus 2.x.

    Uses pymilvus to connect to the Milvus server and stores
    embeddings with metadata for semantic search.
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 19530,
        collection_name: str = "nexus_knowledge",
        dimension: int = 384,
    ):
        self.host = host
        self.port = port
        self.collection_name = collection_name
        self.dimension = dimension
        self._collection = None
        self._model = None
        self._model_name = "all-MiniLM-L6-v2"
        self._use_real_embeddings = False
        self._connected = False

    async def connect(self) -> None:
        """Connect to Milvus and ensure the collection exists."""
        from pymilvus import (  # type: ignore[import-not-found]
            connections,
            Collection,
            FieldSchema,
            CollectionSchema,
            DataType,
            utility,
        )

        connections.connect(alias="default", host=self.host, port=self.port)

        if not utility.has_collection(self.collection_name):
            fields = [
                FieldSchema(
                    name="id",
                    dtype=DataType.VARCHAR,
                    is_primary=True,
                    max_length=128,
                ),
                FieldSchema(
                    name="embedding",
                    dtype=DataType.FLOAT_VECTOR,
                    dim=self.dimension,
                ),
                FieldSchema(
                    name="text", dtype=DataType.VARCHAR, max_length=8192
                ),
                FieldSchema(
                    name="node_type", dtype=DataType.VARCHAR, max_length=64
                ),
                FieldSchema(
                    name="tenant_id", dtype=DataType.VARCHAR, max_length=64
                ),
            ]
            schema = CollectionSchema(
                fields, description="Nexus QA knowledge vectors"
            )
            self._collection = Collection(self.collection_name, schema)
            index_params = {
                "metric_type": "COSINE",
                "index_type": "IVF_FLAT",
                "params": {"nlist": 128},
            }
            self._collection.create_index("embedding", index_params)
        else:
            self._collection = Collection(self.collection_name)

        self._collection.load()
        self._connected = True
        logger.info(
            "backbone: connected to Milvus at %s:%d (collection=%s)",
            self.host,
            self.port,
            self.collection_name,
        )

    async def load_model(self) -> None:
        """Load sentence-transformers model for embeddings."""
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore[import-not-found]

            self._model = SentenceTransformer(self._model_name)
            self.dimension = self._model.get_sentence_embedding_dimension()
            self._use_real_embeddings = True
            logger.info(
                "backbone: Milvus using '%s' embeddings (dim=%d)",
                self._model_name,
                self.dimension,
            )
        except (ImportError, Exception) as exc:
            logger.warning(
                "backbone: sentence-transformers not available for Milvus (%s)",
                exc,
            )
            self._use_real_embeddings = False

    def _embed(self, text: str) -> list[float]:
        """Generate embedding vector."""
        if self._use_real_embeddings and self._model is not None:
            embedding = self._model.encode(text, normalize_embeddings=True)
            return embedding.tolist()
        hash_bytes = hashlib.sha512(text.encode()).digest()
        return [
            (hash_bytes[i % len(hash_bytes)] / 255.0) * 2 - 1
            for i in range(self.dimension)
        ]

    def store(self, node_id: str, text: str, metadata: dict = None) -> None:
        """Insert or upsert a vector into Milvus."""
        if not self._collection:
            return
        meta = metadata or {}
        data = [
            [node_id],
            [self._embed(text)],
            [text[:8192]],
            [meta.get("node_type", "unknown")[:64]],
            [meta.get("tenant_id", "default")[:64]],
        ]
        self._collection.upsert(data)

    def search(
        self,
        query: str,
        limit: int = 10,
        min_similarity: float = 0.0,
    ) -> list[dict]:
        """Semantic similarity search via Milvus."""
        if not self._collection:
            return []
        query_vec = self._embed(query)
        results = self._collection.search(
            data=[query_vec],
            anns_field="embedding",
            param={"metric_type": "COSINE", "params": {"nprobe": 16}},
            limit=limit,
            output_fields=["text", "node_type", "tenant_id"],
        )
        hits = []
        for hit in results[0]:
            similarity = hit.score
            if similarity >= min_similarity:
                hits.append({
                    "node_id": hit.id,
                    "similarity": round(similarity, 4),
                    "metadata": {
                        "text": hit.entity.get("text", ""),
                        "node_type": hit.entity.get("node_type", ""),
                        "tenant_id": hit.entity.get("tenant_id", ""),
                    },
                })
        return hits

    async def health_check(self) -> str:
        """Check Milvus connectivity."""
        try:
            from pymilvus import utility  # type: ignore[import-not-found]

            if utility.has_collection(self.collection_name):
                return "ok"
            return "warning: collection missing"
        except Exception as e:
            return f"error: {e}"

    async def close(self) -> None:
        """Release collection and disconnect."""
        try:
            if self._collection:
                self._collection.release()
            from pymilvus import connections  # type: ignore[import-not-found]

            connections.disconnect("default")
            self._connected = False
        except Exception:
            pass
