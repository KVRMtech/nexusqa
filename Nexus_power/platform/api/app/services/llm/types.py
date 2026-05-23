"""Common request/response types for the LLM abstraction.

These dataclasses are the only thing application code touches.
Providers convert between these and their native API formats so
swapping providers does not change call sites.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal


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
