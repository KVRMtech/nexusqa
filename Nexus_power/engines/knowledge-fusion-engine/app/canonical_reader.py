"""Read-only access to ``canonical_artifacts`` for the indexer.

The indexer is the only path that reads canonical artifact bodies into
the substrate. We fetch via direct SQL with explicit tenant filtering
plus the RLS session variable so both layers agree.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import sqlalchemy as sa

from .db import Database, canonical_artifacts, products

logger = logging.getLogger(__name__)


class CanonicalReader:
    def __init__(self, db: Database):
        self._db = db

    async def fetch_artifact(
        self, *, tenant_id: str, artifact_id: str
    ) -> Optional[dict[str, Any]]:
        """Return the canonical artifact row as a dict, or None."""
        async with self._db.tenant_session(tenant_id) as session:
            row = (
                await session.execute(
                    sa.select(canonical_artifacts).where(
                        canonical_artifacts.c.tenant_id == tenant_id,
                        canonical_artifacts.c.artifact_id == artifact_id,
                    )
                )
            ).mappings().first()
        return dict(row) if row else None

    async def fetch_tenant_products(
        self, *, tenant_id: str
    ) -> list[dict[str, Any]]:
        """Return active products + aliases for product tagging."""
        async with self._db.tenant_session(tenant_id) as session:
            rows = (
                await session.execute(
                    sa.select(
                        products.c.product_id,
                        products.c.name,
                        products.c.slug,
                        products.c.aliases,
                    ).where(
                        products.c.tenant_id == tenant_id,
                        products.c.status == "active",
                    )
                )
            ).mappings().all()
        return [dict(r) for r in rows]
