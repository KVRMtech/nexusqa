"""Ollama HTTP client with JSON-mode parsing.

This is the production transport used by ``classifier.py``. It calls
``POST /api/chat`` with ``format=json`` so the model is constrained to
emit a valid JSON object. We then parse strictly via Pydantic.

The class is intentionally minimal — no streaming, no tool calls.
Echo classification is a one-shot prompt that fits in a single
request/response.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)


class LLMError(Exception):
    """Wraps transport / decode failures from the LLM."""


class LLMTimeout(LLMError):
    """Request exceeded the configured timeout."""


class LLMResponseInvalid(LLMError):
    """Model emitted output that failed JSON / schema validation."""


@dataclass(frozen=True)
class ChatMessage:
    role: str  # 'system' | 'user' | 'assistant'
    content: str

    def to_dict(self) -> dict[str, Any]:
        return {"role": self.role, "content": self.content}


class OllamaJsonClient:
    """Minimal Ollama client for JSON-mode chat completions."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 12.0,
        max_retries: int = 1,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._max_retries = max(0, max_retries)
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout_seconds,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def chat_json(
        self,
        *,
        model: str,
        messages: list[ChatMessage],
        temperature: float = 0.0,
        max_tokens: Optional[int] = None,
        keep_alive: str = "5m",
    ) -> dict[str, Any]:
        """Call ``/api/chat`` in JSON mode. Returns the parsed object.

        Retries on transport errors only; never on a 4xx response or
        a JSON-decode failure (those are deterministic and won't
        change on retry).
        """
        payload: dict[str, Any] = {
            "model": model,
            "messages": [m.to_dict() for m in messages],
            "stream": False,
            "format": "json",
            "options": {"temperature": temperature},
            "keep_alive": keep_alive,
        }
        if max_tokens is not None:
            payload["options"]["num_predict"] = max_tokens

        attempt = 0
        last_exc: Optional[Exception] = None
        while attempt <= self._max_retries:
            attempt += 1
            try:
                resp = await self._client.post("/api/chat", json=payload)
            except httpx.TimeoutException as exc:
                last_exc = exc
                if attempt > self._max_retries:
                    raise LLMTimeout(f"ollama timed out: {exc}") from exc
                continue
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt > self._max_retries:
                    raise LLMError(f"ollama transport error: {exc}") from exc
                continue

            if resp.status_code >= 400:
                raise LLMError(
                    f"ollama returned {resp.status_code}: {resp.text[:512]}"
                )

            try:
                envelope = resp.json()
            except ValueError as exc:
                raise LLMResponseInvalid(
                    f"ollama envelope was not JSON: {exc}"
                ) from exc

            content = (
                envelope.get("message", {}).get("content", "")
                if isinstance(envelope.get("message"), dict)
                else ""
            )
            if not isinstance(content, str) or not content.strip():
                raise LLMResponseInvalid(
                    "ollama returned an empty assistant message"
                )

            try:
                parsed = json.loads(content)
            except ValueError as exc:
                raise LLMResponseInvalid(
                    f"model output was not JSON: {exc} | body={content[:512]}"
                ) from exc

            if not isinstance(parsed, dict):
                raise LLMResponseInvalid(
                    f"model output was not a JSON object: type={type(parsed).__name__}"
                )

            return parsed

        # Should be unreachable due to the raises above.
        raise LLMError(f"ollama exhausted retries: {last_exc}")

    async def health(self) -> str:
        try:
            resp = await self._client.get("/api/tags", timeout=3.0)
            return "healthy" if resp.status_code == 200 else f"degraded:{resp.status_code}"
        except Exception as exc:
            return f"unhealthy:{type(exc).__name__}"
