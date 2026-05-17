"""
Backbone Engine — In-Memory Vector Store.

Development/test fallback with REAL sentence-transformer embeddings.
Uses all-MiniLM-L6-v2 (384 dimensions, ~80 MB) for production-quality
semantic search.  Falls back to hash-based embeddings only if the
model cannot be loaded.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class InMemoryVectorStore:
    """
    In-memory vector store with REAL sentence-transformer embeddings.

    Uses all-MiniLM-L6-v2 (384 dimensions, ~80MB) for production-quality
    semantic search. Falls back to hash-based embeddings only if the
    model cannot be loaded.
    """

    def __init__(self, dimension: int = 384):
        self.dimension = dimension
        self.vectors: dict[str, list[float]] = {}
        self.metadata: dict[str, dict] = {}
        self._model = None
        self._model_name = "all-MiniLM-L6-v2"
        self._use_real_embeddings = False

    async def load_model(self):
        """Load sentence-transformers model for real embeddings."""
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore[import-not-found]

            self._model = SentenceTransformer(self._model_name)
            self.dimension = self._model.get_sentence_embedding_dimension()
            self._use_real_embeddings = True
            logger.info(
                "backbone: loaded sentence-transformers model '%s' (dim=%d)",
                self._model_name,
                self.dimension,
            )
        except ImportError:
            logger.warning(
                "backbone: sentence-transformers not installed — "
                "using hash fallback. pip install sentence-transformers"
            )
            self._use_real_embeddings = False
        except Exception as exc:
            logger.warning(
                "backbone: failed to load embedding model (%s) — using hash fallback",
                exc,
            )
            self._use_real_embeddings = False

    def _embed(self, text: str) -> list[float]:
        """Generate embedding using real model or hash fallback.

        The hash fallback uses word-level additive hashing so that texts
        sharing tokens will produce similar vectors, preserving basic
        bag-of-words semantics.  Each unique lowercased token is hashed
        independently and mapped to a deterministic unit-range vector;
        the document embedding is the L2-normalised sum of its token
        vectors.
        """
        if self._use_real_embeddings and self._model is not None:
            embedding = self._model.encode(text, normalize_embeddings=True)
            return embedding.tolist()

        # ── Word-level additive hash fallback ──────────────────
        import re
        tokens = set(re.findall(r"[a-z0-9]+", text.lower()))
        if not tokens:
            tokens = {"<empty>"}

        embedding = [0.0] * self.dimension
        for token in tokens:
            token_hash = hashlib.sha512(token.encode()).digest()
            for i in range(self.dimension):
                byte_val = token_hash[i % len(token_hash)]
                embedding[i] += (byte_val / 255.0) * 2 - 1

        # L2 normalise so cosine similarity = dot product
        norm = sum(v * v for v in embedding) ** 0.5
        if norm > 0:
            embedding = [v / norm for v in embedding]
        return embedding

    def store(self, node_id: str, text: str, metadata: dict = None):
        """Embed and store text."""
        self.vectors[node_id] = self._embed(text)
        self.metadata[node_id] = metadata or {}

    def search(
        self,
        query: str,
        limit: int = 10,
        min_similarity: float = 0.0,
    ) -> list[dict]:
        """Search for similar vectors using cosine similarity."""
        if not self.vectors:
            return []

        query_vec = self._embed(query)

        results = []
        for node_id, vec in self.vectors.items():
            dot = sum(a * b for a, b in zip(query_vec, vec))
            norm_q = sum(a * a for a in query_vec) ** 0.5
            norm_v = sum(b * b for b in vec) ** 0.5
            similarity = dot / (norm_q * norm_v + 1e-8)

            if similarity >= min_similarity:
                results.append({
                    "node_id": node_id,
                    "similarity": round(similarity, 4),
                    "metadata": self.metadata.get(node_id, {}),
                })

        results.sort(key=lambda r: r["similarity"], reverse=True)
        return results[:limit]
