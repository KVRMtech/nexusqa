"""
Nexus LLM Abstraction Layer — Pluggable, config-driven LLM provider system.

Supports on-prem AND cloud providers with zero code changes.
Switch models via environment variables or config file.

Supported providers:
  - ollama    : Local Ollama server (CPU/GPU, recommended for on-prem)
  - vllm      : vLLM server (GPU, high throughput)
  - openai    : OpenAI API (GPT-4o, GPT-4-turbo, etc.)
  - azure     : Azure OpenAI Service
  - anthropic : Anthropic Claude (Claude 3.5 Sonnet, Opus, etc.)
  - gemini    : Google Gemini (Gemini 3 Pro, 2.0 Flash, etc.)
  - custom    : Any OpenAI-compatible HTTP endpoint

Usage:
    from nexus_sdk.llm import LLMProvider, LLMConfig, create_provider

    config = LLMConfig()  # Reads from env vars
    provider = create_provider(config)
    await provider.initialize()
    response = await provider.generate(system_prompt="...", user_prompt="...")
    await provider.shutdown()
"""

from nexus_sdk.llm.config import LLMConfig
from nexus_sdk.llm.base import LLMProvider, LLMResponse
from nexus_sdk.llm.factory import create_provider
from nexus_sdk.llm.registry import ProviderRegistry
from nexus_sdk.llm.tiered import (
    TieredLLMRouter,
    TieredProviderConfig,
    TierConfig,
    ProviderTier,
    create_tiered_router,
)

__all__ = [
    "LLMConfig",
    "LLMProvider",
    "LLMResponse",
    "create_provider",
    "ProviderRegistry",
    # Multi-tier provider system
    "TieredLLMRouter",
    "TieredProviderConfig",
    "TierConfig",
    "ProviderTier",
    "create_tiered_router",
]
