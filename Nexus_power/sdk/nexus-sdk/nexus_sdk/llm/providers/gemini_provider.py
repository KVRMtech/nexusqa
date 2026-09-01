"""
Google Gemini LLM Provider — Gemini 3 Pro, Gemini 2.0 Flash, etc.

Best-in-class multimodal capabilities (vision, video, audio, text).
Primary choice for Eyes engine (UI understanding, OCR, visual flows).

Configuration:
  LLM_PROVIDER=gemini
  LLM_API_KEY=AIza...           (Google AI API key)
  LLM_MODEL=gemini-3-pro
  GEMINI_BASE_URL=https://generativelanguage.googleapis.com  (optional)

Supported models:
  - gemini-3-pro             : Best multimodal (vision, video, audio, reasoning)
  - gemini-2.0-flash         : Fast, cost-efficient
  - gemini-2.0-flash-lite    : Cheapest, suitable for simple tasks
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from nexus_sdk.llm.base import LLMProvider, LLMResponse
from nexus_sdk.llm.config import LLMConfig

logger = logging.getLogger(__name__)

# Gemini API version
GEMINI_API_VERSION = "v1beta"


class GeminiProvider(LLMProvider):
    """Google Gemini (Generative Language API) provider."""

    def __init__(self, config: LLMConfig):
        super().__init__(config)
        self._client: Optional[httpx.AsyncClient] = None
        self._model: str = ""
        self._api_key: str = ""

    async def initialize(self) -> None:
        if not self._config.api_key:
            raise ValueError(
                "LLM_API_KEY is required for Google Gemini. "
                "Set LLM_API_KEY=AIza... (Google AI API key) in your environment."
            )

        self._api_key = self._config.api_key
        base_url = self._config.get_effective_base_url() or "https://generativelanguage.googleapis.com"
        self._model = self._config.get_effective_model()

        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=float(self._config.timeout),
            headers={
                "Content-Type": "application/json",
            },
        )

        # Verify connectivity — list models
        try:
            resp = await self._client.get(
                f"/{GEMINI_API_VERSION}/models",
                params={"key": self._api_key},
            )
            if resp.status_code == 200:
                self._initialized = True
                logger.info(
                    "gemini.initialized",
                    extra={"model": self._model, "base_url": base_url},
                )
            elif resp.status_code == 401 or resp.status_code == 403:
                raise ValueError("Invalid Google AI API key")
            else:
                # Trust and verify on first generate call
                self._initialized = True
                logger.info(
                    "gemini.initialized_without_model_check",
                    extra={"model": self._model, "status": resp.status_code},
                )
        except httpx.ConnectError as e:
            raise ConnectionError(
                f"Cannot connect to Gemini API at {base_url}: {e}"
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

        # Gemini generateContent payload format
        payload: dict[str, Any] = {
            "contents": [
                {"role": "user", "parts": [{"text": user_prompt}]},
            ],
            "generationConfig": {
                "temperature": temp,
                "maxOutputTokens": max_tok,
                "topP": self._config.top_p,
            },
        }

        # System instruction (Gemini uses a separate field, similar to Anthropic)
        if system_prompt:
            payload["systemInstruction"] = {
                "parts": [{"text": system_prompt}],
            }

        # JSON structured output
        if use_json:
            payload["generationConfig"]["responseMimeType"] = "application/json"

        url = f"/{GEMINI_API_VERSION}/models/{self._model}:generateContent"
        resp = await self._client.post(
            url,
            json=payload,
            params={"key": self._api_key},
        )

        if resp.status_code == 429:
            raise RuntimeError("Gemini rate limit exceeded — will retry")
        if resp.status_code in (401, 403):
            raise ValueError("Invalid Google AI API key")
        if resp.status_code != 200:
            raise RuntimeError(
                f"Gemini error (status={resp.status_code}): {resp.text[:500]}"
            )

        data = resp.json()

        # Extract text from candidates
        candidates = data.get("candidates", [])
        content = ""
        finish_reason = ""
        if candidates:
            candidate = candidates[0]
            parts = candidate.get("content", {}).get("parts", [])
            content = "".join(p.get("text", "") for p in parts)
            finish_reason = candidate.get("finishReason", "")

        # Token usage from usageMetadata
        usage = data.get("usageMetadata", {})

        return LLMResponse(
            content=content,
            model=self._model,
            prompt_tokens=usage.get("promptTokenCount", 0),
            completion_tokens=usage.get("candidatesTokenCount", 0),
            total_tokens=usage.get("totalTokenCount", 0),
            finish_reason=finish_reason.lower() if finish_reason else "",
            raw_response=data,
        )

    async def shutdown(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None
        self._initialized = False
        logger.info("gemini.shutdown")

    async def health(self) -> dict[str, Any]:
        if not self._client:
            return {
                "healthy": False,
                "provider": "gemini",
                "model": self._model,
                "error": "not initialized",
            }
        try:
            resp = await self._client.get(
                f"/{GEMINI_API_VERSION}/models",
                params={"key": self._api_key},
            )
            return {
                "healthy": resp.status_code == 200,
                "provider": "gemini",
                "model": self._model,
            }
        except Exception as e:
            return {
                "healthy": False,
                "provider": "gemini",
                "model": self._model,
                "error": str(e),
            }
