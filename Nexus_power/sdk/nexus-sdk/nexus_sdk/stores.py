"""
Nexus SDK — Redis-backed Job Store for engine state persistence.

All engines use jobs (transcription, analysis, execution, generation).
This module provides a Redis-backed store that replaces in-memory dicts,
so job state survives engine restarts.

Degraded in-memory fallback is opt-in only via
``NEXUS_ALLOW_DEGRADED_MODE=true``.

Usage in any engine:
    from nexus_sdk.stores import JobStore
    store = JobStore(prefix="ears", redis_host="localhost")
    await store.connect()
    await store.set_job("job-123", {"status": "processing", ...})
    job = await store.get_job("job-123")
    all_jobs = await store.list_jobs()
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

from nexus_sdk.config import production_guard

logger = logging.getLogger(__name__)


class JobStore:
    """Redis-backed job store with opt-in in-memory fallback."""

    def __init__(
        self,
        prefix: str = "engine",
        redis_host: str = "localhost",
        redis_port: int = 6379,
        redis_password: str = "",
        redis_db: int = 2,
        ttl: int = 86400 * 7,  # 7 days default TTL for jobs
    ):
        self._prefix = prefix
        self._host = redis_host
        self._port = redis_port
        self._password = redis_password
        self._db = redis_db
        self._ttl = ttl
        self._redis = None
        self._connected = False
        self._fallback: dict[str, dict] = {}

    def _key(self, job_id: str) -> str:
        return f"nexus:jobs:{self._prefix}:{job_id}"

    def _index_key(self) -> str:
        return f"nexus:jobs:{self._prefix}:_index"

    async def connect(self) -> bool:
        """Connect to Redis. Returns True if successful."""
        try:
            import redis.asyncio as aioredis
            self._redis = aioredis.Redis(
                host=self._host,
                port=self._port,
                password=self._password or None,
                db=self._db,
                decode_responses=True,
            )
            await self._redis.ping()
            self._connected = True
            logger.info("JobStore[%s]: Redis connected at %s:%d/db%d",
                        self._prefix, self._host, self._port, self._db)
            return True
        except ImportError:
            logger.warning("JobStore[%s]: redis package not installed — using in-memory", self._prefix)
            self._connected = False
        except Exception as exc:
            logger.warning("JobStore[%s]: Redis unavailable (%s) — using in-memory", self._prefix, exc)
            self._redis = None
            self._connected = False
        production_guard(
            f"Redis job store ({self._prefix})",
            available=self._connected,
        )
        return False

    async def set_job(self, job_id: str, data: dict, ttl: Optional[int] = None) -> None:
        """Store or update a job."""
        if self._connected and self._redis:
            try:
                payload = json.dumps(data, default=str)
                await self._redis.setex(self._key(job_id), ttl or self._ttl, payload)
                await self._redis.sadd(self._index_key(), job_id)
                return
            except Exception as e:
                logger.warning("JobStore[%s]: Redis write failed: %s", self._prefix, e)
        self._fallback[job_id] = data

    async def get_job(self, job_id: str) -> Optional[dict]:
        """Retrieve a job by ID."""
        if self._connected and self._redis:
            try:
                raw = await self._redis.get(self._key(job_id))
                if raw:
                    return json.loads(raw)
            except Exception:
                pass
        return self._fallback.get(job_id)

    async def update_job(self, job_id: str, **fields) -> Optional[dict]:
        """Update specific fields in a job."""
        job = await self.get_job(job_id)
        if job is None:
            return None
        job.update(fields)
        await self.set_job(job_id, job)
        return job

    async def delete_job(self, job_id: str) -> bool:
        """Delete a job."""
        if self._connected and self._redis:
            try:
                await self._redis.delete(self._key(job_id))
                await self._redis.srem(self._index_key(), job_id)
                return True
            except Exception:
                pass
        self._fallback.pop(job_id, None)
        return True

    async def list_jobs(self, limit: int = 100) -> list[dict]:
        """List all jobs (most recent first by created_at)."""
        jobs = []
        if self._connected and self._redis:
            try:
                job_ids = await self._redis.smembers(self._index_key())
                for jid in list(job_ids)[:limit * 2]:  # over-fetch to handle expired
                    raw = await self._redis.get(self._key(jid))
                    if raw:
                        job = json.loads(raw)
                        job.setdefault("job_id", jid)
                        jobs.append(job)
                    else:
                        await self._redis.srem(self._index_key(), jid)
                jobs.sort(key=lambda j: j.get("created_at", ""), reverse=True)
                return jobs[:limit]
            except Exception:
                pass
        # Fallback
        for jid, data in list(self._fallback.items())[-limit:]:
            data.setdefault("job_id", jid)
            jobs.append(data)
        jobs.sort(key=lambda j: j.get("created_at", ""), reverse=True)
        return jobs[:limit]

    async def count(self) -> int:
        """Count active jobs."""
        if self._connected and self._redis:
            try:
                return await self._redis.scard(self._index_key())
            except Exception:
                pass
        return len(self._fallback)

    async def close(self) -> None:
        """Close Redis connection."""
        if self._redis:
            await self._redis.aclose()
            self._redis = None
            self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def mode(self) -> str:
        return "redis" if self._connected else "in-memory"
