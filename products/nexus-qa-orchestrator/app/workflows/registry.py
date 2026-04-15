"""
Nexus Chain Registry — Stores and manages chain definitions.

Chain definitions live in two tiers:
    1. System-level (tenant_id="") — Built-in chains, available to all tenants
    2. Tenant-level — Custom chains created by a specific tenant

Storage: Redis-backed with in-memory fallback.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from .schema import ChainDefinition

logger = logging.getLogger(__name__)


class ChainRegistry:
    """
    Redis-backed registry for chain definitions.
    Falls back to in-memory storage when Redis is unavailable.
    """

    REDIS_KEY = "chain:definitions"

    def __init__(self):
        self._redis = None
        self._mem: dict[str, ChainDefinition] = {}

    async def connect(self, redis_url: str):
        try:
            import redis.asyncio as aioredis

            self._redis = aioredis.from_url(redis_url, decode_responses=True)
            await self._redis.ping()
            logger.info("ChainRegistry connected to Redis")
        except Exception as exc:
            logger.warning(
                "Redis unavailable for ChainRegistry — in-memory fallback: %s",
                exc,
            )
            self._redis = None

    # ── CRUD ───────────────────────────────────────────────────

    async def register(self, chain: ChainDefinition):
        """Register or update a chain definition."""
        if self._redis:
            try:
                await self._redis.hset(
                    self.REDIS_KEY, chain.chain_id, chain.model_dump_json()
                )
                logger.info("Registered chain: %s v%s", chain.chain_id, chain.version)
                return
            except Exception as exc:
                logger.error("Redis register failed: %s", exc)
        self._mem[chain.chain_id] = chain

    async def get(self, chain_id: str) -> Optional[ChainDefinition]:
        """Get a chain definition by ID."""
        if self._redis:
            try:
                raw = await self._redis.hget(self.REDIS_KEY, chain_id)
                if raw:
                    return ChainDefinition.model_validate_json(raw)
            except Exception as exc:
                logger.error("Redis get chain failed: %s", exc)
        return self._mem.get(chain_id)

    async def delete(self, chain_id: str) -> bool:
        """Delete a chain definition. Returns True if it existed."""
        deleted = False
        if self._redis:
            try:
                removed = await self._redis.hdel(self.REDIS_KEY, chain_id)
                deleted = removed > 0
            except Exception as exc:
                logger.error("Redis delete failed: %s", exc)
        mem_removed = self._mem.pop(chain_id, None) is not None
        return deleted or mem_removed

    async def list_chains(
        self, tenant_id: Optional[str] = None
    ) -> list[ChainDefinition]:
        """
        List all chains visible to a tenant.

        System-level chains (tenant_id="") are always included.
        If tenant_id is specified, tenant-specific chains are also included.
        """
        all_chains = await self._all()
        if tenant_id is None:
            return all_chains
        return [
            c
            for c in all_chains
            if c.tenant_id == "" or c.tenant_id == tenant_id
        ]

    async def _all(self) -> list[ChainDefinition]:
        if self._redis:
            try:
                raw_map = await self._redis.hgetall(self.REDIS_KEY)
                return [
                    ChainDefinition.model_validate_json(v)
                    for v in raw_map.values()
                ]
            except Exception as exc:
                logger.error("Redis list_all failed: %s", exc)
        return list(self._mem.values())

    # ── Bulk Registration ──────────────────────────────────────

    async def register_builtins(self, chains: list[ChainDefinition]):
        """Register multiple built-in chain definitions at startup."""
        for chain in chains:
            await self.register(chain)
        logger.info("Registered %d built-in chains", len(chains))

    # ── Validation ─────────────────────────────────────────────

    @staticmethod
    def validate_chain(chain: ChainDefinition) -> list[str]:
        """
        Validate a chain definition for structural correctness.
        Returns a list of error messages (empty = valid).
        """
        errors: list[str] = []

        # Unique stage IDs
        stage_ids = [s.stage_id for s in chain.stages]
        if len(stage_ids) != len(set(stage_ids)):
            seen = set()
            for sid in stage_ids:
                if sid in seen:
                    errors.append(f"Duplicate stage_id: '{sid}'")
                seen.add(sid)

        # All depends_on references exist
        id_set = set(stage_ids)
        for stage in chain.stages:
            for dep in stage.depends_on:
                if dep not in id_set:
                    errors.append(
                        f"Stage '{stage.stage_id}' depends on unknown "
                        f"stage '{dep}'"
                    )

        # Circular dependency detection (Kahn's algorithm)
        from collections import defaultdict

        in_degree: dict[str, int] = {sid: 0 for sid in stage_ids}
        dependents: dict[str, list[str]] = defaultdict(list)
        for stage in chain.stages:
            for dep in stage.depends_on:
                if dep in id_set:
                    in_degree[stage.stage_id] += 1
                    dependents[dep].append(stage.stage_id)

        queue = [sid for sid, deg in in_degree.items() if deg == 0]
        visited = 0
        while queue:
            visited += len(queue)
            next_q: list[str] = []
            for sid in queue:
                for dep_id in dependents.get(sid, []):
                    in_degree[dep_id] -= 1
                    if in_degree[dep_id] == 0:
                        next_q.append(dep_id)
            queue = next_q

        if visited != len(stage_ids):
            errors.append("Circular dependency detected in stage graph")

        # for_each stages must reference the item key somewhere in their
        # mappings — input_mapping, file_mappings, or headers_mapping.
        for stage in chain.stages:
            if stage.for_each and stage.for_each_item_key:
                key_ref = f"$temp.{stage.for_each_item_key}"
                all_mappings_str = (
                    json.dumps(stage.input_mapping)
                    + json.dumps(stage.file_mappings)
                    + json.dumps(stage.headers_mapping)
                )
                if key_ref not in all_mappings_str:
                    errors.append(
                        f"Stage '{stage.stage_id}' has for_each but "
                        f"none of its mappings reference {key_ref}"
                    )

        return errors
