"""Common request/response types for the LLM abstraction.

These dataclasses are the only thing application code touches.
Providers convert between these and their native API formats so
swapping providers does not change call sites.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal


@dataclass(frozen=True)
class ImageContent:
    """One image attached to a multimodal LLM call.

    The provider adapter is responsible for converting this into its
    native shape (Anthropic vision blocks, OpenAI image_url parts,
    etc.).  Application code only needs to load the bytes and declare
    the media type.

    Why bytes (not a URL): we run inside the platform-api container
    where the frames live behind the eyes-engine HTTP API or in the
    artifact store.  Most providers accept base64-encoded inline
    images, so passing bytes is the lowest-common-denominator and
    avoids exposing internal asset URLs to third-party services.
    """

    data: bytes
    """Raw image bytes.  Will be base64-encoded by the provider adapter."""

    media_type: Literal["image/png", "image/jpeg", "image/webp", "image/gif"] = "image/png"
    """MIME type.  Anthropic supports these four; OpenAI/Ollama subset overlap."""


@dataclass(frozen=True)
class ToolDefinition:
    """A tool the LLM can choose to call, exposing a JSON Schema for structured output.

    Used by the action_extractor service to constrain the LLM's output
    to a SceneAction shape: register a tool named ``record_scene_action``
    whose ``input_schema`` is ``SceneAction.model_json_schema()``, then
    set ``tool_choice = "record_scene_action"`` to force the LLM to
    produce a tool_use block rather than free-form text.

    The provider adapter converts this into the native tool format
    (Anthropic ``tools`` array, OpenAI ``tools`` array of type
    ``function``).
    """

    name: str
    """Unique identifier for this tool within the request."""

    description: str
    """One-sentence explanation shown to the LLM.  Helps it decide when to use the tool."""

    input_schema: dict
    """JSON Schema (typically from ``BaseModel.model_json_schema()``) describing the tool's required arguments."""


@dataclass(frozen=True)
class ToolCall:
    """One tool_use block from the LLM, returned in CompletionResponse.tool_calls.

    Populated by the Anthropic provider when ``stop_reason == "tool_use"``
    and by the OpenAI-compatible provider when ``finish_reason == "tool_calls"``.
    """

    name: str
    """Tool name the LLM chose to call."""

    arguments: dict
    """Parsed JSON arguments the LLM provided.  Already json.loads'd."""

    tool_call_id: str = ""
    """Provider-supplied id for this call.  Used when sending tool results back in multi-turn flows; unused in single-shot extraction."""


