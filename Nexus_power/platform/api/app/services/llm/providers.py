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
import base64
import json
import logging
import time
from typing import Protocol

import httpx

from .config import LLMTierConfig
from .types import (
    CompletionRequest,
    CompletionResponse,
    FinishReason,
    ImageContent,
    ToolCall,
    ToolDefinition,
)


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


def _b64_encode_bytes(data: bytes) -> str:
    """Encode raw image bytes to a base64 ASCII string.

    Used by the vision-capable providers to attach inline images in
    the API request body.  Returns the bare base64 (no ``data:`` URI
    prefix) since Anthropic and OpenAI both want just the encoded
    bytes.
    """
    if not data:
        return ""
    return base64.b64encode(data).decode("ascii")


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

        # Compose the system message.  When ``tools`` are present we
        # append the tool's input_schema as a JSON Schema reference so
        # the model knows the exact shape to output.  Ollama's tool API
        # support varies by model and isn't reliable enough for
        # production; we use ``format: "json"`` + schema-in-prompt
        # instead, which works across every Ollama model.
        system_parts: list[str] = []
        if request.system:
            system_parts.append(request.system)
        if request.tools:
            tool = request.tools[0]  # Phase 1 uses exactly one tool.
            system_parts.append(
                f"You MUST respond with ONLY a JSON object matching this "
                f"schema (no prose, no markdown):\n\n{json.dumps(tool.input_schema)}"
            )
        if system_parts:
            messages.append({"role": "system", "content": "\n\n".join(system_parts)})

        # User message — attach base64-encoded images alongside the
        # text prompt when this is a multimodal call.  Ollama's chat
        # API accepts ``images: [base64...]`` on the user message for
        # any vision-capable model (llava, llama3.2-vision, moondream,
        # gemma3, qwen2-vl, etc.).
        user_message: dict = {"role": "user", "content": request.prompt}
        if request.images:
            user_message["images"] = [
                _b64_encode_bytes(img.data) for img in request.images
            ]
        messages.append(user_message)

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
        # When tools are present we force JSON output mode — Ollama
        # constrains the model's output to syntactically valid JSON.
        # The action_extractor's fallback path parses this JSON against
        # the SceneAction Pydantic schema.
        if request.response_format == "json" or request.tools:
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

    Vision support
    --------------
    When ``CompletionRequest.images`` is non-empty, the provider builds
    the user message as a content array with image blocks first, then
    the text block.  Anthropic's vision API accepts inline base64
    images via ``{type: "image", source: {type: "base64", media_type,
    data}}``.

    Tool use (structured output)
    ----------------------------
    When ``CompletionRequest.tools`` is non-empty, the provider passes
    the tools array and (optionally) a ``tool_choice`` override.  If
    the LLM uses a tool, the response carries a ``tool_use`` block;
    we parse the arguments dict and return it as a ``ToolCall`` in
    ``CompletionResponse.tool_calls``.  ``finish_reason`` is set to
    ``TOOL_USE`` so callers know to read tool_calls instead of text.

    Prompt caching
    --------------
    When ``CompletionRequest.enable_prompt_cache`` is True, the system
    prompt and tools are marked ``cache_control: {type: "ephemeral"}``.
    Anthropic stores the cached portion for ~5 min; subsequent calls
    within that window reuse the cache and pay ~10% of the normal
    input price for those tokens.  Cache hit telemetry is surfaced in
    ``CompletionResponse.cache_read_tokens`` and
    ``cache_creation_tokens``.
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
        body = self._build_request_body(request)

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

        content_blocks = payload.get("content") or []
        text = "".join(
            block.get("text", "")
            for block in content_blocks
            if isinstance(block, dict) and block.get("type") == "text"
        ).strip()

        # Extract tool_use blocks when present.  When the LLM was given
        # tools and chose to use one, the response will have a
        # ``tool_use`` content block instead of (or in addition to) a
        # text block.
        tool_calls: list[ToolCall] = []
        for block in content_blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "tool_use":
                continue
            tool_calls.append(ToolCall(
                name=str(block.get("name", "")),
                arguments=dict(block.get("input", {})),
                tool_call_id=str(block.get("id", "")),
            ))

        finish = self._map_finish_reason(payload.get("stop_reason"))
        usage = payload.get("usage") or {}

        # When the LLM used a tool, prefer that path even if there's no text.
        if not text and not tool_calls:
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
            error_detail="" if (text or tool_calls) else "anthropic_empty_response",
            tool_calls=tuple(tool_calls),
            cache_read_tokens=usage.get("cache_read_input_tokens"),
            cache_creation_tokens=usage.get("cache_creation_input_tokens"),
        )

    def _build_request_body(self, request: CompletionRequest) -> dict:
        """Translate the unified CompletionRequest into Anthropic's body shape.

        Extracted to a helper so the vision/tool/cache branches are
        each independently testable without spinning up an HTTP mock.
        """
        # Build the user-message content.  Plain-text-only requests
        # keep the legacy ``content: <string>`` shape for backwards
        # compatibility; multimodal requests promote to the content-array
        # form Anthropic requires for image input.
        if request.images:
            content_parts: list[dict] = []
            for img in request.images:
                content_parts.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": img.media_type,
                        "data": _b64_encode_bytes(img.data),
                    },
                })
            content_parts.append({"type": "text", "text": request.prompt})
            user_content: object = content_parts
        else:
            user_content = request.prompt

        body: dict = {
            "model": self._config.model,
            "messages": [{"role": "user", "content": user_content}],
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
        }
        if request.system:
            # System prompts ALSO accept the content-array form when we
            # need cache_control on them.  Use plain string when no
            # caching needed to keep the request body lean.
            if request.enable_prompt_cache:
                body["system"] = [{
                    "type": "text",
                    "text": request.system,
                    "cache_control": {"type": "ephemeral"},
                }]
            else:
                body["system"] = request.system
        if request.top_p is not None:
            body["top_p"] = request.top_p
        if request.stop_sequences:
            body["stop_sequences"] = list(request.stop_sequences)

        # Tools and tool_choice.  When caching is enabled the tool
        # definitions are also marked cache_control on the LAST tool —
        # Anthropic caches everything up to that breakpoint.
        if request.tools:
            tools_list: list[dict] = []
            for idx, tool in enumerate(request.tools):
                tool_dict: dict = {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.input_schema,
                }
                # Mark the last tool with cache_control when caching is on,
                # so the cache breakpoint covers all tool definitions.
                if request.enable_prompt_cache and idx == len(request.tools) - 1:
                    tool_dict["cache_control"] = {"type": "ephemeral"}
                tools_list.append(tool_dict)
            body["tools"] = tools_list

            if request.tool_choice:
                if request.tool_choice == "any":
                    body["tool_choice"] = {"type": "any"}
                else:
                    body["tool_choice"] = {
                        "type": "tool",
                        "name": request.tool_choice,
                    }

        return body

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
