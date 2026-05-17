"""
In-flight fingerprint dedup.

The legacy completed-artifact cache in spine answers the question
"has this exact content been processed before?" — but it only returns
a hit AFTER processing completes. While the first upload is mid-flight,
N more clients can submit the same content and start N more workflows,
each doing the same 10-minute eyes pipeline.

This module provides the missing layer: a Redis-backed lock keyed by
(tenant_id, content_fingerprint) that holds the workflow_id of the
FIRST workflow processing that content. Subsequent submitters see the
existing workflow_id and join its progress stream instead of starting
a duplicate.

Key shape:
    nexus:fingerprint:inflight:<tenant_id>:<fingerprint>  →  workflow_id

TTL:
    Set to the workflow's deadline_seconds + 5 min grace. If the
    workflow completes or quarantines, the lock is released early.
    If the workflow deadlines out (the orchestrator sweeper hasn't
    woken yet), the TTL expires naturally so the next uploader gets
    a fresh attempt instead of a dead lock.

Fail-safe:
    All Redis operations degrade to a no-op (return None) when Redis
    is unreachable. The worst case is duplicate workflows, not a
    broken upload path.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


_KEY_PREFIX = "nexus:fingerprint:inflight"


class InFlightFingerprintLock:
    """Tenant-scoped fingerprint → workflow_id map with TTL."""

    def __init__(self, redis_client=None) -> None:
        self._r = redis_client

    @staticmethod
    def _key(tenant_id: str, fingerprint: str) -> str:
        return f"{_KEY_PREFIX}:{tenant_id}:{fingerprint}"

    async def try_claim(
        self,
        tenant_id: str,
        fingerprint: str,
        workflow_id: str,
        ttl_seconds: int,
    ) -> Optional[str]:
        """
        Atomically claim the lock with workflow_id. Returns:
          - None on successful claim (caller is the FIRST submitter)
          - the existing workflow_id when another workflow holds the lock

        Implemented as `SET key value NX EX ttl` — the redis-py async
        client returns True on success, None on conflict.
        """
        if self._r is None or not fingerprint:
            return None
        key = self._key(tenant_id, fingerprint)
        try:
            ok = await self._r.set(key, workflow_id, nx=True, ex=ttl_seconds)
        except Exception as e:
            logger.warning("fingerprint_lock.claim_failed err=%s", e)
            return None
        if ok:
            return None
        # Someone else holds the lock — return their workflow_id.
        try:
            existing = await self._r.get(key)
        except Exception as e:
            logger.warning("fingerprint_lock.read_failed err=%s", e)
            return None
        return existing

    async def release(
        self,
        tenant_id: str,
        fingerprint: str,
        workflow_id: str,
    ) -> bool:
        """
        Drop the lock if it still belongs to `workflow_id`. Returns
        True when released. Uses a check-and-delete via Lua to avoid
        racing with a TTL expiry that already let a new claim through.
        """
        if self._r is None or not fingerprint:
            return False
        key = self._key(tenant_id, fingerprint)
        script = """
        if redis.call('GET', KEYS[1]) == ARGV[1] then
            return redis.call('DEL', KEYS[1])
        else
            return 0
        end
        """
        try:
            res = await self._r.eval(script, 1, key, workflow_id)
            return bool(res)
        except Exception as e:
            logger.warning("fingerprint_lock.release_failed err=%s", e)
            return False

    async def peek(
        self, tenant_id: str, fingerprint: str,
    ) -> Optional[str]:
        """Read-only check: return the workflow_id currently holding the
        lock, or None. Used by the admission endpoint before deciding
        whether to claim or to attach."""
        if self._r is None or not fingerprint:
            return None
        try:
            return await self._r.get(self._key(tenant_id, fingerprint))
        except Exception as e:
            logger.warning("fingerprint_lock.peek_failed err=%s", e)
            return None
