"""
Ollama LLM Provider — On-prem inference via Ollama REST API.

Ollama runs locally and supports hundreds of open-weight models.
Recommended for on-prem deployments where data cannot leave the datacenter.

Configuration:
  LLM_PROVIDER=ollama
  OLLAMA_BASE_URL=http://localhost:11434
  OLLAMA_MODEL=llama3.1:70b
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

import httpx

from nexus_sdk.llm.base import LLMProvider, LLMResponse
from nexus_sdk.llm.config import LLMConfig

logger = logging.getLogger(__name__)


class OllamaProvider(LLMProvider):
    """Ollama REST API provider for local on-prem models."""

    def __init__(self, config: LLMConfig):
        super().__init__(config)
        self._client: Optional[httpx.AsyncClient] = None
        self._model: str = ""

    async def initialize(self) -> None:
        base_url = self._config.ollama_base_url or "http://localhost:11434"
        self._model = self._config.get_effective_model()
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=float(self._config.timeout),
        )

        # Verify Ollama is running
        try:
            resp = await self._client.get("/api/tags")
            if resp.status_code != 200:
                raise ConnectionError(f"Ollama returned status {resp.status_code}")

            models = resp.json().get("models", [])
            available = [m.get("name", "") for m in models]
            if not any(self._model in name for name in available):
                logger.info(
                    "ollama.model_not_found, will pull on first request",
                    extra={"model": self._model, "available": available},
                )

            self._initialized = True
            logger.info(
                "ollama.initialized",
                extra={"base_url": base_url, "model": self._model},
            )
        except httpx.ConnectError:
            raise ConnectionError(
                f"Cannot connect to Ollama at {base_url}. "
                "Ensure Ollama is running: `ollama serve`"
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
            "stream": False,
            "keep_alive": "30m",
            "options": {
                "temperature": temp,
                "num_predict": max_tok,
                "num_ctx": int(os.environ.get("OLLAMA_NUM_CTX", "4096")),
            },
        }
        if use_json:
            payload["format"] = "json"

        resp = await self._client.post("/api/chat", json=payload)

        # Auto-pull model if Ollama returns 404 (model not found)
        if resp.status_code == 404 and "not found" in resp.text.lower():
            logger.info(
                "ollama.auto_pull",
                extra={"model": self._model, "detail": resp.text[:200]},
            )
            pull_resp = await self._client.post(
                "/api/pull",
                json={"name": self._model, "stream": False},
                timeout=600.0,
            )
            if pull_resp.status_code == 200:
                logger.info(
                    "ollama.auto_pull.success",
                    extra={"model": self._model},
                )
                # Retry the original request after pull
                resp = await self._client.post("/api/chat", json=payload)
            else:
                raise RuntimeError(
                    f"Ollama model '{self._model}' not found and auto-pull failed "
                    f"(status={pull_resp.status_code}): {pull_resp.text[:300]}"
                )

        if resp.status_code != 200:
            raise RuntimeError(f"Ollama error (status={resp.status_code}): {resp.text[:500]}")

        data = resp.json()
        content = data.get("message", {}).get("content", "")

        # Ollama provides token counts in eval_count / prompt_eval_count
        prompt_tokens = data.get("prompt_eval_count", 0)
        completion_tokens = data.get("eval_count", 0)

        return LLMResponse(
            content=content,
            model=self._model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            finish_reason="stop",
            raw_response=data,
        )

    async def shutdown(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None
        self._initialized = False
        logger.info("ollama.shutdown")

    async def health(self) -> dict[str, Any]:
        if not self._client:
            return {"healthy": False, "provider": "ollama", "model": self._model, "error": "not initialized"}
        try:
            resp = await self._client.get("/api/tags")
            return {
                "healthy": resp.status_code == 200,
                "provider": "ollama",
                "model": self._model,
                "base_url": str(self._client.base_url),
            }
        except Exception as e:
            return {"healthy": False, "provider": "ollama", "model": self._model, "error": str(e)}
