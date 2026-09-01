"""
Multi-Tier LLM Provider Router — Automatic failover across provider tiers.

Each engine can define up to 3 tiers of LLM providers:
  - Tier 1 (Primary)  : Best quality — e.g., Claude Opus, GPT-5, Gemini 3 Pro
  - Tier 2 (Fallback) : Good quality — e.g., GPT-4o, Claude Sonnet
  - Tier 3 (Local)    : On-prem backup — e.g., Llama 3.1 70B via Ollama

The router tries Tier 1 first. On failure (timeout, rate limit, error),
it falls back to Tier 2, then Tier 3. Each tier has independent config.

Environment Variables (per engine):
  # Heart engine example:
  HEART_TIER1_PROVIDER=anthropic
  HEART_TIER1_MODEL=claude-opus-4-20250514
  HEART_TIER1_API_KEY=sk-ant-...

  HEART_TIER2_PROVIDER=openai
  HEART_TIER2_MODEL=gpt-4o
  HEART_TIER2_API_KEY=sk-...

  HEART_TIER3_PROVIDER=ollama
  HEART_TIER3_MODEL=llama3.1:70b

  # Or use global fallback:
  LLM_PROVIDER=ollama        (used if no tier-specific config)
  LLM_MODEL=llama3.1:8b

Usage:
    from nexus_sdk.llm.tiered import TieredProviderConfig, TieredLLMRouter

    config = TieredProviderConfig.from_engine("heart")
    router = TieredLLMRouter(config)
    await router.initialize()

    # Automatically tries Tier 1 → Tier 2 → Tier 3
    response = await router.generate(system_prompt="...", user_prompt="...")

    await router.shutdown()
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from nexus_sdk.llm.base import LLMProvider, LLMResponse
from nexus_sdk.llm.config import LLMConfig
from nexus_sdk.llm.factory import create_provider
from nexus_sdk.llm.pii_guard import PIIGuardViolation, enforce as pii_enforce
from nexus_sdk.llm.pricing import cost_usd_for
from nexus_sdk.llm.token_budget import (
    TenantTokenBudget, TokenBudgetExhausted,
)

logger = logging.getLogger(__name__)


class ProviderTier(str, Enum):
    """Provider quality tiers."""
    PRIMARY = "tier1"
    SECONDARY = "tier2"
    LOCAL = "tier3"


@dataclass
class TierConfig:
    """Configuration for a single provider tier."""
    tier: ProviderTier
    provider: str                    # e.g., "anthropic", "openai", "ollama"
    model: str                       # e.g., "claude-opus-4-20250514", "gpt-4o"
    api_key: str = ""
    api_base_url: str = ""
    azure_endpoint: str = ""
    azure_deployment: str = ""
    azure_api_version: str = "2024-06-01"
    ollama_base_url: str = "http://localhost:11434"
    vllm_base_url: str = "http://localhost:8000"
    gemini_base_url: str = "https://generativelanguage.googleapis.com"
    max_retries: int = 2             # Per-tier retries (lower = faster failover)
    timeout: int = 120               # Per-tier timeout
    enabled: bool = True

    def to_llm_config(self) -> LLMConfig:
        """Convert tier config to standard LLMConfig for provider creation."""
        return LLMConfig(
            **{
                "LLM_PROVIDER": self.provider,
                "LLM_MODEL": self.model,
                "LLM_API_KEY": self.api_key,
                "LLM_API_BASE_URL": self.api_base_url,
                "LLM_MAX_RETRIES": str(self.max_retries),
                "LLM_TIMEOUT": str(self.timeout),
                "AZURE_OPENAI_ENDPOINT": self.azure_endpoint,
                "AZURE_OPENAI_DEPLOYMENT": self.azure_deployment,
                "AZURE_OPENAI_API_VERSION": self.azure_api_version,
                "OLLAMA_BASE_URL": self.ollama_base_url,
                "OLLAMA_MODEL": self.model if self.provider == "ollama" else "",
                "VLLM_BASE_URL": self.vllm_base_url,
                "GEMINI_BASE_URL": self.gemini_base_url,
            }
        )


@dataclass
class TieredProviderConfig:
    """
    Multi-tier provider configuration for an engine.

    Each engine gets up to 3 tiers of providers, configured via env vars
    with the engine name as prefix (e.g., HEART_TIER1_PROVIDER=anthropic).
    """
    engine_name: str
    tiers: list[TierConfig] = field(default_factory=list)
    mode: str = "failover"  # "failover" (try in order) or "router" (task-based)

    @classmethod
    def from_engine(cls, engine_name: str) -> TieredProviderConfig:
        """
        Build tier config from environment variables.

        Reads {ENGINE}_TIER{1,2,3}_PROVIDER etc.
        Falls back to global LLM_PROVIDER if no tiers are configured.
        """
        prefix = engine_name.upper().replace("-", "_")
        tiers: list[TierConfig] = []

        for tier_enum in ProviderTier:
            tier_num = tier_enum.value.replace("tier", "")
            tier_prefix = f"{prefix}_TIER{tier_num}"

            provider = os.environ.get(f"{tier_prefix}_PROVIDER", "")
            if not provider:
                continue

            model = os.environ.get(f"{tier_prefix}_MODEL", "")
            api_key = os.environ.get(f"{tier_prefix}_API_KEY", "")
            api_base_url = os.environ.get(f"{tier_prefix}_API_BASE_URL", "")
            azure_endpoint = os.environ.get(f"{tier_prefix}_AZURE_ENDPOINT", "")
            azure_deployment = os.environ.get(f"{tier_prefix}_AZURE_DEPLOYMENT", "")
            azure_api_version = os.environ.get(
                f"{tier_prefix}_AZURE_API_VERSION", "2024-06-01"
            )
            ollama_base_url = os.environ.get(
                f"{tier_prefix}_OLLAMA_BASE_URL",
                os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434"),
            )
            vllm_base_url = os.environ.get(
                f"{tier_prefix}_VLLM_BASE_URL",
                os.environ.get("VLLM_BASE_URL", "http://localhost:8000"),
            )
            gemini_base_url = os.environ.get(
                f"{tier_prefix}_GEMINI_BASE_URL",
                os.environ.get("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com"),
            )
            max_retries = int(os.environ.get(f"{tier_prefix}_MAX_RETRIES", "2"))
            timeout = int(os.environ.get(f"{tier_prefix}_TIMEOUT", "120"))
            enabled = os.environ.get(
                f"{tier_prefix}_ENABLED", "true"
            ).lower() in ("true", "1", "yes")

            tiers.append(TierConfig(
                tier=tier_enum,
                provider=provider,
                model=model,
                api_key=api_key,
                api_base_url=api_base_url,
                azure_endpoint=azure_endpoint,
                azure_deployment=azure_deployment,
                azure_api_version=azure_api_version,
                ollama_base_url=ollama_base_url,
                vllm_base_url=vllm_base_url,
                gemini_base_url=gemini_base_url,
                max_retries=max_retries,
                timeout=timeout,
                enabled=enabled,
            ))

        # Fallback: if no tiers configured, use global LLM_PROVIDER as single tier
        if not tiers:
            global_provider = os.environ.get("LLM_PROVIDER", "ollama")
            global_api_key = os.environ.get("LLM_API_KEY", "")
            # For Ollama, also check OLLAMA_MODEL env var
            if global_provider == "ollama":
                global_model = os.environ.get("LLM_MODEL", "") or os.environ.get("OLLAMA_MODEL", "")
            else:
                global_model = os.environ.get("LLM_MODEL", "")
            tiers.append(TierConfig(
                tier=ProviderTier.LOCAL,
                provider=global_provider,
                model=global_model,
                api_key=global_api_key,
            ))

        config = cls(engine_name=engine_name, tiers=tiers)
        return config

    def get_tier(self, tier: ProviderTier) -> TierConfig | None:
        """Get config for a specific tier."""
        for t in self.tiers:
            if t.tier == tier and t.enabled:
                return t
        return None

    @property
    def active_tiers(self) -> list[TierConfig]:
        """Return only enabled tiers in priority order."""
        return [t for t in self.tiers if t.enabled]

    def describe(self) -> dict[str, Any]:
        """Return human-readable description of tier configuration."""
        return {
            "engine": self.engine_name,
            "mode": self.mode,
            "tiers": [
                {
                    "tier": t.tier.value,
                    "provider": t.provider,
                    "model": t.model,
                    "enabled": t.enabled,
                    "has_api_key": bool(t.api_key),
                }
                for t in self.tiers
            ],
        }


# Default circuit-breaker cooldown in seconds.  When a tier fails,
# it is skipped for this duration before being retried.  Set via
# {ENGINE}_CIRCUIT_BREAKER_COOLDOWN env var.
DEFAULT_CIRCUIT_BREAKER_COOLDOWN_SECONDS: float = 60.0

# Number of consecutive failures before the circuit breaker opens.
DEFAULT_CIRCUIT_BREAKER_FAILURE_THRESHOLD: int = 2


def _env_bool(name: str, default: bool = False) -> bool:
    """Parse a truthy env var; tolerant of 1/0/true/false/yes/no."""
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _classify_tenant(tenant_id: str | None) -> str:
    """Bucket a tenant id into a coarse class so cost metrics keep
    cardinality bounded. Reuses the workflow-metrics convention so the
    same bucketing applies across the platform."""
    try:
        from nexus_sdk.workflows.metrics import classify_tenant
        return classify_tenant(tenant_id or "")
    except Exception:
        return "unknown"


def _estimate_tokens(
    system_prompt: str, user_prompt: str, max_tokens: int | None,
) -> int:
    """Lower-bound estimate for budget pre-check. Real charge is
    applied via `consume()` after the response. The estimate is
    intentionally conservative — over-blocking is preferable to
    blowing past budget.

    Rule of thumb: ~4 chars per token for English. Output guess uses
    max_tokens when provided, otherwise a conservative 512.
    """
    char_total = len(system_prompt or "") + len(user_prompt or "")
    prompt_tokens_est = max(1, char_total // 4)
    output_tokens_est = max_tokens if max_tokens else 512
    return prompt_tokens_est + output_tokens_est


@dataclass
class TierHealth:
    """Health status for a single provider tier."""
    tier: ProviderTier
    provider_name: str
    model: str
    healthy: bool
    latency_ms: float = 0.0
    error: str = ""
    requests: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    last_failure: float = 0.0  # timestamp
    last_success: float = 0.0  # timestamp


class TieredLLMRouter:
    """
    Multi-tier LLM router with automatic failover and circuit-breaker.

    On each generate() call:
      1. Try Tier 1 (primary/best) provider — SKIP if circuit open
      2. On failure → try Tier 2 (secondary) — SKIP if circuit open
      3. On failure → try Tier 3 (local backup)

    Circuit-breaker: After consecutive_failures >= threshold, a tier is
    "open" (skipped) for cooldown_seconds.  After cooldown expires, the
    tier is retried (half-open).  On success it closes; on failure it
    reopens.  This prevents every request from paying the timeout cost
    of an unhealthy tier.

    Tracks per-tier health and exposes diagnostics via get_health().
    """

    def __init__(
        self,
        config: TieredProviderConfig,
        circuit_breaker_cooldown: float | None = None,
        circuit_breaker_threshold: int | None = None,
        token_budget: TenantTokenBudget | None = None,
    ):
        self._config = config
        self._providers: dict[ProviderTier, LLMProvider] = {}
        self._health: dict[ProviderTier, TierHealth] = {}
        self._initialized = False
        self._active_tier: ProviderTier | None = None
        # Fix 5: optional per-tenant token budget. When set, the router
        # checks budget before dispatching to a tier; on exhaustion it
        # falls through to the next tier (typically a cheaper one) or
        # raises TokenBudgetExhausted if all tiers are out of budget.
        self._budget = token_budget

        # Circuit-breaker settings — can be overridden per engine via env var
        prefix = config.engine_name.upper().replace("-", "_")
        self._cb_cooldown = circuit_breaker_cooldown or float(
            os.environ.get(
                f"{prefix}_CIRCUIT_BREAKER_COOLDOWN",
                str(DEFAULT_CIRCUIT_BREAKER_COOLDOWN_SECONDS),
            )
        )
        self._cb_threshold = circuit_breaker_threshold or int(
            os.environ.get(
                f"{prefix}_CIRCUIT_BREAKER_FAILURE_THRESHOLD",
                str(DEFAULT_CIRCUIT_BREAKER_FAILURE_THRESHOLD),
            )
        )

        # Fix 2: fail-fast for the last tier too. When set, the
        # circuit-breaker applies to every tier including the final
        # fallback — protects the system from cascading-timeout failure
        # when the local-Ollama path itself becomes unhealthy. Per-engine
        # override takes precedence over the global LLM_TIER_FAIL_FAST_LAST.
        self._fail_fast_last = _env_bool(
            f"{prefix}_TIER_FAIL_FAST_LAST",
            default=_env_bool("LLM_TIER_FAIL_FAST_LAST", default=False),
        )

        # Prometheus metrics (lazy — registered on first generate() call)
        self._prom_requests = None
        self._prom_latency = None
        self._prom_failovers = None
        # Fix 1: per-tier cost and token counters. Tenant label is
        # populated when caller passes `tenant_id=...` to generate().
        self._prom_cost = None
        self._prom_tokens = None
        self._prom_initialized = False

    def _ensure_prom_metrics(self) -> None:
        """Lazily register Prometheus metrics (metrics may not exist at __init__ time)."""
        if self._prom_initialized:
            return
        self._prom_initialized = True
        try:
            from nexus_sdk.observability.metrics import get_metrics
            m = get_metrics()
            if m:
                self._prom_requests = m.custom_counter(
                    "llm_tier_requests_total",
                    "Total LLM requests per tier/provider",
                    labels=["engine", "tier", "provider", "status"],
                )
                self._prom_latency = m.custom_histogram(
                    "llm_tier_latency_seconds",
                    "LLM tier response latency",
                    labels=["engine", "tier", "provider"],
                    buckets=(0.1, 0.5, 1, 2, 5, 10, 30, 60),
                )
                self._prom_failovers = m.custom_counter(
                    "llm_tier_failover_total",
                    "LLM tier failover events",
                    labels=["engine", "from_tier"],
                )
                # Fix 1: cost + token observability. tenant_class
                # uses the same coarse bucketing the workflow metrics
                # do (enterprise / pilot / internal / unknown) to keep
                # Prometheus cardinality bounded.
                self._prom_cost = m.custom_counter(
                    "nexus_llm_cost_usd_total",
                    "Cumulative LLM list-price spend (USD).",
                    labels=["engine", "tier", "provider", "model", "tenant_class"],
                )
                self._prom_tokens = m.custom_counter(
                    "nexus_llm_tokens_total",
                    "Cumulative LLM tokens by direction.",
                    labels=["engine", "tier", "provider", "model", "direction"],
                )
        except Exception:
            pass  # metrics not available — non-critical

    @property
    def engine_name(self) -> str:
        return self._config.engine_name

    @property
    def active_tier(self) -> ProviderTier | None:
        """The tier that served the most recent successful request."""
        return self._active_tier

    @property
    def is_initialized(self) -> bool:
        return self._initialized

    async def initialize(self) -> None:
        """Initialize all configured tier providers."""
        for tier_cfg in self._config.active_tiers:
            tier = tier_cfg.tier
            try:
                llm_config = tier_cfg.to_llm_config()
                provider = create_provider(llm_config)
                await provider.initialize()
                self._providers[tier] = provider
                self._health[tier] = TierHealth(
                    tier=tier,
                    provider_name=tier_cfg.provider,
                    model=tier_cfg.model or llm_config.get_effective_model(),
                    healthy=True,
                )
                logger.info(
                    "tiered.provider.initialized",
                    extra={
                        "engine": self.engine_name,
                        "tier": tier.value,
                        "provider": tier_cfg.provider,
                        "model": tier_cfg.model or llm_config.get_effective_model(),
                    },
                )
            except Exception as e:
                logger.warning(
                    "tiered.provider.init_failed",
                    extra={
                        "engine": self.engine_name,
                        "tier": tier.value,
                        "provider": tier_cfg.provider,
                        "error": str(e),
                    },
                )
                self._health[tier] = TierHealth(
                    tier=tier,
                    provider_name=tier_cfg.provider,
                    model=tier_cfg.model,
                    healthy=False,
                    error=str(e),
                )

        if not self._providers:
            raise RuntimeError(
                f"No LLM providers could be initialized for engine '{self.engine_name}'. "
                f"Configured tiers: {[t.tier.value for t in self._config.active_tiers]}"
            )

        self._initialized = True
        logger.info(
            "tiered.router.ready",
            extra={
                "engine": self.engine_name,
                "active_tiers": [t.value for t in self._providers.keys()],
            },
        )

    async def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool | None = None,
        preferred_tier: ProviderTier | None = None,
        tenant_id: str | None = None,
    ) -> LLMResponse:
        """
        Generate response with automatic tier failover.

        Tries providers in tier order (1 → 2 → 3). On success, returns
        immediately. On failure, logs warning and tries next tier.

        Args:
            system_prompt: System instruction
            user_prompt: User message
            temperature: Override (optional)
            max_tokens: Override (optional)
            json_mode: Override (optional)
            preferred_tier: Force a specific tier (optional, skips failover)

        Returns:
            LLMResponse with tier metadata

        Raises:
            RuntimeError: If ALL tiers fail
        """
        if not self._initialized:
            raise RuntimeError("TieredLLMRouter not initialized. Call await router.initialize()")

        # If preferred tier is specified, try only that tier
        if preferred_tier and preferred_tier in self._providers:
            return await self._try_tier(
                preferred_tier, system_prompt, user_prompt,
                temperature, max_tokens, json_mode, tenant_id=tenant_id,
            )

        # Failover chain: try each tier in order, respecting circuit-breaker
        errors: list[str] = []
        tier_order = sorted(self._providers.keys(), key=lambda t: t.value)
        now = time.time()
        skipped_all = True  # Track whether ALL tiers were skipped by circuit-breaker
        budget_exhausted_tiers: list[str] = []  # track for retry-after computation
        max_retry_after = 0.0

        for tier in tier_order:
            health = self._health.get(tier)

            # Circuit-breaker: skip tiers that have exceeded the failure
            # threshold and are still within their cooldown window.
            # Fix 2: when LLM_TIER_FAIL_FAST_LAST (or the per-engine
            # override) is true, the last tier is also eligible for
            # circuit-breaking. This stops the cascading-timeout failure
            # mode where every request hangs on a dead Tier 3. Without
            # the flag, behavior is unchanged from the legacy contract.
            is_last_tier = (tier == tier_order[-1])
            circuit_breaker_applies = (not is_last_tier) or self._fail_fast_last
            if health and circuit_breaker_applies:
                if (
                    health.consecutive_failures >= self._cb_threshold
                    and (now - health.last_failure) < self._cb_cooldown
                ):
                    errors.append(
                        f"{tier.value}({health.provider_name}): "
                        f"circuit-breaker open ({health.consecutive_failures} failures, "
                        f"cooldown {self._cb_cooldown}s)"
                    )
                    logger.debug(
                        "tiered.circuit_breaker.skip",
                        extra={
                            "engine": self.engine_name,
                            "tier": tier.value,
                            "is_last_tier": is_last_tier,
                            "consecutive_failures": health.consecutive_failures,
                            "seconds_since_failure": round(now - health.last_failure, 1),
                            "cooldown": self._cb_cooldown,
                        },
                    )
                    continue

            # Fix 5: per-tenant budget check BEFORE dispatch. We can't
            # know the exact token count yet (the prompt-token count is
            # known on response), so use the prompt length as a rough
            # lower bound — better than nothing, much less than the
            # actual cost. The post-call consume() applies the real
            # number.
            if self._budget is not None and tenant_id:
                est_tokens = _estimate_tokens(system_prompt, user_prompt, max_tokens)
                budget_result = await self._budget.check(
                    tenant_id=tenant_id,
                    tier_value=tier.value,
                    estimated_tokens=est_tokens,
                )
                if not budget_result.allowed:
                    budget_exhausted_tiers.append(tier.value)
                    max_retry_after = max(
                        max_retry_after, budget_result.retry_after_seconds,
                    )
                    errors.append(
                        f"{tier.value}({health.provider_name}): "
                        f"tenant budget exhausted (remaining "
                        f"{budget_result.tokens_remaining}/{budget_result.daily_limit})"
                    )
                    logger.info(
                        "tiered.budget.exhausted",
                        extra={
                            "engine": self.engine_name,
                            "tier": tier.value,
                            "tenant_id": tenant_id,
                            "remaining": budget_result.tokens_remaining,
                            "limit": budget_result.daily_limit,
                        },
                    )
                    continue

            skipped_all = False
            try:
                response = await self._try_tier(
                    tier, system_prompt, user_prompt,
                    temperature, max_tokens, json_mode,
                    tenant_id=tenant_id,
                )
                self._active_tier = tier
                # Reset circuit-breaker on success
                if health:
                    health.consecutive_failures = 0
                    health.last_success = now
                return response
            except Exception as e:
                if health:
                    health.failures += 1
                    health.consecutive_failures += 1
                    health.last_failure = time.time()
                    health.healthy = False
                errors.append(f"{tier.value}({self._health[tier].provider_name}): {e}")
                logger.warning(
                    "tiered.failover",
                    extra={
                        "engine": self.engine_name,
                        "failed_tier": tier.value,
                        "error": str(e),
                        "consecutive_failures": health.consecutive_failures if health else 0,
                        "remaining_tiers": [
                            t.value for t in tier_order
                            if t.value > tier.value
                        ],
                    },
                )

        # Fix 5: distinguish "everyone failed" from "everyone is out
        # of budget" — these have different operational meanings.
        # Budget exhaustion is a 429-style condition; provider failure
        # is a 5xx-style condition. The orchestrator's gateway maps
        # these to the right HTTP status.
        if budget_exhausted_tiers and not [
            t for t in errors if "circuit-breaker" not in t
            and "tenant budget" not in t
        ]:
            raise TokenBudgetExhausted(
                tenant_id=tenant_id or "",
                tried_tiers=budget_exhausted_tiers,
                retry_after_seconds=max_retry_after,
            )

        raise RuntimeError(
            f"All LLM tiers failed for engine '{self.engine_name}': "
            + " | ".join(errors)
        )

    async def _try_tier(
        self,
        tier: ProviderTier,
        system_prompt: str,
        user_prompt: str,
        temperature: float | None,
        max_tokens: int | None,
        json_mode: bool | None,
        tenant_id: str | None = None,
    ) -> LLMResponse:
        """Try a single tier provider."""
        self._ensure_prom_metrics()
        provider = self._providers[tier]
        health = self._health[tier]

        # Fix 4: PII guard runs BEFORE the provider call. Tier 3 (or
        # whatever NEXUS_LLM_PII_GUARD_TIERS leaves unguarded) bypasses;
        # the guard only enforces against egress to cloud tiers.
        # `redact` mode rewrites the prompts; `block` mode raises.
        try:
            system_prompt, user_prompt = pii_enforce(
                system_prompt, user_prompt,
                tier_value=tier.value,
                engine_name=self.engine_name,
            )
        except PIIGuardViolation:
            # Let the failover loop treat this like a tier failure so we
            # fall through to the next (presumably on-prem) tier. Bump
            # the same metric a real provider failure would.
            if self._prom_requests:
                self._prom_requests.labels(
                    service=self.engine_name, engine=self.engine_name,
                    tier=tier.value, provider=health.provider_name,
                    status="pii_blocked",
                ).inc()
            raise

        health.requests += 1

        start = time.monotonic()
        try:
            response = await provider.generate(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=temperature,
                max_tokens=max_tokens,
                json_mode=json_mode,
            )
            elapsed_ms = (time.monotonic() - start) * 1000.0
            elapsed_s = elapsed_ms / 1000.0
            health.latency_ms = elapsed_ms
            health.healthy = True

            # Record Prometheus metrics
            if self._prom_requests:
                self._prom_requests.labels(
                    service=self.engine_name, engine=self.engine_name,
                    tier=tier.value, provider=health.provider_name, status="success",
                ).inc()
            if self._prom_latency:
                self._prom_latency.labels(
                    service=self.engine_name, engine=self.engine_name,
                    tier=tier.value, provider=health.provider_name,
                ).observe(elapsed_s)

            # Fix 1: cost + token observability.
            input_tokens = int(getattr(response, "prompt_tokens", 0) or 0)
            output_tokens = int(getattr(response, "completion_tokens", 0) or 0)
            if not input_tokens and not output_tokens:
                # Some providers only fill total_tokens. Assign all to
                # output as a conservative bias (output is the priced
                # direction for most providers).
                output_tokens = int(response.total_tokens or 0)

            if self._prom_tokens:
                for direction, count in (("input", input_tokens), ("output", output_tokens)):
                    if count > 0:
                        self._prom_tokens.labels(
                            service=self.engine_name, engine=self.engine_name,
                            tier=tier.value, provider=health.provider_name,
                            model=response.model or health.model,
                            direction=direction,
                        ).inc(count)

            if self._prom_cost:
                cost = cost_usd_for(
                    provider=health.provider_name,
                    model=response.model or health.model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                )
                if cost > 0:
                    self._prom_cost.labels(
                        service=self.engine_name, engine=self.engine_name,
                        tier=tier.value, provider=health.provider_name,
                        model=response.model or health.model,
                        tenant_class=_classify_tenant(tenant_id),
                    ).inc(cost)

            # Fix 5: consume the actual token count after a successful
            # response. The earlier check() in generate() used an
            # estimate; this is the authoritative deduction.
            if self._budget is not None and tenant_id:
                try:
                    await self._budget.consume(
                        tenant_id=tenant_id,
                        tier_value=tier.value,
                        tokens=input_tokens + output_tokens,
                    )
                except Exception as exc:
                    # A failed budget update must NOT fail the user's
                    # request — that would invert the policy intent
                    # ("budget protects us, not the customer").
                    logger.warning(
                        "tiered.budget.consume_failed",
                        extra={
                            "engine": self.engine_name,
                            "tier": tier.value,
                            "tenant_id": tenant_id,
                            "err": str(exc),
                        },
                    )

            # Annotate response with tier info
            response.provider = f"{tier.value}:{response.provider}"

            logger.debug(
                "tiered.generate.success",
                extra={
                    "engine": self.engine_name,
                    "tier": tier.value,
                    "provider": health.provider_name,
                    "model": response.model,
                    "latency_ms": round(elapsed_ms, 1),
                    "tokens": response.total_tokens,
                },
            )
            return response
        except Exception as e:
            elapsed_ms = (time.monotonic() - start) * 1000.0
            health.latency_ms = elapsed_ms
            # Record failure metric
            if self._prom_requests:
                self._prom_requests.labels(
                    service=self.engine_name, engine=self.engine_name,
                    tier=tier.value, provider=health.provider_name, status="failure",
                ).inc()
            if self._prom_failovers:
                self._prom_failovers.labels(
                    service=self.engine_name, engine=self.engine_name,
                    from_tier=tier.value,
                ).inc()
            raise

    async def shutdown(self) -> None:
        """Shutdown all tier providers."""
        for tier, provider in self._providers.items():
            try:
                await provider.shutdown()
                logger.info(
                    "tiered.provider.shutdown",
                    extra={"engine": self.engine_name, "tier": tier.value},
                )
            except Exception as e:
                logger.warning(
                    "tiered.provider.shutdown_error",
                    extra={
                        "engine": self.engine_name,
                        "tier": tier.value,
                        "error": str(e),
                    },
                )
        self._providers.clear()
        self._initialized = False

    def get_health(self) -> dict[str, Any]:
        """Return health status for all tiers including circuit-breaker state."""
        now = time.time()
        return {
            "engine": self.engine_name,
            "initialized": self._initialized,
            "active_tier": self._active_tier.value if self._active_tier else None,
            "circuit_breaker": {
                "cooldown_seconds": self._cb_cooldown,
                "failure_threshold": self._cb_threshold,
            },
            "tiers": {
                tier.value: {
                    "provider": h.provider_name,
                    "model": h.model,
                    "healthy": h.healthy,
                    "latency_ms": round(h.latency_ms, 1),
                    "requests": h.requests,
                    "failures": h.failures,
                    "consecutive_failures": h.consecutive_failures,
                    "circuit_open": (
                        h.consecutive_failures >= self._cb_threshold
                        and (now - h.last_failure) < self._cb_cooldown
                    ),
                    "error": h.error,
                }
                for tier, h in self._health.items()
            },
        }

    def get_stats(self) -> dict[str, Any]:
        """Return aggregated stats across all tiers."""
        total_requests = sum(h.requests for h in self._health.values())
        total_failures = sum(h.failures for h in self._health.values())
        return {
            "engine": self.engine_name,
            "total_requests": total_requests,
            "total_failures": total_failures,
            "failure_rate": (
                round(total_failures / total_requests, 4) if total_requests else 0.0
            ),
            "tiers_available": len(self._providers),
            "tiers_healthy": sum(1 for h in self._health.values() if h.healthy),
            "active_tier": self._active_tier.value if self._active_tier else None,
        }


# ─── Convenience function ─────────────────────────────────────

async def create_tiered_router(engine_name: str) -> TieredLLMRouter:
    """
    Create and initialize a tiered LLM router for an engine.

    Reads tier config from environment variables:
      {ENGINE}_TIER1_PROVIDER, {ENGINE}_TIER1_MODEL, etc.

    Falls back to global LLM_PROVIDER if no tiers configured.

    Returns an initialized TieredLLMRouter ready for generate() calls.
    """
    config = TieredProviderConfig.from_engine(engine_name)
    router = TieredLLMRouter(config)
    await router.initialize()
    return router