class FinishReason(str, Enum):
    """Why generation stopped, mapped from provider-specific reasons.

    Stored as strings so JSON-logging is friendly.  Application code
    may treat ``LENGTH`` differently from ``STOP`` (e.g. enforce a
    "weak caption" classification when length-truncated).
    """

    STOP = "stop"           # natural end of generation
    LENGTH = "length"       # hit max_tokens
    STOP_SEQUENCE = "stop_sequence"  # hit one of stop_sequences
    CONTENT_FILTER = "content_filter"  # provider-side moderation
    TOOL_USE = "tool_use"   # requested tool use (not used in Phase 1)
    ERROR = "error"         # provider returned an error/empty body
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CompletionRequest:
    """One inference request, agnostic to which provider executes it.

    Fields are the lowest-common-denominator of OpenAI-compatible,
    Anthropic, and Ollama APIs.  Provider implementations translate
    these into native payloads.
    """

    # System / instruction prompt.  Empty means "no system prompt"
    # (some providers ignore this; others place it in a separate field).
    system: str = ""

    # The user prompt.  Must be non-empty — providers return ERROR
    # finish_reason if asked to generate from nothing.
    prompt: str = ""

    # Hard cap on output tokens.  Providers may map this to their
    # native field (``num_predict``, ``max_tokens``, etc.).  Setting
    # too low silently truncates with finish_reason=LENGTH.
    max_tokens: int = 256

    # Sampling temperature.  0.0 = deterministic greedy decoding.
    # Caption rewriting wants low temperature for consistency; creative
    # tasks (story generation) want higher.
    temperature: float = 0.2

    # Top-p nucleus sampling.  None means "use provider default".
    top_p: float | None = None

    # Generation stops as soon as the model outputs any of these strings.
    # Useful for enforcing one-line outputs.
    stop_sequences: tuple[str, ...] = ()

    # When ``"json"`` and the provider supports JSON mode, the provider
    # will set the appropriate native flag.  Providers that do not
    # support a JSON mode ignore this hint (callers must still parse).
    response_format: Literal["text", "json"] = "text"

    # Per-call timeout.  Overrides the tier default when set; None
    # falls back to the tier's configured timeout.
    request_timeout_s: float | None = None

    # Arbitrary correlation id (caller-provided, e.g. caption_id).
    # Propagated to logs so noisy requests can be traced back to the
    # caller without bespoke logging code.
    correlation_id: str = ""

    # Free-form per-call metadata that the router includes in logs.
    metadata: dict = field(default_factory=dict)

    # ── Multimodal inputs (vision tiers) ────────────────────────────────
    # When non-empty, the provider adapter attaches these images to the
    # user message before the text prompt.  Providers that don't support
    # vision return an ERROR finish reason rather than silently dropping
    # the images.  Anthropic supports up to 100 images per request;
    # OpenAI supports a smaller cap; Ollama support varies by model.
    images: tuple[ImageContent, ...] = ()

    # ── Tool use / structured output ────────────────────────────────────
    # When non-empty, the LLM may produce a tool_use response instead of
    # free-form text.  Set ``tool_choice`` to force a specific tool.
    # The action_extractor uses this to constrain output to the
    # SceneAction Pydantic schema with no prose parsing.
    tools: tuple[ToolDefinition, ...] = ()

    # ``None`` = LLM may freely use any of the provided tools or none.
    # ``"any"`` = LLM MUST use one of the provided tools.
    # ``"<tool_name>"`` = LLM MUST use the named tool.
    tool_choice: str | None = None

    # ── Prompt caching ──────────────────────────────────────────────────
    # When True and the provider supports caching (Anthropic Sonnet/Opus),
    # the system prompt + tool definitions are marked cache_control:
    # ephemeral.  Subsequent calls within ~5 min reuse the cache, cutting
    # input tokens by ~80% on the cached portion.  Safe to enable
    # unconditionally — providers that don't support it ignore the flag.
    enable_prompt_cache: bool = False


@dataclass(frozen=True)
class CompletionResponse:
    """One inference response, agnostic to which provider produced it.

    All fields are populated even when the call failed — providers
    return ``finish_reason=ERROR`` with ``text=""`` rather than raising
    on transient failures so the router can fall back cleanly.
    """

    text: str
    finish_reason: FinishReason

    # Effective provider/tier/model that handled the call.  Differs
    # from the requested tier when the router fell back.
    tier: str
    provider: str
    model: str

    # Token usage, when the provider reports it.  Ollama returns these
    # via ``prompt_eval_count`` / ``eval_count``; OpenAI via ``usage``;
    # Anthropic via ``usage``.  None means "provider did not report".
    prompt_tokens: int | None = None
    completion_tokens: int | None = None

    # End-to-end wall time including network and JSON parsing.
    latency_ms: int = 0

    # Number of retries the router performed for this response (0 when
    # the first attempt succeeded).
    retries: int = 0

    # Whether the router fell back to a different tier than requested.
    fell_back: bool = False

    # Diagnostic — populated by the router on errors.  Never expose to
    # end-users (may contain provider error messages, model names, etc.)
    error_detail: str = ""

    # ── Tool use (structured output) ────────────────────────────────────
    # Populated when the LLM chose to invoke one of the tools in the
    # request.  Empty tuple for plain text responses.  Callers that
    # expected tool use should check ``finish_reason == TOOL_USE`` and
    # read ``tool_calls`` instead of ``text``.
    tool_calls: tuple = ()  # tuple[ToolCall, ...] — annotated loosely to avoid forward-ref cycles

    # ── Cache hit telemetry (when prompt caching is enabled) ────────────
    # Anthropic reports cache_creation_input_tokens and
    # cache_read_input_tokens in the usage dict.  Surfaced here so
    # downstream services can track cache effectiveness.
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None
