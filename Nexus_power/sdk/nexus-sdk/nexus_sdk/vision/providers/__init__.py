"""Concrete vision provider implementations.

Each provider is registered in :mod:`nexus_sdk.vision.registry` so the
router can instantiate the right class given a :class:`VisionTierSpec`.
"""

from .anthropic import AnthropicVisionProvider
from .ollama import OllamaVisionProvider
from .openai import OpenAIVisionProvider

__all__ = [
    "AnthropicVisionProvider",
    "OllamaVisionProvider",
    "OpenAIVisionProvider",
]
