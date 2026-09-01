"""
Custom Provider — Connect to any OpenAI-compatible HTTP endpoint.

Useful for:
  - Self-hosted models behind a reverse proxy
  - LiteLLM, LocalAI, LM Studio, text-generation-inference
  - Corporate API gateways

Configuration:
  LLM_PROVIDER=custom
  LLM_API_BASE_URL=http://your-server:8080/v1
  LLM_MODEL=your-model-name
  LLM_API_KEY=<optional-key>
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from nexus_sdk.llm.base import LLMProvider, LLMResponse
from nexus_sdk.llm.config import LLMConfig

logger = logging.getLogger(__name__)


class CustomProvider(LLMProvider):
    """OpenAI-compatible custom endpoint provider."""

    def __init__(self, config: LLMConfig):
        super().__init__(config)
        self._client: Optional[httpx.AsyncClient] = None
        self._model: str = ""

    async def initialize(self) -> None:
        base_url = self._config.get_effective_base_url()
        if not base_url:
            raise ValueError(
                "LLM_API_BASE_URL is required for custom provider. "
                "Example: LLM_API_BASE_URL=http://localhost:8080/v1"
            )

        self._model = self._config.get_effective_model()

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._config.api_key:
            headers["Authorization"] = f"Bearer {self._config.api_key}"

        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=float(self._config.timeout),
            headers=headers,
        )

        self._initialized = True
        logger.info(
            "custom.initialized",
            extra={"base_url": base_url, "model": self._model},
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

        if resp.status_code != 200:
            raise RuntimeError(
                f"Custom endpoint error (status={resp.status_code}): {resp.text[:500]}"
            )

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
        logger.info("custom.shutdown")

    async def health(self) -> dict[str, Any]:
        if not self._client:
            return {"healthy": False, "provider": "custom", "model": self._model, "error": "not initialized"}
        try:
            resp = await self._client.get("/models")
            return {
                "healthy": resp.status_code == 200,
                "provider": "custom",
                "model": self._model,
                "base_url": str(self._client.base_url),
            }
        except Exception:
            # Custom endpoints may not have /models — still mark healthy if initialized
            return {
                "healthy": self._initialized,
                "provider": "custom",
                "model": self._model,
                "base_url": str(self._client.base_url),
            }
