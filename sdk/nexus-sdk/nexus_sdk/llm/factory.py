"""
LLM Provider Factory — Create the right provider from config.

This is the main entry point for creating LLM providers.
Call create_provider() with an LLMConfig and it returns
the appropriate initialized provider instance.

Usage:
    from nexus_sdk.llm import create_provider, LLMConfig

    config = LLMConfig()  # Reads from env vars
    provider = create_provider(config)
    await provider.initialize()

    response = await provider.generate(
        system_prompt="You are a test engineer.",
        user_prompt="Generate test cases for login flow.",
    )

    await provider.shutdown()
"""

from __future__ import annotations

import logging

from nexus_sdk.llm.base import LLMProvider
from nexus_sdk.llm.config import LLMConfig
from nexus_sdk.llm.registry import ProviderRegistry

logger = logging.getLogger(__name__)


def create_provider(config: LLMConfig | None = None) -> LLMProvider:
    """
    Create an LLM provider instance from configuration.

    Args:
        config: LLMConfig instance. If None, creates one from env vars.

    Returns:
        An uninitialized LLMProvider instance. Call await provider.initialize().

    Raises:
        ValueError: If the provider is not registered or config is invalid.
    """
    if config is None:
        config = LLMConfig()

    # Validate config for the selected provider
    errors = config.validate_for_provider()
    if errors:
        raise ValueError(
            f"Invalid LLM configuration for provider '{config.provider}': "
            + "; ".join(errors)
        )

    provider_cls = ProviderRegistry.get(config.provider)
    provider = provider_cls(config)

    logger.info(
        "llm.factory.created",
        extra={
            "provider": config.provider,
            "model": config.get_effective_model(),
        },
    )

    return provider
