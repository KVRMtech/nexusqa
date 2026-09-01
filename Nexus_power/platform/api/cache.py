"""
Redis caching layer for Nexus Platform API.

Caches expensive aggregation queries and engine health checks
with configurable TTL. Falls back gracefully if Redis is unavailable.

Usage:
    from platform.api.cache import RedisCache
    cache = RedisCache()
    await cache.connect()

    # Cache with 60s TTL
    data = await cache.get_or_set("engine_status", fetch_engine_status, ttl=60)
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable, Coroutine, Optional

logger = logging.getLogger(__name__)


class RedisCache:
    """Async Redis cache with graceful degradation."""

    def __init__(self, host: str = "localhost", port: int = 6379, password: str = "", db: int = 1):
        self._host = host
        self._port = port
        self._password = password
        self._db = db
        self._redis = None
        self._connected = False

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
            logger.info("platform-api: Redis cache connected at %s:%d", self._host, self._port)
            return True
        except ImportError:
            logger.warning("platform-api: redis package not installed — caching disabled")
            return False
        except Exception as exc:
            logger.warning("platform-api: Redis unavailable (%s) — caching disabled", exc)
            self._redis = None
            self._connected = False
            return False

    async def get(self, key: str) -> Optional[Any]:
        """Get a cached value. Returns None if not cached or Redis unavailable."""
        if not self._connected or not self._redis:
            return None
        try:
            val = await self._redis.get(f"nexus:cache:{key}")
            if val:
                return json.loads(val)
        except Exception:
            pass
        return None

    async def set(self, key: str, value: Any, ttl: int = 60) -> bool:
        """Set a cached value with TTL in seconds."""
        if not self._connected or not self._redis:
            return False
        try:
            await self._redis.setex(
                f"nexus:cache:{key}",
                ttl,
                json.dumps(value, default=str),
            )
            return True
        except Exception:
            return False

    async def get_or_set(
        self,
        key: str,
        fetch_fn: Callable[[], Coroutine[Any, Any, Any]],
        ttl: int = 60,
    ) -> Any:
        """Get from cache or call fetch_fn and cache the result."""
        cached = await self.get(key)
        if cached is not None:
            return cached
        result = await fetch_fn()
        await self.set(key, result, ttl)
        return result

    async def invalidate(self, key: str) -> bool:
        """Remove a key from cache."""
        if not self._connected or not self._redis:
            return False
        try:
            await self._redis.delete(f"nexus:cache:{key}")
            return True
        except Exception:
            return False

    async def close(self):
        """Close Redis connection."""
        if self._redis:
            await self._redis.aclose()
            self._redis = None
            self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected
