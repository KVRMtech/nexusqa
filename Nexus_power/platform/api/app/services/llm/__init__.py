"""LLM abstraction — provider-agnostic, tiered routing.

This package decouples Nexus services from any specific LLM provider.
Application code calls ``LLMRouter.complete(task=..., request=...)``;
the router maps the task name to a configured tier (``tier_fast``,
``tier_balanced``, ``tier_premium`` or any custom tier), the tier maps
to a configured provider (Ollama, OpenAI-compatible, Anthropic, ...),
and the provider executes the call.

Why three tiers?

* ``tier_fast``     — cheapest/fastest model.  Used for high-volume
  low-complexity tasks like 5-word caption rewriting.  Typical:
  Ollama local, GPT-4o-mini, Claude Haiku, Gemini Flash.
* ``tier_balanced`` — mid-quality.  Used for moderate-complexity
  rewriting (story generation, summaries).  Typical: GPT-4o,
  Claude Sonnet, Gemini Pro.
* ``tier_premium``  — hard reasoning over structured data (BPMN
  refinement, knowledge graph queries).  Typical: Claude Opus,
  GPT-5, o3.

Per-tier fallback chain: every tier can declare a ``fallback`` tier so
a transient provider outage degrades gracefully.  If every tier
configured for a task is unavailable, the calling service must handle
the empty result (no fabrication — return weak quality + log).

Configuration is entirely env-driven.  See ``config.py`` for the env
contract.  Code never imports a specific provider by name; the router
loads providers dynamically from the config.

Public API:

* ``LLMRouter`` — main entry point
* ``CompletionRequest`` / ``CompletionResponse`` — call shapes
* ``LLMConfig`` / ``LLMTierConfig`` — config types (returned by load_config)
* ``load_config()`` — read env and return immutable config
* ``LLMError`` — base exception
"""

from .config import (
    LLMConfig,
    LLMConfigError,
    LLMTierConfig,
    load_config,
)
from .providers import (
    AnthropicProvider,
    LLMError,
    LLMProvider,
    LLMProviderError,
    OllamaProvider,
    OpenAICompatProvider,
    build_provider,
)
from .router import LLMRouter, LLMRouterError, NoLLMConfiguredError
from .types import (
    CompletionRequest,
    CompletionResponse,
    FinishReason,
    ImageContent,
    ToolCall,
    ToolDefinition,
)

__all__ = [
    "AnthropicProvider",
    "CompletionRequest",
    "CompletionResponse",
    "FinishReason",
    "ImageContent",
    "LLMConfig",
    "LLMConfigError",
    "LLMError",
    "LLMProvider",
    "LLMProviderError",
    "LLMRouter",
    "LLMRouterError",
    "LLMTierConfig",
    "NoLLMConfiguredError",
    "OllamaProvider",
    "OpenAICompatProvider",
    "ToolCall",
    "ToolDefinition",
    "build_provider",
    "load_config",
]
