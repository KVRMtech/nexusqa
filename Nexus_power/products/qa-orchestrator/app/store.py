"""
QA Orchestrator — Redis-backed Session Store.

Persists KTSession objects and per-session pipeline data to Redis.
Optional in-memory fallback is disabled by default so E2E runs fail fast
when persistence is not actually available.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Optional

from .models import KTSession

logger = logging.getLogger(__name__)


class RedisSessionStore:
    """Redis-backed session store with opt-in in-memory fallback."""

    def __init__(self):
        self._redis = None
        self._mem_sessions: dict[str, KTSession] = {}
        self._mem_data: dict[str, dict] = {}
        self._allow_memory_fallback = (
            os.getenv("ORCHESTRATOR_ALLOW_IN_MEMORY_FALLBACK", "false").lower()
            == "true"
        )

    async def connect(self, redis_url: str) -> None:
        """Connect to Redis. Raise by default when persistence is unavailable."""
        try:
            import redis.asyncio as aioredis
            self._redis = aioredis.from_url(redis_url, decode_responses=True)
            await self._redis.ping()
            logger.info("Orchestrator session store connected to Redis")
        except Exception as exc:
            if not self._allow_memory_fallback:
                raise RuntimeError(
                    "Orchestrator requires Redis for persistent session state. "
                    "Set ORCHESTRATOR_ALLOW_IN_MEMORY_FALLBACK=true only for explicit local dev fallback."
                ) from exc
            logger.warning("Redis unavailable — using in-memory sessions: %s", exc)
            self._redis = None

    # ── Session CRUD ───────────────────────────────────────────

    async def save_session(self, session: KTSession) -> None:
        if self._redis:
            try:
                await self._redis.hset(
                    "orch:sessions", session.session_id, session.model_dump_json()
                )
                return
            except Exception as exc:
                logger.error("Redis save_session failed: %s", exc)
        self._mem_sessions[session.session_id] = session

    async def get_session(self, session_id: str) -> Optional[KTSession]:
        if self._redis:
            try:
                raw = await self._redis.hget("orch:sessions", session_id)
                if raw:
                    return KTSession.model_validate_json(raw)
            except Exception as exc:
                logger.error("Redis get_session failed: %s", exc)
        return self._mem_sessions.get(session_id)

    async def list_sessions(self, tenant_id: str) -> list[KTSession]:
        all_sessions = await self._all_sessions()
        return [s for s in all_sessions if s.tenant_id == tenant_id]

    async def _all_sessions(self) -> list[KTSession]:
        if self._redis:
            try:
                raw_map = await self._redis.hgetall("orch:sessions")
                return [KTSession.model_validate_json(v) for v in raw_map.values()]
            except Exception as exc:
                logger.error("Redis _all_sessions failed: %s", exc)
        return list(self._mem_sessions.values())

    # ── Session Data CRUD ──────────────────────────────────────

    async def save_data(self, session_id: str, data: dict) -> None:
        if self._redis:
            try:
                await self._redis.hset(
                    "orch:data", session_id, json.dumps(data, default=str)
                )
                return
            except Exception as exc:
                logger.error("Redis save_data failed: %s", exc)
        self._mem_data[session_id] = data

    async def get_data(self, session_id: str) -> dict:
        if self._redis:
            try:
                raw = await self._redis.hget("orch:data", session_id)
                if raw:
                    return json.loads(raw)
            except Exception as exc:
                logger.error("Redis get_data failed: %s", exc)
        return self._mem_data.get(session_id, {})

    async def delete_session(self, session_id: str) -> bool:
        """Delete a session and its associated data."""
        deleted = False
        if self._redis:
            try:
                deleted = bool(await self._redis.hdel("orch:sessions", session_id))
                await self._redis.hdel("orch:data", session_id)
                return deleted
            except Exception as exc:
                logger.error("Redis delete_session failed: %s", exc)
        if session_id in self._mem_sessions:
            del self._mem_sessions[session_id]
            self._mem_data.pop(session_id, None)
            return True
        return False
