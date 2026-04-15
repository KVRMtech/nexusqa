"""
LLM Provider base class — Abstract interface all providers implement.

Every provider must implement:
  - initialize()  : Set up HTTP client / load model
  - generate()    : Send prompt and get response
  - shutdown()    : Clean up resources
  - health()      : Check if provider is operational
"""

from __future__ import annotations

import abc
import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class LLMResponse:
    """Standardized response from any LLM provider."""

    content: str
    """The generated text content."""

    model: str = ""
    """The model that generated this response."""

    provider: str = ""
    """Which provider served this request."""

    prompt_tokens: int = 0
    """Number of tokens in the prompt."""

    completion_tokens: int = 0
    """Number of tokens in the completion."""

    total_tokens: int = 0
    """Total tokens used (prompt + completion)."""

    latency_ms: float = 0.0
    """End-to-end latency in milliseconds."""

    finish_reason: str = ""
    """Why generation stopped: 'stop', 'length', 'content_filter', etc."""

    raw_response: Optional[dict[str, Any]] = field(default=None, repr=False)
    """Raw provider response for debugging (not serialized by default)."""

    def is_truncated(self) -> bool:
        """Check if the response was cut short due to token limit."""
        return self.finish_reason == "length"


class LLMProvider(abc.ABC):
    """
    Abstract base class for all LLM providers.

    Subclasses implement provider-specific initialization, generation, and cleanup.
    The base class provides retry logic, latency tracking, and structured responses.
    """

    def __init__(self, config: Any):
        from nexus_sdk.llm.config import LLMConfig

        self._config: LLMConfig = config
        self._initialized: bool = False
        self._total_requests: int = 0
        self._total_tokens: int = 0
        self._total_errors: int = 0

    @property
    def provider_name(self) -> str:
        """Human-readable provider identifier."""
        return self.__class__.__name__.replace("Provider", "").lower()

    @property
    def is_initialized(self) -> bool:
        return self._initialized

    # ─── Abstract methods ─────────────────────────────────────

    @abc.abstractmethod
    async def initialize(self) -> None:
        """
        Initialize the provider (set up HTTP clients, verify connectivity).
        Called once at startup.
        """
        ...

    @abc.abstractmethod
    async def _do_generate(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        json_mode: Optional[bool] = None,
    ) -> LLMResponse:
        """
        Provider-specific generation implementation.
        Subclasses implement this — the base class wraps it with retry + metrics.
        """
        ...

    @abc.abstractmethod
    async def shutdown(self) -> None:
        """Clean up resources (close HTTP clients, release GPU memory, etc.)."""
        ...

    @abc.abstractmethod
    async def health(self) -> dict[str, Any]:
        """
        Check if the provider is operational.
        Returns dict with at least {"healthy": bool, "provider": str, "model": str}.
        """
        ...

    # ─── Public API with retry + metrics ──────────────────────

    async def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        json_mode: Optional[bool] = None,
    ) -> LLMResponse:
        """
        Generate a response from the LLM with automatic retry and latency tracking.

        Args:
            system_prompt: System/instruction prompt.
            user_prompt: User message/query.
            temperature: Override config temperature (optional).
            max_tokens: Override config max_tokens (optional).
            json_mode: Override config json_mode (optional).

        Returns:
            LLMResponse with generated content and metadata.

        Raises:
            RuntimeError: If provider is not initialized.
            Exception: After all retries are exhausted.
        """
        if not self._initialized:
            raise RuntimeError(
                f"LLM provider '{self.provider_name}' is not initialized. "
                "Call await provider.initialize() first."
            )

        last_error: Optional[Exception] = None
        retries = self._config.max_retries

        for attempt in range(1, retries + 1):
            try:
                start = time.monotonic()
                response = await self._do_generate(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    json_mode=json_mode,
                )
                elapsed_ms = (time.monotonic() - start) * 1000.0

                response.latency_ms = elapsed_ms
                response.provider = self.provider_name
                if not response.model:
                    response.model = self._config.get_effective_model()

                self._total_requests += 1
                self._total_tokens += response.total_tokens

                logger.debug(
                    "llm.generate.success",
                    extra={
                        "provider": self.provider_name,
                        "model": response.model,
                        "latency_ms": round(elapsed_ms, 1),
                        "tokens": response.total_tokens,
                        "attempt": attempt,
                    },
                )
                return response

            except Exception as e:
                last_error = e
                self._total_errors += 1
                logger.warning(
                    "llm.generate.retry",
                    extra={
                        "provider": self.provider_name,
                        "attempt": attempt,
                        "max_retries": retries,
                        "error": str(e),
                    },
                )
                if attempt < retries:
                    delay = self._config.retry_delay * (2 ** (attempt - 1))
                    await asyncio.sleep(delay)

        raise RuntimeError(
            f"LLM generation failed after {retries} attempts "
            f"(provider={self.provider_name}): {last_error}"
        ) from last_error

    def get_stats(self) -> dict[str, Any]:
        """Return provider usage statistics."""
        return {
            "provider": self.provider_name,
            "model": self._config.get_effective_model(),
            "initialized": self._initialized,
            "total_requests": self._total_requests,
            "total_tokens": self._total_tokens,
            "total_errors": self._total_errors,
        }
