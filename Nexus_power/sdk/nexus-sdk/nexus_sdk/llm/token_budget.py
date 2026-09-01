"""
Per-tenant LLM token budget — Redis token bucket.

Each (tenant, tier) pair gets a daily token allowance. Each successful
LLM call decrements by `prompt_tokens + completion_tokens`. When a
tenant runs out, the router falls through to the next cheaper tier
(or rejects the request if no fallback is available).

Why daily, not per-minute:
  - LLM cost is the financial pain point, not rate. Per-minute
    smoothing matters for queue health (that's the workflow-plane
    rate limiter's job); per-day caps matter for budgeting.
  - "We bill monthly; cap us daily" is the request pattern from
    customers we've talked to.

Defaults:
  Tier 1 (premium cloud)   — 1,000,000 tokens/day
  Tier 2 (cloud fallback)  —   500,000 tokens/day
  Tier 3 (on-prem)         — unlimited (no cost)

Operators override via env (per-tenant overrides via a `tenant_budgets`
hash in Redis — Phase 16's tenant admin can write to it):

  LLM_TOKEN_BUDGET_TIER1_DEFAULT=1000000
  LLM_TOKEN_BUDGET_TIER2_DEFAULT=500000
  LLM_TOKEN_BUDGET_TIER3_DEFAULT=0   # 0 means unlimited

Per-tenant key shape (writable by tenant admin):
  nexus:llm_budget:<tenant_id>:<tier>  → hash {limit, tokens, updated}

Fail-open semantics: if Redis is unreachable, the budget check
returns ALLOWED. This matches the existing TenantRateLimiter
behavior (admission.py): never block requests due to infrastructure
issues — but log loudly so SRE sees it.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


# ─── Default per-tier daily allowances ──────────────────────────


def _default_daily_tokens(tier_value: str) -> int:
    """Per-tier default; env overrides apply if set."""
    defaults = {
        "tier1": 1_000_000,
        "tier2":   500_000,
        "tier3":         0,   # 0 = unlimited
    }
    env_key = f"LLM_TOKEN_BUDGET_{tier_value.upper()}_DEFAULT"
    raw = os.environ.get(env_key, "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            logger.warning(
                "llm.token_budget.env_parse_failed",
                extra={"env_key": env_key, "value": raw},
            )
    return defaults.get(tier_value, 0)


@dataclass
class BudgetCheckResult:
    allowed: bool
    tier_value: str
    tokens_remaining: int
    daily_limit: int
    retry_after_seconds: float = 0.0   # time until tomorrow's refill
    reason: str = ""                   # explanation for the caller / logs


class TenantTokenBudget:
    """Redis-backed daily token bucket.

    One hash per (tenant_id, tier):
        nexus:llm_budget:<tenant>:<tier> = {
            limit:    <int, daily allowance>,
            tokens:   <float, remaining today>,
            updated:  <float, epoch of last refill>,
        }

    Refill: full daily limit at the start of each UTC day. Mid-day
    requests check `updated`; if the day has rolled over since then,
    `tokens` resets to `limit` before the deduction.

    Fail-open when Redis is unreachable — matches the workflow-plane
    rate limiter's behavior and stops a Redis outage from blocking
    every LLM call.
    """

    KEY_PREFIX = "nexus:llm_budget"
    DAY_SECONDS = 86_400

    def __init__(self, redis_client=None) -> None:
        self._r = redis_client

    @classmethod
    def _key(cls, tenant_id: str, tier_value: str) -> str:
        return f"{cls.KEY_PREFIX}:{tier_value}:{tenant_id}"

    async def check(
        self,
        tenant_id: str,
        tier_value: str,
        estimated_tokens: int,
    ) -> BudgetCheckResult:
        """Will an `estimated_tokens` charge fit in this tenant's
        budget for this tier? Returns the result without deducting.

        The caller deducts via `consume()` AFTER the request succeeds
        — this prevents one-shot crashes from burning budget.
        """
        return await self._check_or_consume(
            tenant_id, tier_value, estimated_tokens, consume=False,
        )

    async def consume(
        self,
        tenant_id: str,
        tier_value: str,
        tokens: int,
    ) -> BudgetCheckResult:
        """Atomically decrement the budget by `tokens`. Called after a
        successful LLM response so we know the exact charge."""
        return await self._check_or_consume(
            tenant_id, tier_value, tokens, consume=True,
        )

    async def _check_or_consume(
        self,
        tenant_id: str,
        tier_value: str,
        tokens: int,
        consume: bool,
    ) -> BudgetCheckResult:
        if not tenant_id:
            # No tenant context → no budget enforcement. This is for
            # platform-internal calls (sweepers, admin tools).
            return BudgetCheckResult(
                allowed=True, tier_value=tier_value,
                tokens_remaining=-1, daily_limit=-1,
                reason="no_tenant_context",
            )

        daily_limit = _default_daily_tokens(tier_value)

        if daily_limit == 0:
            # 0 = unlimited (default for tier3 / on-prem).
            return BudgetCheckResult(
                allowed=True, tier_value=tier_value,
                tokens_remaining=-1, daily_limit=0,
                reason="unlimited_tier",
            )

        if self._r is None:
            # Fail-open. Caller is responsible for fronting the LLM
            # call without a budget guarantee; SRE sees this in logs.
            logger.warning(
                "llm.token_budget.no_redis_client",
                extra={"tenant_id": tenant_id, "tier": tier_value},
            )
            return BudgetCheckResult(
                allowed=True, tier_value=tier_value,
                tokens_remaining=daily_limit, daily_limit=daily_limit,
                reason="redis_unavailable_fail_open",
            )

        now = time.time()
        key = self._key(tenant_id, tier_value)

        try:
            raw = await self._r.hgetall(key)
        except Exception as e:
            logger.warning(
                "llm.token_budget.redis_get_failed",
                extra={"tenant_id": tenant_id, "err": str(e)},
            )
            return BudgetCheckResult(
                allowed=True, tier_value=tier_value,
                tokens_remaining=daily_limit, daily_limit=daily_limit,
                reason="redis_get_fail_open",
            )

        # Per-tenant override (set by the tenant admin in the
        # tenants table, replicated into Redis on tenant provision).
        per_tenant_limit = int(raw.get("limit", 0) or 0)
        effective_limit = per_tenant_limit if per_tenant_limit > 0 else daily_limit

        remaining = float(raw.get("tokens", effective_limit))
        updated = float(raw.get("updated", now))

        # Daily roll-over: if the UTC day has changed since `updated`,
        # the bucket refills to full.
        if _is_new_utc_day(updated, now):
            remaining = float(effective_limit)

        # check mode: refuse if not enough tokens; never modify state.
        if not consume:
            if tokens > remaining:
                retry_after = _seconds_to_next_utc_midnight(now)
                return BudgetCheckResult(
                    allowed=False, tier_value=tier_value,
                    tokens_remaining=int(remaining),
                    daily_limit=effective_limit,
                    retry_after_seconds=retry_after,
                    reason="budget_exhausted",
                )
            return BudgetCheckResult(
                allowed=True, tier_value=tier_value,
                tokens_remaining=int(remaining),
                daily_limit=effective_limit,
                reason="ok",
            )

        # consume mode: ALWAYS deduct, even if it dips below zero. The
        # check() call before the request was the budget gate; if the
        # caller proceeded past that check and used more than estimated,
        # the deduction must still reflect reality. Future check()s
        # will see a negative balance and refuse new requests.
        remaining -= tokens
        try:
            await self._r.hset(key, mapping={
                "limit": effective_limit,
                "tokens": remaining,
                "updated": now,
            })
            # 48h TTL — survives a UTC-day boundary plus padding.
            await self._r.expire(key, 2 * self.DAY_SECONDS)
        except Exception as e:
            logger.warning(
                "llm.token_budget.redis_set_failed",
                extra={"tenant_id": tenant_id, "err": str(e)},
            )

        return BudgetCheckResult(
            allowed=True, tier_value=tier_value,
            tokens_remaining=int(remaining),
            daily_limit=effective_limit,
            reason="consumed",
        )

    async def get_status(
        self, tenant_id: str, tier_value: str,
    ) -> BudgetCheckResult:
        """Read-only — used by admin endpoints to surface a tenant's
        current budget without touching it."""
        return await self.check(tenant_id, tier_value, estimated_tokens=0)

    async def set_tenant_limit(
        self, tenant_id: str, tier_value: str, limit: int,
    ) -> None:
        """Override the default daily allowance for one tenant. Set
        from the tenant admin endpoint when a customer negotiates a
        bigger (or smaller) per-day cap."""
        if self._r is None:
            return
        key = self._key(tenant_id, tier_value)
        await self._r.hset(key, mapping={"limit": int(limit)})
        await self._r.expire(key, 2 * self.DAY_SECONDS)
        logger.info(
            "llm.token_budget.tenant_limit_set",
            extra={
                "tenant_id": tenant_id, "tier": tier_value,
                "daily_limit": limit,
            },
        )


# ─── Helpers ───────────────────────────────────────────────────


def _is_new_utc_day(prev_epoch: float, now_epoch: float) -> bool:
    """True if now_epoch is in a later UTC day than prev_epoch."""
    if now_epoch <= prev_epoch:
        return False
    prev_day = int(prev_epoch // 86_400)
    now_day = int(now_epoch // 86_400)
    return now_day > prev_day


def _seconds_to_next_utc_midnight(now_epoch: float) -> float:
    seconds_into_today = now_epoch % 86_400
    return 86_400 - seconds_into_today


# ─── Exception type the router catches ─────────────────────────


class TokenBudgetExhausted(Exception):
    """Raised when the tier router cannot dispatch because no tier
    has remaining budget. Distinct from a tier-failover RuntimeError
    so admission-level callers can treat it as a 429-style rate-limit
    response instead of a 5xx."""

    def __init__(self, tenant_id: str, tried_tiers: list[str], retry_after_seconds: float = 0.0):
        super().__init__(
            f"All LLM tiers exhausted budget for tenant {tenant_id!r}; "
            f"tried tiers: {tried_tiers}"
        )
        self.tenant_id = tenant_id
        self.tried_tiers = tried_tiers
        self.retry_after_seconds = retry_after_seconds


__all__ = [
    "TenantTokenBudget",
    "BudgetCheckResult",
    "TokenBudgetExhausted",
]
