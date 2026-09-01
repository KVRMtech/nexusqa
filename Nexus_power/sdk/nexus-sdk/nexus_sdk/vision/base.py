"""Vision provider base abstractions.

Every concrete provider (Ollama, Anthropic, OpenAI, Gemini, vLLM, ...)
implements :class:`VisionProvider` and returns a :class:`VisionAnalysisResponse`
with the canonical four fields the rest of the Nexus pipeline depends on:

    description   — one-paragraph natural-language summary of the screen
    ui_elements   — list of detected UI controls with types, labels, regions
    tables        — extracted data tables (empty list when none)
    page_title    — visible page or dialog title

Concrete providers are wired into :func:`nexus_sdk.vision.build_vision_router`
through :class:`nexus_sdk.vision.registry`; nothing in this base module knows
about specific cloud / local backends.

Failures raise :class:`VisionProviderError` so the tier router can decide
whether to retry on the same provider or fall back to the next tier.
"""

from __future__ import annotations

import abc
import time
from dataclasses import dataclass, field
from typing import Any, Optional


class VisionProviderError(RuntimeError):
    """Raised when a vision provider call fails after its own retries.

    The tier router catches this and tries the next configured tier.  Wrap
    the underlying provider exception with a short reason so logs stay
    actionable.
    """

    def __init__(self, message: str, *, provider: str = "", retriable: bool = True):
        super().__init__(message)
        self.provider = provider
        # ``retriable`` is True when the failure was transient (5xx, timeout,
        # connection reset).  False when it was a permanent/auth failure
        # (401, 403, image-format rejected) — the router uses this hint to
        # avoid hammering a misconfigured provider on every call.
        self.retriable = retriable


# ─── Request / Response payloads ──────────────────────────────────────────────

@dataclass
class VisionAnalysisRequest:
    """One frame to analyse, plus the supporting context.

    ``frame_path`` is the only mandatory field; everything else is optional
    additional context that helps the model produce a focused description.
    Providers may ignore fields they cannot use.

    For per-frame LLaVA with delta prompts (Phase B.1) the caller sets
    ``previous_description`` to the prior frame's description and
    ``previous_frame_path`` when the provider supports image-pair
    comparison (Anthropic / OpenAI multi-image).  Ollama uses only
    ``previous_description`` in the prompt today.
    """

    frame_path: str
    ocr_text: str = ""
    app_type: str = ""
    previous_description: str = ""
    previous_frame_path: Optional[str] = None
    # Per-call overrides — when None the provider uses its config defaults.
    max_tokens: Optional[int] = None
    temperature: Optional[float] = None


@dataclass
class VisionTransitionRequest:
    """Two-image transition analysis — what action happened between
    BEFORE and AFTER screenshots.

    Used by eyes-engine's scene-transition LLM pass. Providers with
    native multi-image support (Anthropic/OpenAI) reason over both
    images simultaneously; Ollama-style providers send a single prompt
    that references both base64-encoded images.

    The response shape is fixed (action_kind, action_label,
    target_element_label, observed_value, confidence, evidence_text) —
    the transitions canonical-pipeline step depends on these exact
    field names.
    """

    before_frame_path: str
    after_frame_path: str
    ocr_before: str = ""
    ocr_after: str = ""
    url_changed: bool = False
    app_type: str = ""
    # Per-call overrides — when None the provider uses its config defaults.
    max_tokens: Optional[int] = None
    temperature: Optional[float] = None


@dataclass
class VisionTransitionResponse:
    """Result of a two-image transition analysis.

    Fields mirror the eyes-engine canonical contract verbatim so the
    pipeline can swap providers without remapping. `confidence` is a
    float in [0.0, 1.0]; callers reject below their threshold.

    `kind` values (canonical): click_cta | enter_text | select_option |
    toggle | navigate | submit_form | scroll | unknown.
    """

    kind: str = "unknown"
    action_label: str = ""
    target_element_label: str = ""
    observed_value: str = ""
    confidence: float = 0.0
    evidence_text: str = ""

    provider: str = ""
    model: str = ""
    latency_ms: float = 0.0
    raw_response: Optional[dict[str, Any]] = None

    def to_eyes_dict(self) -> dict[str, Any]:
        """Match the dict shape eyes-engine's
        ``_analyze_scene_transitions_llm`` consumes."""
        return {
            "action_kind": self.kind,
            "action_label": self.action_label,
            "target_element_label": self.target_element_label,
            "observed_value": self.observed_value,
            "confidence": self.confidence,
            "evidence_text": self.evidence_text,
        }


