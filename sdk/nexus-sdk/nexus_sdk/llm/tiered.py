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
    last_failure: float = 0.0  # timestamp


class TieredLLMRouter:
    """
    Multi-tier LLM router with automatic failover.

    On each generate() call:
      1. Try Tier 1 (primary/best) provider
      2. On failure → try Tier 2 (secondary)
      3. On failure → try Tier 3 (local backup)

    Tracks per-tier health and exposes diagnostics via get_health().
    """

    def __init__(self, config: TieredProviderConfig):
        self._config = config
        self._providers: dict[ProviderTier, LLMProvider] = {}
        self._health: dict[ProviderTier, TierHealth] = {}
        self._initialized = False
        self._active_tier: ProviderTier | None = None

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
                temperature, max_tokens, json_mode,
            )

        # Failover chain: try each tier in order
        errors: list[str] = []
        tier_order = sorted(self._providers.keys(), key=lambda t: t.value)

        for tier in tier_order:
            try:
                response = await self._try_tier(
                    tier, system_prompt, user_prompt,
                    temperature, max_tokens, json_mode,
                )
                self._active_tier = tier
                return response
            except Exception as e:
                health = self._health.get(tier)
                if health:
                    health.failures += 1
                    health.last_failure = time.time()
                    health.healthy = False
                errors.append(f"{tier.value}({self._health[tier].provider_name}): {e}")
                logger.warning(
                    "tiered.failover",
                    extra={
                        "engine": self.engine_name,
                        "failed_tier": tier.value,
                        "error": str(e),
                        "remaining_tiers": [
                            t.value for t in tier_order
                            if t.value > tier.value
                        ],
                    },
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
    ) -> LLMResponse:
        """Try a single tier provider."""
        provider = self._providers[tier]
        health = self._health[tier]
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
            health.latency_ms = elapsed_ms
            health.healthy = True

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
        """Return health status for all tiers."""
        return {
            "engine": self.engine_name,
            "initialized": self._initialized,
            "active_tier": self._active_tier.value if self._active_tier else None,
            "tiers": {
                tier.value: {
                    "provider": h.provider_name,
                    "model": h.model,
                    "healthy": h.healthy,
                    "latency_ms": round(h.latency_ms, 1),
                    "requests": h.requests,
                    "failures": h.failures,
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
