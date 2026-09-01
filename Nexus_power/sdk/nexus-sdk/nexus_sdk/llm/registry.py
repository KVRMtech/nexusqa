"""
LLM Provider Registry — Central registration of all available providers.

This enables dynamic provider discovery and plugin-style extension.
Third-party providers can be registered at runtime before initialization.
"""

from __future__ import annotations

import logging
from typing import Callable, Type

from nexus_sdk.llm.base import LLMProvider

logger = logging.getLogger(__name__)

# Type alias for provider class factories
ProviderFactory = Callable[..., LLMProvider]


class ProviderRegistry:
    """
    Singleton registry of LLM provider implementations.

    Built-in providers are registered at import time.
    Custom providers can be added via register().

    Usage:
        ProviderRegistry.register("my_provider", MyProviderClass)
        provider_cls = ProviderRegistry.get("my_provider")
    """

    _providers: dict[str, Type[LLMProvider]] = {}

    @classmethod
    def register(cls, name: str, provider_class: Type[LLMProvider]) -> None:
        """Register a provider implementation under a name."""
        cls._providers[name.lower()] = provider_class
        logger.debug("llm.registry.registered", extra={"provider": name})

    @classmethod
    def get(cls, name: str) -> Type[LLMProvider]:
        """Get a registered provider class by name."""
        key = name.lower()
        if key not in cls._providers:
            available = ", ".join(sorted(cls._providers.keys()))
            raise ValueError(
                f"Unknown LLM provider '{name}'. "
                f"Available providers: {available}. "
                "Set LLM_PROVIDER to one of these, or register a custom provider."
            )
        return cls._providers[key]

    @classmethod
    def list_providers(cls) -> list[str]:
        """List all registered provider names."""
        return sorted(cls._providers.keys())

    @classmethod
    def is_registered(cls, name: str) -> bool:
        """Check if a provider is registered."""
        return name.lower() in cls._providers


# ─── Register built-in providers at import time ───────────────

def _register_builtins() -> None:
    """Register all built-in provider implementations."""
    from nexus_sdk.llm.providers import OllamaProvider
    from nexus_sdk.llm.providers.openai_provider import OpenAIProvider
    from nexus_sdk.llm.providers.anthropic_provider import AnthropicProvider
    from nexus_sdk.llm.providers.azure_provider import AzureOpenAIProvider
    from nexus_sdk.llm.providers.vllm_provider import VLLMProvider
    from nexus_sdk.llm.providers.custom_provider import CustomProvider
    from nexus_sdk.llm.providers.gemini_provider import GeminiProvider

    ProviderRegistry.register("ollama", OllamaProvider)
    ProviderRegistry.register("openai", OpenAIProvider)
    ProviderRegistry.register("anthropic", AnthropicProvider)
    ProviderRegistry.register("azure", AzureOpenAIProvider)
    ProviderRegistry.register("vllm", VLLMProvider)
    ProviderRegistry.register("custom", CustomProvider)
    ProviderRegistry.register("gemini", GeminiProvider)


_register_builtins()