@dataclass
class VisionUIElement:
    """One detected UI control.

    ``element_type`` follows the canonical taxonomy used by
    ``control_extractor`` so downstream code does not have to translate.
    Concrete values: button, text_field, dropdown, label, table, menu,
    checkbox, radio, link, image, tab.
    """

    element_type: str
    text: str = ""
    confidence: float = 0.0
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass
class VisionAnalysisResponse:
    """Provider-agnostic vision analysis result.

    The four required fields (``description``, ``ui_elements``, ``tables``,
    ``page_title``) are the persistent contract.  Diagnostic metadata
    (provider, model, latency_ms, raw_response) is supplied for
    observability and tests but is not relied on by downstream consumers.
    """

    description: str
    ui_elements: list[VisionUIElement] = field(default_factory=list)
    tables: list[list[list[str]]] = field(default_factory=list)
    page_title: str = ""

    provider: str = ""
    model: str = ""
    latency_ms: float = 0.0
    raw_response: Optional[dict[str, Any]] = None
    truncated: bool = False

    def to_eyes_dict(self) -> dict[str, Any]:
        """Convert to the dict shape eyes-engine's :func:`_analyze_scenes` consumes.

        The historical eyes pipeline expects ``ui_elements`` items as plain
        dicts with ``element_type`` / ``text`` / ``confidence`` / ``properties``
        rather than dataclass instances.  This conversion preserves binary
        compatibility with the rest of the pipeline.
        """
        return {
            "description": self.description,
            "ui_elements": [
                {
                    "element_type": el.element_type,
                    "text": el.text,
                    "confidence": el.confidence,
                    "properties": dict(el.properties or {}),
                }
                for el in self.ui_elements
            ],
            "tables": [list(t) for t in self.tables],
            "page_title": self.page_title,
        }


# ─── Provider ABC ─────────────────────────────────────────────────────────────

