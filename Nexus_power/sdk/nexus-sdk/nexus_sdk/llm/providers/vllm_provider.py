"""
vLLM Provider — High-throughput GPU inference via vLLM server.

Configuration:
  LLM_PROVIDER=vllm
  VLLM_BASE_URL=http://localhost:8000
  LLM_MODEL=llama-3.1-70b-instruct
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from nexus_sdk.llm.base import LLMProvider, LLMResponse
from nexus_sdk.llm.config import LLMConfig

logger = logging.getLogger(__name__)


class VLLMProvider(LLMProvider):
    """vLLM OpenAI-compatible server provider."""

    def __init__(self, config: LLMConfig):
        super().__init__(config)
        self._client: Optional[httpx.AsyncClient] = None
        self._model: str = ""

    async def initialize(self) -> None:
        base_url = self._config.vllm_base_url or "http://localhost:8000"
        self._model = self._config.get_effective_model()

        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=float(self._config.timeout),
        )

        # vLLM exposes an OpenAI-compatible /v1/models endpoint
        try:
            resp = await self._client.get("/v1/models")
            if resp.status_code == 200:
                models = resp.json().get("data", [])
                available = [m.get("id", "") for m in models]
                if available:
                    logger.info(
                        "vllm.models_available",
                        extra={"models": available},
                    )
                    if self._model not in available and available:
                        logger.info(
                            "vllm.using_first_available_model",
                            extra={"requested": self._model, "using": available[0]},
                        )
                        self._model = available[0]
            self._initialized = True
            logger.info("vllm.initialized", extra={"base_url": base_url, "model": self._model})
        except httpx.ConnectError:
            raise ConnectionError(
                f"Cannot connect to vLLM at {base_url}. "
                "Ensure vLLM is running: `python -m vllm.entrypoints.openai.api_server`"
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

        # vLLM uses OpenAI-compatible chat completions format
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temp,
            "max_tokens": max_tok,
        }

        resp = await self._client.post("/v1/chat/completions", json=payload)

        if resp.status_code != 200:
            raise RuntimeError(f"vLLM error (status={resp.status_code}): {resp.text[:500]}")

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
        logger.info("vllm.shutdown")

    async def health(self) -> dict[str, Any]:
        if not self._client:
            return {"healthy": False, "provider": "vllm", "model": self._model, "error": "not initialized"}
        try:
            resp = await self._client.get("/v1/models")
            return {
                "healthy": resp.status_code == 200,
                "provider": "vllm",
                "model": self._model,
            }
        except Exception as e:
            return {"healthy": False, "provider": "vllm", "model": self._model, "error": str(e)}
