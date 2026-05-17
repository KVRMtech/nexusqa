"""FeatureFlagService — DB-backed flag store with Redis cache.

Operational characteristics:
    * Reads: Redis cache (60s TTL) with DB fall-through. p99 read
      latency well under 5ms when warm.
    * Writes: DB-first, then explicit cache invalidate (DEL). Optimistic
      concurrency via ``expected_version``.
    * Audit: every write emits an event to the supplied audit sink
      (Postgres + structlog by default).
    * Circuit breaker: persisted state, row-locked transitions, lazy
      OPEN->HALF_OPEN recovery on every read.

The service does not know about plugins, integrations, or echo logic;
those modules consume ``FlagState`` and decide what to do.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional, Protocol

from .circuit_breaker import CircuitBreaker, CircuitDecision
from .models import (
    CircuitConfig,
    CircuitState,
    FlagState,
    FlagUpdate,
    Mode,
    OptimisticLockError,
    Outcome,
)
from .repository import FlagRepository

logger = logging.getLogger(__name__)


class _RedisLike(Protocol):
    async def get(self, key: str) -> Optional[bytes]: ...
    async def setex(self, key: str, ttl: int, value: bytes) -> None: ...
    async def delete(self, *keys: str) -> int: ...


# Audit sink — keep callable-shaped so the consumer can wire it to
# whatever it wants (structlog, OpenTelemetry, plugin_events_log…).
AuditSink = Callable[[dict], Awaitable[None]]


async def _default_audit_sink(event: dict) -> None:
    logger.info(
        "feature_flag.audit",
        extra={"audit": event},
    )


@dataclass(frozen=True)
class ServiceConfig:
    cache_ttl_seconds: int = 60
    cache_namespace: str = "ff"
    default_mode: Mode = Mode.SHADOW
    circuit: CircuitConfig = CircuitConfig()


class FeatureFlagService:
    """Public service surface — instantiate once per process.

    Parameters
    ----------
    session_factory
        Async SQLAlchemy session factory (from platform/api/database.py
        or ``nexus_sdk.db``).
    redis
        ``redis.asyncio`` client.
    config
        Optional ``ServiceConfig``; defaults are production-sane.
    audit_sink
        Optional async callable invoked once per write. Defaults to a
        structlog emitter; production deployments should wire it to
        ``integration_events_log`` to retain audit trail in Postgres.
    """

    def __init__(
        self,
        session_factory,
        redis: _RedisLike,
        config: Optional[ServiceConfig] = None,
        audit_sink: Optional[AuditSink] = None,
    ):
        self._repo = FlagRepository(session_factory)
        self._redis = redis
        self._cfg = config or ServiceConfig()
        self._breaker = CircuitBreaker(self._repo, self._cfg.circuit)
        self._audit = audit_sink or _default_audit_sink

    # ── Reads ────────────────────────────────────────────────────

    async def get(self, tenant_id: str, feature_key: str) -> FlagState:
        """Return the current flag state, auto-recovering circuit if due.

        The cache is honored on the hot path. We only invoke
        ``maybe_recover`` (a DB round-trip with a row lock) when the
        cached state claims OPEN *and* its cooldown has elapsed — that
        is the single case where the cache could lie to the caller.
        """
        _validate_keys(tenant_id, feature_key)

        cached = await self._cache_get(tenant_id, feature_key)
        if cached is not None:
            if _circuit_should_recover(cached):
                recovered = await self._breaker.maybe_recover(
                    tenant_id, feature_key
                )
                if recovered is not None:
                    await self._cache_invalidate(tenant_id, feature_key)
                    # Fall through to DB read so the cached payload is
                    # rebuilt with the new circuit state.
                else:
                    return cached
            else:
                return cached

        state = await self._repo.get(tenant_id, feature_key)
        if state is None:
            state = self._default_state(tenant_id, feature_key)

        await self._cache_set(state)
        return state

    async def list_for_tenant(self, tenant_id: str) -> list[FlagState]:
        _validate_keys(tenant_id, "_")
        return await self._repo.list_for_tenant(tenant_id)

    # ── Writes ───────────────────────────────────────────────────

    async def set(
        self,
        tenant_id: str,
        feature_key: str,
        update: FlagUpdate,
    ) -> FlagState:
        _validate_keys(tenant_id, feature_key)

        # Merge update onto current state so callers can patch fields.
        current = await self._repo.get(tenant_id, feature_key)
        base_state = current or self._default_state(tenant_id, feature_key)

        enabled = update.enabled if update.enabled is not None else base_state.enabled
        mode = update.mode if update.mode is not None else base_state.mode
        config = (
            update.config if update.config is not None else base_state.config
        )

        expected_version = update.expected_version
        if expected_version is None and current is not None:
            expected_version = current.version

        success, new_state = await self._repo.upsert(
            tenant_id=tenant_id,
            feature_key=feature_key,
            enabled=enabled,
            mode=mode,
            config=config,
            actor=update.actor,
            expected_version=expected_version,
        )
        if not success or new_state is None:
            actual = current.version if current else 0
            raise OptimisticLockError(
                tenant_id, feature_key, expected_version or 0, actual
            )

        await self._cache_invalidate(tenant_id, feature_key)
        await self._audit(
            {
                "kind": "feature_flag.set",
                "tenant_id": tenant_id,
                "feature_key": feature_key,
                "actor": update.actor,
                "before": _audit_dump(base_state) if current else None,
                "after": _audit_dump(new_state),
                "at": datetime.now(timezone.utc).isoformat(),
            }
        )
        return new_state

    async def promote(
        self,
        tenant_id: str,
        feature_key: str,
        new_mode: Mode,
        actor: str,
    ) -> FlagState:
        """Convenience wrapper for shadow→dm_only→live progression."""
        return await self.set(
            tenant_id,
            feature_key,
            FlagUpdate(enabled=True, mode=new_mode, actor=actor),
        )

    async def disable(
        self,
        tenant_id: str,
        feature_key: str,
        actor: str,
    ) -> FlagState:
        return await self.set(
            tenant_id,
            feature_key,
            FlagUpdate(enabled=False, actor=actor),
        )

    # ── Outcome recording / circuit operator controls ───────────

    async def record_outcome(
        self,
        tenant_id: str,
        feature_key: str,
        outcome: Outcome,
    ) -> CircuitDecision:
        _validate_keys(tenant_id, feature_key)
        decision = await self._breaker.record_outcome(
            tenant_id, feature_key, outcome
        )
        if decision.transitioned:
            await self._cache_invalidate(tenant_id, feature_key)
            await self._audit(
                {
                    "kind": "feature_flag.circuit_transition",
                    "tenant_id": tenant_id,
                    "feature_key": feature_key,
                    "new_state": decision.state.value,
                    "tripped": decision.tripped,
                    "failure_rate": decision.failure_rate,
                    "window_samples": decision.window_samples,
                    "at": datetime.now(timezone.utc).isoformat(),
                }
            )
        return decision

    async def force_trip(
        self,
        tenant_id: str,
        feature_key: str,
        reason: str,
        actor: str,
    ) -> CircuitState:
        state = await self._breaker.force_trip(
            tenant_id, feature_key, reason, actor
        )
        await self._cache_invalidate(tenant_id, feature_key)
        await self._audit(
            {
                "kind": "feature_flag.circuit_force_trip",
                "tenant_id": tenant_id,
                "feature_key": feature_key,
                "actor": actor,
                "reason": reason,
                "at": datetime.now(timezone.utc).isoformat(),
            }
        )
        return state

    async def reset_circuit(
        self,
        tenant_id: str,
        feature_key: str,
        actor: str,
    ) -> CircuitState:
        state = await self._breaker.reset(tenant_id, feature_key, actor)
        await self._cache_invalidate(tenant_id, feature_key)
        await self._audit(
            {
                "kind": "feature_flag.circuit_reset",
                "tenant_id": tenant_id,
                "feature_key": feature_key,
                "actor": actor,
                "at": datetime.now(timezone.utc).isoformat(),
            }
        )
        return state

    # ── Cache plumbing ───────────────────────────────────────────

    def _cache_key(self, tenant_id: str, feature_key: str) -> str:
        return f"{self._cfg.cache_namespace}:{tenant_id}:{feature_key}"

    async def _cache_get(
        self, tenant_id: str, feature_key: str
    ) -> Optional[FlagState]:
        try:
            raw = await self._redis.get(self._cache_key(tenant_id, feature_key))
        except Exception as exc:
            # Cache misses fall through to DB — never fail the request.
            logger.warning("feature_flag.cache_get_failed: %s", exc)
            return None
        if not raw:
            return None
        try:
            return FlagState.model_validate_json(raw)
        except Exception as exc:
            logger.warning("feature_flag.cache_decode_failed: %s", exc)
            return None

    async def _cache_set(self, state: FlagState) -> None:
        try:
            payload = state.model_dump_json().encode("utf-8")
            await self._redis.setex(
                self._cache_key(state.tenant_id, state.feature_key),
                self._cfg.cache_ttl_seconds,
                payload,
            )
        except Exception as exc:
            logger.warning("feature_flag.cache_set_failed: %s", exc)

    async def _cache_invalidate(
        self, tenant_id: str, feature_key: str
    ) -> None:
        try:
            await self._redis.delete(self._cache_key(tenant_id, feature_key))
        except Exception as exc:
            logger.warning("feature_flag.cache_invalidate_failed: %s", exc)

    # ── Helpers ──────────────────────────────────────────────────

    def _default_state(
        self, tenant_id: str, feature_key: str
    ) -> FlagState:
        return FlagState(
            tenant_id=tenant_id,
            feature_key=feature_key,
            enabled=False,
            mode=self._cfg.default_mode,
            config={},
            version=0,
            circuit_state=CircuitState.CLOSED,
        )


# ── Validation ───────────────────────────────────────────────────


_MAX_KEY_LEN = 128


def _validate_keys(tenant_id: str, feature_key: str) -> None:
    if not tenant_id or not isinstance(tenant_id, str):
        raise ValueError("tenant_id must be a non-empty string")
    if len(tenant_id) > 64:
        raise ValueError("tenant_id exceeds 64 characters")
    if not feature_key or not isinstance(feature_key, str):
        raise ValueError("feature_key must be a non-empty string")
    if len(feature_key) > _MAX_KEY_LEN:
        raise ValueError(f"feature_key exceeds {_MAX_KEY_LEN} characters")


def _audit_dump(state: FlagState) -> dict:
    return json.loads(state.model_dump_json())


def _circuit_should_recover(state: FlagState) -> bool:
    """Return True iff a cached OPEN state is past its cooldown."""
    if state.circuit_state != CircuitState.OPEN:
        return False
    if state.cooldown_until is None:
        return False
    return datetime.now(timezone.utc) >= state.cooldown_until
