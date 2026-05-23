"""LLM provider implementations.

Three providers ship out of the box:

* ``OllamaProvider``       — Ollama's REST API (``/api/chat``).  Free,
  runs locally next to platform-api in docker-compose.
* ``OpenAICompatProvider`` — Any service that speaks the OpenAI
  ``/v1/chat/completions`` shape.  This covers OpenAI itself, Azure
  OpenAI, OpenRouter, Anyscale, Together, vLLM, LM Studio, and
  countless others.  Bring-your-own-base-URL.
* ``AnthropicProvider``    — Native Anthropic ``/v1/messages`` shape.

Adding a fourth provider takes one new class and one entry in
``build_provider()``.  The protocol surface is intentionally small
(complete + health_check + close) so this stays sane.

All providers:

* use ``httpx.AsyncClient`` for HTTP (already in the SDK deps).
* never raise on transient failure — they return a ``CompletionResponse``
  with ``finish_reason=ERROR`` and let the router decide whether to
  retry / fall back.
* redact api_key + bearer tokens from any error_detail string.
* report latency_ms and (when available) token usage.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Protocol

import httpx

from .config import LLMTierConfig
from .types import CompletionRequest, CompletionResponse, FinishReason


logger = logging.getLogger(__name__)


class LLMError(Exception):
    """Base class for non-recoverable LLM errors (config, protocol)."""


class LLMProviderError(LLMError):
    """Raised by providers on irrecoverable misuse (never on transient failures)."""


class LLMProvider(Protocol):
    """Provider interface.

    Implementations must be safe to share across coroutines (the router
    holds one provider instance per tier and reuses it for the lifetime
    of the application).
    """

    name: str

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        ...

    async def health_check(self) -> bool:
        ...

    async def close(self) -> None:
        ...


# ── Shared utilities ──────────────────────────────────────────────────────────


def _redact(text: str, api_key: str) -> str:
    """Remove an API key (and a bearer header form) from ``text``."""
    if not text:
        return ""
    redacted = text
    if api_key:
        redacted = redacted.replace(api_key, "[redacted-api-key]")
        if len(api_key) > 8:
            redacted = redacted.replace(api_key[-8:], "[redacted-api-key-suffix]")
    return redacted


def _now_ms() -> int:
    return int(time.monotonic() * 1000)


def _error_response(
    *,
    tier: str,
    provider: str,
    model: str,
    detail: str,
    latency_ms: int,
) -> CompletionResponse:
    return CompletionResponse(
        text="",
        finish_reason=FinishReason.ERROR,
        tier=tier,
        provider=provider,
        model=model,
        prompt_tokens=None,
        completion_tokens=None,
        latency_ms=latency_ms,
        retries=0,
        fell_back=False,
        error_detail=detail,
    )


def _coerce_timeout(request: CompletionRequest, tier_timeout: float) -> float:
    """Per-call override wins over tier default."""
    if request.request_timeout_s is not None and request.request_timeout_s > 0:
        return float(request.request_timeout_s)
    return float(tier_timeout)


# ── Ollama ────────────────────────────────────────────────────────────────────


class OllamaProvider:
    """Ollama-native provider using the ``/api/chat`` endpoint.

    Ollama exposes both a ``/api/generate`` (single-prompt) and a
    ``/api/chat`` (messages) endpoint.  We use ``chat`` because it lets
    the application separate system instructions from the user prompt
    without prompt-template gymnastics.
    """

    name: str = "ollama"

    def __init__(self, config: LLMTierConfig) -> None:
        self._config = config
        # Long connection lifetime — providers are reused for the
        # lifetime of the app.  Per-call timeout is enforced via
        # ``httpx.Timeout`` per-request.
        self._client = httpx.AsyncClient(
            base_url=config.base_url,
            timeout=httpx.Timeout(config.timeout_s),
            headers={"Content-Type": "application/json", **config.extra_headers},
        )

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        messages: list[dict] = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        messages.append({"role": "user", "content": request.prompt})

        options: dict = {
            "temperature": request.temperature,
            "num_predict": request.max_tokens,
        }
        if request.top_p is not None:
            options["top_p"] = request.top_p
        if request.stop_sequences:
            options["stop"] = list(request.stop_sequences)

        body = {
            "model": self._config.model,
            "messages": messages,
            "stream": False,
            "options": options,
        }
        if request.response_format == "json":
            body["format"] = "json"

        timeout = _coerce_timeout(request, self._config.timeout_s)
        start = _now_ms()
        try:
            response = await self._client.post(
                "/api/chat",
                json=body,
                timeout=httpx.Timeout(timeout),
            )
        except (httpx.HTTPError, asyncio.TimeoutError) as exc:
            return _error_response(
                tier=self._config.name,
                provider=self.name,
                model=self._config.model,
                detail=f"ollama_transport_error:{type(exc).__name__}:{exc}"[:500],
                latency_ms=_now_ms() - start,
            )

        latency_ms = _now_ms() - start
        if response.status_code >= 400:
            return _error_response(
                tier=self._config.name,
                provider=self.name,
                model=self._config.model,
                detail=f"ollama_http_{response.status_code}:{response.text[:300]}",
                latency_ms=latency_ms,
            )
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            return _error_response(
                tier=self._config.name,
                provider=self.name,
                model=self._config.model,
                detail=f"ollama_invalid_json:{exc}"[:300],
                latency_ms=latency_ms,
            )

        message = payload.get("message") or {}
        text = (message.get("content") or "").strip()
        done_reason = payload.get("done_reason", "stop")
        finish = self._map_finish_reason(done_reason)

        if not text:
            finish = FinishReason.ERROR

        return CompletionResponse(
            text=text,
            finish_reason=finish,
            tier=self._config.name,
            provider=self.name,
            model=payload.get("model") or self._config.model,
            prompt_tokens=payload.get("prompt_eval_count"),
            completion_tokens=payload.get("eval_count"),
            latency_ms=latency_ms,
            retries=0,
            fell_back=False,
            error_detail="" if text else "ollama_empty_response",
        )

    @staticmethod
    def _map_finish_reason(raw: str) -> FinishReason:
        mapping = {
            "stop": FinishReason.STOP,
            "length": FinishReason.LENGTH,
            "load": FinishReason.STOP,           # rare, treat as natural
            "unload": FinishReason.STOP,
            "exit": FinishReason.STOP,
        }
        return mapping.get(str(raw or "").lower(), FinishReason.UNKNOWN)

    async def health_check(self) -> bool:
        try:
            r = await self._client.get("/api/tags", timeout=httpx.Timeout(5.0))
            return r.status_code == 200
        except httpx.HTTPError:
            return False

    async def close(self) -> None:
        await self._client.aclose()


# ── OpenAI-compatible (covers OpenAI, OpenRouter, Anyscale, vLLM, LM Studio) ──


class OpenAICompatProvider:
    """OpenAI ``/v1/chat/completions`` shape.

    Works against any provider that mirrors the OpenAI chat API.  Most
    do — the public OpenAI API, Azure OpenAI (with a different base
    URL + headers), Anyscale, Together, OpenRouter, vLLM, LM Studio,
    Ollama's optional OpenAI shim, etc.
    """

    name: str = "openai_compat"

    def __init__(self, config: LLMTierConfig) -> None:
        self._config = config
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if config.api_key:
            headers["Authorization"] = f"Bearer {config.api_key}"
        headers.update(config.extra_headers)
        self._client = httpx.AsyncClient(
            base_url=config.base_url,
            timeout=httpx.Timeout(config.timeout_s),
            headers=headers,
        )

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        messages: list[dict] = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        messages.append({"role": "user", "content": request.prompt})

        body: dict = {
            "model": self._config.model,
            "messages": messages,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "stream": False,
        }
        if request.top_p is not None:
            body["top_p"] = request.top_p
        if request.stop_sequences:
            body["stop"] = list(request.stop_sequences)
        if request.response_format == "json":
            body["response_format"] = {"type": "json_object"}

        timeout = _coerce_timeout(request, self._config.timeout_s)
        start = _now_ms()
        try:
            response = await self._client.post(
                "/chat/completions",
                json=body,
                timeout=httpx.Timeout(timeout),
            )
        except (httpx.HTTPError, asyncio.TimeoutError) as exc:
            return _error_response(
                tier=self._config.name,
                provider=self.name,
                model=self._config.model,
                detail=_redact(
                    f"openai_compat_transport_error:{type(exc).__name__}:{exc}"[:500],
                    self._config.api_key,
                ),
                latency_ms=_now_ms() - start,
            )

        latency_ms = _now_ms() - start
        if response.status_code >= 400:
            return _error_response(
                tier=self._config.name,
                provider=self.name,
                model=self._config.model,
                detail=_redact(
                    f"openai_compat_http_{response.status_code}:{response.text[:300]}",
                    self._config.api_key,
                ),
                latency_ms=latency_ms,
            )
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            return _error_response(
                tier=self._config.name,
                provider=self.name,
                model=self._config.model,
                detail=f"openai_compat_invalid_json:{exc}"[:300],
                latency_ms=latency_ms,
            )

        choices = payload.get("choices") or []
        if not choices:
            return _error_response(
                tier=self._config.name,
                provider=self.name,
                model=self._config.model,
                detail="openai_compat_empty_choices",
                latency_ms=latency_ms,
            )

        choice = choices[0]
        message = choice.get("message") or {}
        text = (message.get("content") or "").strip()
        finish = self._map_finish_reason(choice.get("finish_reason"))
        usage = payload.get("usage") or {}

        if not text:
            finish = FinishReason.ERROR

        return CompletionResponse(
            text=text,
            finish_reason=finish,
            tier=self._config.name,
            provider=self.name,
            model=payload.get("model") or self._config.model,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            latency_ms=latency_ms,
            retries=0,
            fell_back=False,
            error_detail="" if text else "openai_compat_empty_response",
        )

    @staticmethod
    def _map_finish_reason(raw: str | None) -> FinishReason:
        mapping = {
            "stop": FinishReason.STOP,
            "length": FinishReason.LENGTH,
            "content_filter": FinishReason.CONTENT_FILTER,
            "tool_calls": FinishReason.TOOL_USE,
            "function_call": FinishReason.TOOL_USE,
        }
        return mapping.get(str(raw or "").lower(), FinishReason.UNKNOWN)

    async def health_check(self) -> bool:
        # OpenAI does not have a cheap /healthz; /models is the canonical
        # liveness check.  Authenticated providers return 200 with a list.
        try:
            r = await self._client.get("/models", timeout=httpx.Timeout(5.0))
            return r.status_code in (200, 401, 403)
            # 401/403 still means the service is up — config issue, not down.
        except httpx.HTTPError:
            return False

    async def close(self) -> None:
        await self._client.aclose()


# ── Anthropic (Claude) ────────────────────────────────────────────────────────


class AnthropicProvider:
    """Anthropic native ``/v1/messages`` shape.

    Anthropic uses ``x-api-key`` (not ``Authorization: Bearer``) and
    requires an ``anthropic-version`` header.  System prompts are a
    separate top-level field rather than a message role.
    """

    name: str = "anthropic"
    _DEFAULT_API_VERSION = "2023-06-01"

    def __init__(self, config: LLMTierConfig) -> None:
        self._config = config
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "anthropic-version": self._DEFAULT_API_VERSION,
        }
        if config.api_key:
            headers["x-api-key"] = config.api_key
        headers.update(config.extra_headers)
        self._client = httpx.AsyncClient(
            base_url=config.base_url,
            timeout=httpx.Timeout(config.timeout_s),
            headers=headers,
        )

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        body: dict = {
            "model": self._config.model,
            "messages": [{"role": "user", "content": request.prompt}],
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
        }
        if request.system:
            body["system"] = request.system
        if request.top_p is not None:
            body["top_p"] = request.top_p
        if request.stop_sequences:
            body["stop_sequences"] = list(request.stop_sequences)

        timeout = _coerce_timeout(request, self._config.timeout_s)
        start = _now_ms()
        try:
            response = await self._client.post(
                "/v1/messages",
                json=body,
                timeout=httpx.Timeout(timeout),
            )
        except (httpx.HTTPError, asyncio.TimeoutError) as exc:
            return _error_response(
                tier=self._config.name,
                provider=self.name,
                model=self._config.model,
                detail=_redact(
                    f"anthropic_transport_error:{type(exc).__name__}:{exc}"[:500],
                    self._config.api_key,
                ),
                latency_ms=_now_ms() - start,
            )

        latency_ms = _now_ms() - start
        if response.status_code >= 400:
            return _error_response(
                tier=self._config.name,
                provider=self.name,
                model=self._config.model,
                detail=_redact(
                    f"anthropic_http_{response.status_code}:{response.text[:300]}",
                    self._config.api_key,
                ),
                latency_ms=latency_ms,
            )
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            return _error_response(
                tier=self._config.name,
                provider=self.name,
                model=self._config.model,
                detail=f"anthropic_invalid_json:{exc}"[:300],
                latency_ms=latency_ms,
            )

        text_blocks = payload.get("content") or []
        text = "".join(
            block.get("text", "")
            for block in text_blocks
            if isinstance(block, dict) and block.get("type") == "text"
        ).strip()
        finish = self._map_finish_reason(payload.get("stop_reason"))
        usage = payload.get("usage") or {}

        if not text:
            finish = FinishReason.ERROR

        return CompletionResponse(
            text=text,
            finish_reason=finish,
            tier=self._config.name,
            provider=self.name,
            model=payload.get("model") or self._config.model,
            prompt_tokens=usage.get("input_tokens"),
            completion_tokens=usage.get("output_tokens"),
            latency_ms=latency_ms,
            retries=0,
            fell_back=False,
            error_detail="" if text else "anthropic_empty_response",
        )

    @staticmethod
    def _map_finish_reason(raw: str | None) -> FinishReason:
        mapping = {
            "end_turn": FinishReason.STOP,
            "max_tokens": FinishReason.LENGTH,
            "stop_sequence": FinishReason.STOP_SEQUENCE,
            "tool_use": FinishReason.TOOL_USE,
        }
        return mapping.get(str(raw or "").lower(), FinishReason.UNKNOWN)

    async def health_check(self) -> bool:
        # Anthropic has no public /healthz; the cheapest liveness is a
        # 1-token completion against the configured model.  We treat
        # non-network failures (4xx) as "service up".
        try:
            r = await self._client.post(
                "/v1/messages",
                json={
                    "model": self._config.model,
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 1,
                },
                timeout=httpx.Timeout(5.0),
            )
            return r.status_code < 500
        except httpx.HTTPError:
            return False

    async def close(self) -> None:
        await self._client.aclose()


# ── Factory ───────────────────────────────────────────────────────────────────


def build_provider(config: LLMTierConfig) -> LLMProvider:
    """Construct the right provider for a tier's configured provider name.

    Adding a fourth provider takes one new class plus one line in this
    factory.  Callers (the router) never import provider classes
    directly.
    """
    provider = config.provider.lower()
    if provider == "ollama":
        return OllamaProvider(config)
    if provider == "openai_compat":
        return OpenAICompatProvider(config)
    if provider == "anthropic":
        return AnthropicProvider(config)
    raise LLMProviderError(f"unknown provider {config.provider!r} for tier {config.name!r}")
