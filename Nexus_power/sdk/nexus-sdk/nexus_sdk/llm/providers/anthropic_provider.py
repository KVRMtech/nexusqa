"""
Anthropic Claude LLM Provider — Claude 3.5 Sonnet, Claude 3 Opus, etc.

Configuration:
  LLM_PROVIDER=anthropic
  LLM_API_KEY=sk-ant-...
  LLM_MODEL=claude-sonnet-4-20250514
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from nexus_sdk.llm.base import LLMProvider, LLMResponse
from nexus_sdk.llm.config import LLMConfig

logger = logging.getLogger(__name__)

# Anthropic API version header
ANTHROPIC_API_VERSION = "2023-06-01"


class AnthropicProvider(LLMProvider):
    """Anthropic Messages API provider for Claude models."""

    def __init__(self, config: LLMConfig):
        super().__init__(config)
        self._client: Optional[httpx.AsyncClient] = None
        self._model: str = ""

    async def initialize(self) -> None:
        if not self._config.api_key:
            raise ValueError(
                "LLM_API_KEY is required for Anthropic Claude. "
                "Set LLM_API_KEY=sk-ant-... in your environment."
            )

        base_url = self._config.get_effective_base_url() or "https://api.anthropic.com"
        self._model = self._config.get_effective_model()

        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=float(self._config.timeout),
            headers={
                "x-api-key": self._config.api_key,
                "anthropic-version": ANTHROPIC_API_VERSION,
                "Content-Type": "application/json",
            },
        )

        self._initialized = True
        logger.info(
            "anthropic.initialized",
            extra={"model": self._model, "base_url": base_url},
        )

    async def _do_generate(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        json_mode: Optional[bool] = None,
    ) -> LLMResponse:
        temp = temperature if temperature is not None else self._config.temperature
        max_tok = max_tokens if max_tokens is not None else self._config.max_tokens

        # Anthropic uses a different message format:
        # - system prompt is a top-level field, not a message
        # - only user/assistant messages in the messages array
        payload: dict[str, Any] = {
            "model": self._model,
            "max_tokens": max_tok,
            "temperature": temp,
            "system": system_prompt,
            "messages": [
                {"role": "user", "content": user_prompt},
            ],
        }

        resp = await self._client.post("/v1/messages", json=payload)

        if resp.status_code == 429:
            raise RuntimeError("Anthropic rate limit exceeded — will retry")
        if resp.status_code == 401:
            raise ValueError("Invalid Anthropic API key")
        if resp.status_code != 200:
            raise RuntimeError(f"Anthropic error (status={resp.status_code}): {resp.text[:500]}")

        data = resp.json()

        # Extract text from content blocks
        content_blocks = data.get("content", [])
        content = ""
        for block in content_blocks:
            if block.get("type") == "text":
                content += block.get("text", "")

        usage = data.get("usage", {})

        return LLMResponse(
            content=content,
            model=data.get("model", self._model),
            prompt_tokens=usage.get("input_tokens", 0),
            completion_tokens=usage.get("output_tokens", 0),
            total_tokens=usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
            finish_reason=data.get("stop_reason", ""),
            raw_response=data,
        )

    async def shutdown(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None
        self._initialized = False
        logger.info("anthropic.shutdown")

    async def health(self) -> dict[str, Any]:
        return {
            "healthy": self._initialized and self._client is not None,
            "provider": "anthropic",
            "model": self._model,
        }
