"""
Azure OpenAI LLM Provider — Enterprise Azure deployments.

Configuration:
  LLM_PROVIDER=azure
  LLM_API_KEY=<azure-key>
  AZURE_OPENAI_ENDPOINT=https://<resource>.openai.azure.com
  AZURE_OPENAI_DEPLOYMENT=gpt-4o
  AZURE_OPENAI_API_VERSION=2024-06-01
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from nexus_sdk.llm.base import LLMProvider, LLMResponse
from nexus_sdk.llm.config import LLMConfig

logger = logging.getLogger(__name__)


class AzureOpenAIProvider(LLMProvider):
    """Azure OpenAI Service provider."""

    def __init__(self, config: LLMConfig):
        super().__init__(config)
        self._client: Optional[httpx.AsyncClient] = None
        self._deployment: str = ""
        self._api_version: str = ""

    async def initialize(self) -> None:
        if not self._config.api_key:
            raise ValueError("LLM_API_KEY is required for Azure OpenAI")
        if not self._config.azure_endpoint:
            raise ValueError("AZURE_OPENAI_ENDPOINT is required")
        if not self._config.azure_deployment:
            raise ValueError("AZURE_OPENAI_DEPLOYMENT is required")

        self._deployment = self._config.azure_deployment
        self._api_version = self._config.azure_api_version or "2024-06-01"

        self._client = httpx.AsyncClient(
            base_url=self._config.azure_endpoint.rstrip("/"),
            timeout=float(self._config.timeout),
            headers={
                "api-key": self._config.api_key,
                "Content-Type": "application/json",
            },
        )

        # Verify connectivity with a lightweight call
        try:
            url = f"/openai/deployments/{self._deployment}/chat/completions?api-version={self._api_version}"
            test_payload = {
                "messages": [{"role": "user", "content": "test"}],
                "max_tokens": 1,
            }
            resp = await self._client.post(url, json=test_payload)
            if resp.status_code in (200, 400):
                # 400 might mean model doesn't support test params, but connection works
                self._initialized = True
            elif resp.status_code == 401:
                raise ValueError("Invalid Azure OpenAI API key")
            elif resp.status_code == 404:
                raise ValueError(f"Azure deployment '{self._deployment}' not found")
            else:
                self._initialized = True  # Optimistically proceed
        except httpx.ConnectError as e:
            raise ConnectionError(
                f"Cannot connect to Azure OpenAI at {self._config.azure_endpoint}: {e}"
            )

        logger.info(
            "azure_openai.initialized",
            extra={
                "endpoint": self._config.azure_endpoint,
                "deployment": self._deployment,
                "api_version": self._api_version,
            },
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
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temp,
            "max_tokens": max_tok,
        }
        if use_json:
            payload["response_format"] = {"type": "json_object"}

        url = (
            f"/openai/deployments/{self._deployment}/chat/completions"
            f"?api-version={self._api_version}"
        )
        resp = await self._client.post(url, json=payload)

        if resp.status_code == 429:
            raise RuntimeError("Azure OpenAI rate limit exceeded — will retry")
        if resp.status_code != 200:
            raise RuntimeError(f"Azure OpenAI error (status={resp.status_code}): {resp.text[:500]}")

        data = resp.json()
        choice = data.get("choices", [{}])[0]
        content = choice.get("message", {}).get("content", "")
        usage = data.get("usage", {})

        return LLMResponse(
            content=content,
            model=data.get("model", self._deployment),
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
        logger.info("azure_openai.shutdown")

    async def health(self) -> dict[str, Any]:
        if not self._client:
            return {
                "healthy": False,
                "provider": "azure",
                "deployment": self._deployment,
                "error": "not initialized",
            }
        return {
            "healthy": self._initialized,
            "provider": "azure",
            "deployment": self._deployment,
            "endpoint": self._config.azure_endpoint,
        }
