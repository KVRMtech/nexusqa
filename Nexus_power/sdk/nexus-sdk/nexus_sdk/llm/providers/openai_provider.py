"""
OpenAI LLM Provider — GPT-4o, GPT-4-turbo, o1, o3, etc.

Configuration:
  LLM_PROVIDER=openai
  LLM_API_KEY=sk-...
  LLM_MODEL=gpt-4o
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

import httpx

from nexus_sdk.llm.base import LLMProvider, LLMResponse
from nexus_sdk.llm.config import LLMConfig

logger = logging.getLogger(__name__)


class OpenAIProvider(LLMProvider):
    """OpenAI Chat Completions API provider."""

    def __init__(self, config: LLMConfig):
        super().__init__(config)
        self._client: Optional[httpx.AsyncClient] = None
        self._model: str = ""

    async def initialize(self) -> None:
        if not self._config.api_key:
            raise ValueError(
                "LLM_API_KEY is required for OpenAI provider. "
                "Set LLM_API_KEY=sk-... in your environment."
            )

        base_url = self._config.get_effective_base_url()
        self._model = self._config.get_effective_model()

        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=float(self._config.timeout),
            headers={
                "Authorization": f"Bearer {self._config.api_key}",
                "Content-Type": "application/json",
            },
        )

        # Verify connectivity
        try:
            resp = await self._client.get("/models")
            if resp.status_code == 200:
                self._initialized = True
                logger.info(
                    "openai.initialized",
                    extra={"model": self._model, "base_url": base_url},
                )
            elif resp.status_code == 401:
                raise ValueError("Invalid OpenAI API key")
            else:
                # Some custom OpenAI-compatible servers don't have /models
                # We'll trust it and verify on first generate call
                self._initialized = True
                logger.info(
                    "openai.initialized_without_model_check",
                    extra={"model": self._model, "status": resp.status_code},
                )
        except httpx.ConnectError as e:
            raise ConnectionError(f"Cannot connect to OpenAI API at {base_url}: {e}")

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
        use_json = json_mode if json_mode is not None else self._config.json_mode

        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temp,
            "max_tokens": max_tok,
        }

        if use_json:
            payload["response_format"] = {"type": "json_object"}

        resp = await self._client.post("/chat/completions", json=payload)

        if resp.status_code == 429:
            raise RuntimeError("OpenAI rate limit exceeded — will retry")
        if resp.status_code == 401:
            raise ValueError("Invalid OpenAI API key")
        if resp.status_code != 200:
            raise RuntimeError(f"OpenAI error (status={resp.status_code}): {resp.text[:500]}")

        data = resp.json()
        choice = data.get("choices", [{}])[0]
        content = choice.get("message", {}).get("content", "")
        usage = data.get("usage", {})

        return LLMResponse(
            content=content,
            model=data.get("model", self._model),
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            total_tokens=usage.get("total_tokens", 0),
            finish_reason=choice.get("finish_reason", ""),
            raw_response=data,
        )

    async def shutdown(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None
        self._initialized = False
        logger.info("openai.shutdown")

    async def health(self) -> dict[str, Any]:
        if not self._client:
            return {"healthy": False, "provider": "openai", "model": self._model, "error": "not initialized"}
        try:
            resp = await self._client.get("/models")
            return {
                "healthy": resp.status_code == 200,
                "provider": "openai",
                "model": self._model,
            }
        except Exception as e:
            return {"healthy": False, "provider": "openai", "model": self._model, "error": str(e)}
