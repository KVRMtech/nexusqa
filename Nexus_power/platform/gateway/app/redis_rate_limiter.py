"""
API Gateway — Redis-Backed Distributed Rate Limiter.

Replaces the in-memory sliding-window limiter with an atomic
Redis-based implementation that works across multiple gateway pods.

Uses a Redis Lua script for atomic sliding-window counting:
  - ZADD: add request timestamp to sorted set
  - ZREMRANGEBYSCORE: prune timestamps outside the window
  - ZCARD: count remaining requests
  - EXPIRE: auto-cleanup of idle tenant keys

The Lua script runs atomically on Redis — no race conditions.

Usage:
    limiter = RedisRateLimiter(redis_host="redis", max_per_minute=600)
    await limiter.connect()
    allowed = await limiter.allow("tenant-123")
"""

from __future__ import annotations

import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

# Lua script for atomic sliding-window rate limiting.
# Runs entirely on Redis — single round-trip, no race conditions.
_SLIDING_WINDOW_LUA = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])

-- Remove expired entries
redis.call('ZREMRANGEBYSCORE', key, '-inf', now - window)

-- Count current window
local count = redis.call('ZCARD', key)

if count < limit then
    -- Add this request
    redis.call('ZADD', key, now, now .. '-' .. math.random(1000000))
    redis.call('EXPIRE', key, math.ceil(window) + 10)
    return 1
else
    -- Rate limited
    redis.call('EXPIRE', key, math.ceil(window) + 10)
    return 0
end
"""


class RedisRateLimiter:
    """Distributed rate limiter using Redis sorted sets."""

    def __init__(
        self,
        redis_host: str = "localhost",
        redis_port: int = 6379,
        redis_password: str = "",
        redis_db: int = 4,
        max_per_minute: int = 600,
        window_seconds: float = 60.0,
        key_prefix: str = "nexus:ratelimit",
    ):
        self._host = redis_host
        self._port = redis_port
        self._password = redis_password
        self._db = redis_db
        self.max_per_minute = max_per_minute
        self._window = window_seconds
        self._prefix = key_prefix
        self._redis = None
        self._script_sha: Optional[str] = None
        self._connected = False

    async def connect(self) -> bool:
        """Connect to Redis and pre-load the Lua script."""
        try:
            import redis.asyncio as aioredis

            self._redis = aioredis.Redis(
                host=self._host,
                port=self._port,
                password=self._password or None,
                db=self._db,
                decode_responses=True,
                socket_connect_timeout=5,
                socket_timeout=5,
                retry_on_timeout=True,
            )
            await self._redis.ping()
            # Pre-load Lua script for EVALSHA (avoids sending full script each call)
            self._script_sha = await self._redis.script_load(_SLIDING_WINDOW_LUA)
            self._connected = True
            logger.info("RedisRateLimiter: connected to %s:%d", self._host, self._port)
            return True
        except ImportError:
            logger.warning("RedisRateLimiter: redis package not installed")
            return False
        except Exception as e:
            logger.warning("RedisRateLimiter: connection failed: %s", e)
            self._connected = False
            return False

    async def close(self):
        """Close the Redis connection."""
        if self._redis:
            await self._redis.aclose()
            self._redis = None
            self._connected = False

    async def allow(self, tenant_id: str) -> bool:
        """
        Check if a request from tenant_id should be allowed.

        Returns True if within rate limit, False if exceeded.
        Falls back to allow-all if Redis is unavailable.
        """
        if self.max_per_minute <= 0:
            return True

        if not self._connected or not self._redis:
            # Fail open: allow requests if Redis is down
            return True

        key = f"{self._prefix}:{tenant_id}"
        now = time.time()

        try:
            result = await self._redis.evalsha(
                self._script_sha,
                1,  # number of keys
                key,
                str(now),
                str(self._window),
                str(self.max_per_minute),
            )
            return int(result) == 1
        except Exception as e:
            # Script might have been flushed; reload it
            if "NOSCRIPT" in str(e):
                try:
                    self._script_sha = await self._redis.script_load(
                        _SLIDING_WINDOW_LUA
                    )
                    result = await self._redis.evalsha(
                        self._script_sha,
                        1,
                        key,
                        str(now),
                        str(self._window),
                        str(self.max_per_minute),
                    )
                    return int(result) == 1
                except Exception:
                    pass
            logger.warning("RedisRateLimiter: error for %s: %s", tenant_id, e)
            # Fail open
            return True

    async def get_remaining(self, tenant_id: str) -> int:
        """Get the number of remaining allowed requests for a tenant."""
        if not self._connected or not self._redis:
            return self.max_per_minute

        key = f"{self._prefix}:{tenant_id}"
        now = time.time()
        try:
            # Clean expired entries first
            await self._redis.zremrangebyscore(key, "-inf", now - self._window)
            count = await self._redis.zcard(key)
            return max(0, self.max_per_minute - count)
        except Exception:
            return self.max_per_minute

    @property
    def is_connected(self) -> bool:
        return self._connected