class VisionProvider(abc.ABC):
    """Abstract base for all vision-capable LLM providers.

    Concrete providers implement :meth:`_do_analyze` (the actual API call)
    and :meth:`health` (a quick probe used by the tier router).  The base
    class layers in retry, timing, and structured logging so every provider
    behaves consistently.
    """

    name: str = "unknown"
    model: str = ""

    def __init__(self, *, name: str, model: str, retries: int = 1):
        self.name = name
        self.model = model
        self.retries = max(0, int(retries))
        self._initialized = False
        self._total_calls = 0
        self._total_failures = 0
        self._total_latency_ms = 0.0

    # ── Lifecycle ────────────────────────────────────────────────

    @abc.abstractmethod
    async def initialize(self) -> None:
        """Open HTTP clients, validate credentials, warm caches.

        Called once during router startup.  Implementations should raise
        :class:`VisionProviderError` with ``retriable=False`` when the
        provider is misconfigured (bad API key, model not pulled, etc.)
        so the router can mark the tier unhealthy without crashing.
        """

    async def shutdown(self) -> None:
        """Close HTTP clients and release resources.  Default no-op."""
        return None

    @abc.abstractmethod
    async def health(self) -> dict[str, Any]:
        """Cheap operational probe.  Returns ``{"healthy": bool, ...}``."""

    # ── Generation ───────────────────────────────────────────────

    @abc.abstractmethod
    async def _do_analyze(self, request: VisionAnalysisRequest) -> VisionAnalysisResponse:
        """Provider-specific analysis call.  No retry logic here — the base
        ``analyze`` method handles that uniformly."""

    async def analyze(
        self,
        request: VisionAnalysisRequest,
        *,
        retries: int = 1,
    ) -> VisionAnalysisResponse:
        """Run :meth:`_do_analyze` with timing, retry, and stat tracking.

        ``retries`` is the number of *additional* attempts after the first
        one fails; default 1 means up to two calls.  The router adds another
        layer of retry across tiers, so per-provider retries are kept low to
        avoid latency cliffs on flaky providers.
        """
        if not self._initialized:
            raise VisionProviderError(
                f"vision provider {self.name!r} is not initialized",
                provider=self.name,
                retriable=False,
            )

        last_exc: Optional[BaseException] = None
        for attempt in range(retries + 1):
            start = time.monotonic()
            try:
                response = await self._do_analyze(request)
                elapsed_ms = (time.monotonic() - start) * 1000.0
                response.provider = self.name
                if not response.model:
                    response.model = self.model
                response.latency_ms = elapsed_ms
                self._total_calls += 1
                self._total_latency_ms += elapsed_ms
                return response
            except VisionProviderError as exc:
                last_exc = exc
                self._total_failures += 1
                if not exc.retriable or attempt == retries:
                    raise
            except Exception as exc:
                last_exc = exc
                self._total_failures += 1
                if attempt == retries:
                    raise VisionProviderError(
                        f"{self.name} analyze failed: {exc}",
                        provider=self.name,
                        retriable=True,
                    ) from exc

        # Defensive — loop should always either return or raise.
        raise VisionProviderError(
            f"{self.name} exhausted retries without raising",
            provider=self.name,
            retriable=True,
        ) from last_exc

    # ── Transition (two-image) analysis ─────────────────────────

    # Concrete providers OVERRIDE this when they support native
    # multi-image reasoning (Anthropic, OpenAI, Ollama). The default
    # implementation raises an explicit "not supported" so the tier
    # router can poison this provider for the transition path while
    # still using it for single-frame analysis.
    #
    # Providers that override should:
    #   - Decode both images (or pass paths through to a multi-image API)
    #   - Build a transition-specific prompt naming the canonical
    #     action_kind values
    #   - Parse the JSON response into VisionTransitionResponse
    #   - Raise VisionProviderError(retriable=...) on failure with the
    #     same retriable semantics as _do_analyze
    async def _do_analyze_transition(
        self, request: VisionTransitionRequest,
    ) -> VisionTransitionResponse:
        raise VisionProviderError(
            f"vision provider {self.name!r} does not implement "
            f"transition (two-image) analysis",
            provider=self.name,
            retriable=False,
        )

    async def analyze_transition(
        self,
        request: VisionTransitionRequest,
        *,
        retries: int = 1,
    ) -> VisionTransitionResponse:
        """Mirror of :meth:`analyze` for the two-image transition path.

        Tier router calls this when eyes-engine asks "what action
        happened between these two frames." Failures raise
        VisionProviderError so the router can fall through tiers.
        """
        if not self._initialized:
            raise VisionProviderError(
                f"vision provider {self.name!r} is not initialized",
                provider=self.name,
                retriable=False,
            )

        last_exc: Optional[BaseException] = None
        for attempt in range(retries + 1):
            start = time.monotonic()
            try:
                response = await self._do_analyze_transition(request)
                elapsed_ms = (time.monotonic() - start) * 1000.0
                response.provider = self.name
                if not response.model:
                    response.model = self.model
                response.latency_ms = elapsed_ms
                self._total_calls += 1
                self._total_latency_ms += elapsed_ms
                return response
            except VisionProviderError as exc:
                last_exc = exc
                self._total_failures += 1
                if not exc.retriable or attempt == retries:
                    raise
            except Exception as exc:
                last_exc = exc
                self._total_failures += 1
                if attempt == retries:
                    raise VisionProviderError(
                        f"{self.name} analyze_transition failed: {exc}",
                        provider=self.name,
                        retriable=True,
                    ) from exc

        raise VisionProviderError(
            f"{self.name} exhausted transition retries without raising",
            provider=self.name,
            retriable=True,
        ) from last_exc

    # ── Stats ────────────────────────────────────────────────────

    def stats(self) -> dict[str, Any]:
        avg_latency_ms = (
            self._total_latency_ms / self._total_calls
            if self._total_calls
            else 0.0
        )
        return {
            "provider": self.name,
            "model": self.model,
            "total_calls": self._total_calls,
            "total_failures": self._total_failures,
            "average_latency_ms": round(avg_latency_ms, 2),
        }
