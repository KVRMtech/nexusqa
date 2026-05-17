"""
Auth Service — Redis-Backed Brute-Force Protection.

Replaces the in-memory brute-force tracker with a distributed
Redis implementation that works across multiple auth-service pods.

Uses Redis INCR + EXPIRE for atomic counting per email/IP:
  - Key: nexus:bruteforce:{email}
  - Value: count of failed attempts
  - TTL: LOGIN_WINDOW_SECONDS (auto-cleanup)

If Redis is unavailable, falls back to in-memory tracking.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from fastapi import HTTPException

logger = logging.getLogger(__name__)

MAX_LOGIN_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 300  # 5 minutes

# Lua script for atomic check-and-increment:
# Returns the current count AFTER incrementing.
# Sets expiry on first attempt only (so window is from first failure).
_BRUTE_FORCE_LUA = """
local key = KEYS[1]
local window = tonumber(ARGV[1])
local limit = tonumber(ARGV[2])

local count = redis.call('GET', key)
if count and tonumber(count) >= limit then
    local ttl = redis.call('TTL', key)
    return {0, ttl}
end

local current = redis.call('INCR', key)
if current == 1 then
    redis.call('EXPIRE', key, window)
end
return {1, 0}
"""


class RedisBruteForceGuard:
    """Distributed brute-force protection using Redis."""

    def __init__(
        self,
        redis_host: str = "localhost",
        redis_port: int = 6379,
        redis_password: str = "",
        redis_db: int = 4,
        max_attempts: int = MAX_LOGIN_ATTEMPTS,
        window_seconds: int = LOGIN_WINDOW_SECONDS,
        key_prefix: str = "nexus:bruteforce",
    ):
        self._host = redis_host
        self._port = redis_port
        self._password = redis_password
        self._db = redis_db
        self._max_attempts = max_attempts
        self._window = window_seconds
        self._prefix = key_prefix
        self._redis = None
        self._script_sha: Optional[str] = None
        self._connected = False
        # In-memory fallback
        self._fallback: dict[str, list[float]] = {}

    async def connect(self) -> bool:
        """Connect to Redis."""
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
            self._script_sha = await self._redis.script_load(_BRUTE_FORCE_LUA)
            self._connected = True
            logger.info("BruteForceGuard: connected to Redis")
            return True
        except Exception as e:
            logger.warning("BruteForceGuard: Redis unavailable (%s), using in-memory", e)
            self._connected = False
            return False

    async def close(self):
        if self._redis:
            await self._redis.aclose()
            self._redis = None
            self._connected = False

    async def check(self, identifier: str) -> None:
        """
        Check if login should be allowed for this identifier (email or IP).
        Raises HTTPException 429 if too many attempts.
        """
        if self._connected and self._redis:
            await self._check_redis(identifier)
        else:
            self._check_memory(identifier)

    async def record_failure(self, identifier: str) -> None:
        """Record a failed login attempt.

        NOTE: When Redis is connected, check() already atomically increments
        the counter via the Lua script. This method only increments for the
        in-memory fallback to avoid double-counting.
        """
        if not self._connected or not self._redis:
            self._record_failure_memory(identifier)

    async def clear(self, identifier: str) -> None:
        """Clear brute-force counter after successful login."""
        if self._connected and self._redis:
            try:
                await self._redis.delete(f"{self._prefix}:{identifier}")
            except Exception:
                pass
        self._fallback.pop(identifier, None)

    # ── Redis implementation ───────────────────────────────

    async def _check_redis(self, identifier: str) -> None:
        key = f"{self._prefix}:{identifier}"
        try:
            result = await self._redis.evalsha(
                self._script_sha,
                1,
                key,
                str(self._window),
                str(self._max_attempts),
            )
            allowed, ttl = int(result[0]), int(result[1])
            if not allowed:
                retry_after = max(ttl, 60)
                logger.warning(
                    "auth.brute_force_blocked",
                    extra={"identifier": identifier, "retry_after": retry_after},
                )
                raise HTTPException(
                    status_code=429,
                    detail=f"Too many failed attempts. Try again in {retry_after // 60} minutes.",
                    headers={"Retry-After": str(retry_after)},
                )
        except HTTPException:
            raise
        except Exception as e:
            if "NOSCRIPT" in str(e):
                try:
                    self._script_sha = await self._redis.script_load(
                        _BRUTE_FORCE_LUA
                    )
                    await self._check_redis(identifier)
                    return
                except Exception:
                    pass
            # Fall back to in-memory
            self._check_memory(identifier)

    # ── In-memory fallback ─────────────────────────────────

    def _check_memory(self, identifier: str) -> None:
        now = time.monotonic()
        attempts = self._fallback.get(identifier, [])
        attempts = [t for t in attempts if now - t < self._window]
        self._fallback[identifier] = attempts
        if len(attempts) >= self._max_attempts:
            raise HTTPException(
                status_code=429,
                detail=f"Too many failed attempts. Try again in {self._window // 60} minutes.",
            )

    def _record_failure_memory(self, identifier: str) -> None:
        if identifier not in self._fallback:
            self._fallback[identifier] = []
        self._fallback[identifier].append(time.monotonic())
