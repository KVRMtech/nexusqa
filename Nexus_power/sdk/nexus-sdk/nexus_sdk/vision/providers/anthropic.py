"""Anthropic Claude vision provider (Tier 1).

Wraps the Anthropic Messages API with multimodal image content blocks.
Produces the same canonical analysis shape as the Ollama provider so the
router can swap between them transparently.

Recommended models for this provider:
    claude-opus-4-7         - flagship, best UI understanding
    claude-sonnet-4-6       - 5x cheaper, near-flagship quality
    claude-haiku-4-5        - cheap + fast, good for high-volume per-frame

Authentication: ``api_key`` from :class:`VisionTierSpec`.  The provider
never logs the key; transport errors include only the host name.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, Optional

import httpx

from ..base import (
    VisionAnalysisRequest,
    VisionAnalysisResponse,
    VisionProvider,
    VisionProviderError,
    VisionUIElement,
)
from ..config import VisionTierSpec
from .ollama import _parse_provider_response


_DEFAULT_BASE_URL = "https://api.anthropic.com"
_DEFAULT_API_VERSION = "2023-06-01"


class AnthropicVisionProvider(VisionProvider):
    """Vision provider backed by Anthropic Messages API."""

    def __init__(
        self,
        spec: VisionTierSpec,
        *,
        max_tokens: int = 1024,
        api_version: str = _DEFAULT_API_VERSION,
    ):
        super().__init__(name=f"anthropic:{spec.model}", model=spec.model, retries=spec.retries)
        if not spec.api_key:
            # Spec validation — surface the missing key now rather than on
            # first call.  The router treats this as non-retriable.
            raise VisionProviderError(
                f"anthropic tier{spec.tier} missing api_key "
                f"(set EYES_VISION_TIER{spec.tier}_API_KEY)",
                provider=self.name,
                retriable=False,
            )
        self._spec = spec
        self._base_url = (spec.base_url or _DEFAULT_BASE_URL).rstrip("/")
        self._timeout = spec.timeout_seconds
        self._max_tokens = max_tokens
        self._api_version = api_version
        self._client: Optional[httpx.AsyncClient] = None

    async def initialize(self) -> None:
        # No prefetch call — Anthropic does not expose a free probe endpoint.
        # We construct the HTTP client lazily; misconfiguration surfaces on
        # the first analyze call as a 401 / 403, which the router classifies
        # as non-retriable via the status-code mapping below.
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout,
            headers={
                "x-api-key": self._spec.api_key or "",
                "anthropic-version": self._api_version,
                "content-type": "application/json",
            },
        )
        self._initialized = True

    async def shutdown(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self._initialized = False

    async def health(self) -> dict[str, Any]:
        # Anthropic has no cheap health endpoint; we report initialised
        # state and let the next real call surface auth/network errors.
        return {
            "healthy": self._initialized,
            "provider": self.name,
            "model": self.model,
            "reason": "no_probe_endpoint" if self._initialized else "not_initialized",
        }

    async def _do_analyze(self, request: VisionAnalysisRequest) -> VisionAnalysisResponse:
        if self._client is None:
            raise VisionProviderError(
                "anthropic provider not initialized",
                provider=self.name,
                retriable=False,
            )

        prompt_text = _build_prompt(
            ocr_text=request.ocr_text,
            app_type=request.app_type,
            previous_description=request.previous_description,
        )

        try:
            image_b64, media_type = _encode_image(request.frame_path)
        except FileNotFoundError as exc:
            raise VisionProviderError(
                f"frame not found: {request.frame_path}",
                provider=self.name,
                retriable=False,
            ) from exc
        except OSError as exc:
            raise VisionProviderError(
                f"failed to read frame {request.frame_path}: {exc}",
                provider=self.name,
                retriable=False,
            ) from exc

        content_blocks: list[dict[str, Any]] = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": image_b64,
                },
            },
        ]
        # When the caller provided a previous frame path, attach it so the
        # model can reason about *what changed* — Anthropic supports
        # multiple image blocks per message.
        if request.previous_frame_path:
            try:
                prev_b64, prev_media = _encode_image(request.previous_frame_path)
                # Insert the previous frame BEFORE the current one and
                # introduce both with text so the model knows their roles.
                content_blocks = [
                    {"type": "text", "text": "PREVIOUS FRAME (for change-detection):"},
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": prev_media, "data": prev_b64},
                    },
                    {"type": "text", "text": "CURRENT FRAME (analyze this one):"},
                    content_blocks[0],
                ]
            except (FileNotFoundError, OSError):
                # Missing previous frame is non-fatal — degrade to single
                # image with text-only previous_description in the prompt.
                pass

        content_blocks.append({"type": "text", "text": prompt_text})

        payload = {
            "model": self.model,
            "max_tokens": (
                request.max_tokens
                if request.max_tokens is not None
                else self._max_tokens
            ),
            "temperature": (
                request.temperature
                if request.temperature is not None
                else 0.1
            ),
            "messages": [
                {"role": "user", "content": content_blocks},
            ],
        }

        try:
            resp = await self._client.post("/v1/messages", json=payload, timeout=self._timeout)
        except httpx.TimeoutException as exc:
            raise VisionProviderError(
                f"anthropic timeout after {self._timeout}s",
                provider=self.name,
                retriable=True,
            ) from exc
        except httpx.HTTPError as exc:
            raise VisionProviderError(
                f"anthropic HTTP error: {type(exc).__name__}",
                provider=self.name,
                retriable=True,
            ) from exc

        # Status-code based retry classification.  4xx is permanent
        # (bad key, bad model, image too large) so the router should not
        # retry the same tier; 5xx and 429 are transient.
        if resp.status_code in (401, 403):
            raise VisionProviderError(
                f"anthropic auth failed (status {resp.status_code})",
                provider=self.name,
                retriable=False,
            )
        if resp.status_code == 429:
            raise VisionProviderError(
                "anthropic rate-limited (429)",
                provider=self.name,
                retriable=True,
            )
        if resp.status_code >= 500:
            raise VisionProviderError(
                f"anthropic server error {resp.status_code}",
                provider=self.name,
                retriable=True,
            )
        if resp.status_code >= 400:
            raise VisionProviderError(
                f"anthropic client error {resp.status_code}: {resp.text[:200]}",
                provider=self.name,
                retriable=False,
            )

        body = resp.json()
        # Messages API returns content as a list of blocks; concatenate the
        # text blocks (the JSON we asked for is in there).
        text_chunks: list[str] = []
        for block in body.get("content", []) or []:
            if isinstance(block, dict) and block.get("type") == "text":
                text_chunks.append(str(block.get("text", "")))
        content_str = "".join(text_chunks).strip()

        response = _parse_provider_response(
            content_str=content_str, raw=body, model=self.model,
        )
        # Anthropic surfaces stop_reason="max_tokens" when output was clipped.
        if (body.get("stop_reason") or "").lower() == "max_tokens":
            response.truncated = True
        return response

    async def _do_analyze_transition(
        self, request,  # type: ignore[override]  # VisionTransitionRequest
    ):
        """Two-image transition via Anthropic Messages API. Two image
        content blocks in a single user message — Claude reasons about
        them as a chronological pair."""
        from ..base import (
            VisionTransitionRequest,        # noqa: F401  — type hint via comment above
            VisionTransitionResponse,
            VisionProviderError,
        )
        if self._client is None:
            raise VisionProviderError(
                "anthropic provider not initialized",
                provider=self.name, retriable=False,
            )

        try:
            b_before, mt_before = _encode_image(request.before_frame_path)
            b_after, mt_after = _encode_image(request.after_frame_path)
        except FileNotFoundError as exc:
            raise VisionProviderError(
                f"frame not found in transition request: {exc}",
                provider=self.name, retriable=False,
            ) from exc
        except OSError as exc:
            raise VisionProviderError(
                f"failed to read transition frames: {exc}",
                provider=self.name, retriable=False,
            ) from exc

        from ._transition_shared import (
            build_transition_prompt, parse_transition_response,
        )
        prompt_text = build_transition_prompt(
            ocr_before=request.ocr_before,
            ocr_after=request.ocr_after,
            url_changed=request.url_changed,
        )

        content_blocks: list[dict[str, Any]] = [
            {"type": "text", "text": "BEFORE (Image 1):"},
            {
                "type": "image",
                "source": {"type": "base64", "media_type": mt_before, "data": b_before},
            },
            {"type": "text", "text": "AFTER (Image 2):"},
            {
                "type": "image",
                "source": {"type": "base64", "media_type": mt_after, "data": b_after},
            },
            {"type": "text", "text": prompt_text},
        ]

        payload = {
            "model": self.model,
            "max_tokens": (
                request.max_tokens
                if request.max_tokens is not None else self._max_tokens
            ),
            "temperature": (
                request.temperature
                if request.temperature is not None else 0.1
            ),
            "messages": [{"role": "user", "content": content_blocks}],
        }

        try:
            resp = await self._client.post(
                "/v1/messages", json=payload, timeout=self._timeout,
            )
        except httpx.TimeoutException as exc:
            raise VisionProviderError(
                f"anthropic transition timeout after {self._timeout}s",
                provider=self.name, retriable=True,
            ) from exc
        except httpx.HTTPError as exc:
            raise VisionProviderError(
                f"anthropic transition HTTP error: {type(exc).__name__}",
                provider=self.name, retriable=True,
            ) from exc

        if resp.status_code in (401, 403):
            raise VisionProviderError(
                f"anthropic auth failed (status {resp.status_code})",
                provider=self.name, retriable=False,
            )
        if resp.status_code == 429:
            raise VisionProviderError(
                "anthropic rate-limited (429)",
                provider=self.name, retriable=True,
            )
        if resp.status_code >= 500:
            raise VisionProviderError(
                f"anthropic server error {resp.status_code}",
                provider=self.name, retriable=True,
            )
        if resp.status_code >= 400:
            raise VisionProviderError(
                f"anthropic client error {resp.status_code}: {resp.text[:200]}",
                provider=self.name, retriable=False,
            )

        body = resp.json()
        text_chunks: list[str] = []
        for block in body.get("content", []) or []:
            if isinstance(block, dict) and block.get("type") == "text":
                text_chunks.append(str(block.get("text", "")))
        content_str = "".join(text_chunks).strip()

        return parse_transition_response(
            content_str=content_str, raw=body, model=self.model,
        )


def _encode_image(frame_path: str) -> tuple[str, str]:
    """Return (base64, media_type) for an image at ``frame_path``.

    Media type detection is by file extension — Anthropic supports
    image/jpeg, image/png, image/gif, image/webp.  Unknown extensions
    default to image/png since the eyes engine writes PNGs.
    """
    p = Path(frame_path)
    suffix = p.suffix.lower()
    media_type = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }.get(suffix, "image/png")
    with p.open("rb") as fh:
        return base64.b64encode(fh.read()).decode("ascii"), media_type


def _build_prompt(*, ocr_text: str, app_type: str, previous_description: str) -> str:
    """Build the analysis prompt for Anthropic vision.

    Same canonical fields as Ollama; differences:
      - We use Markdown emphasis since Claude responds well to formatting
      - We're explicit that JSON only is required so the parser can rely
        on it without code-fence stripping in most cases
    """
    parts: list[str] = [
        "You are analyzing a screenshot from a software application for QA testing.",
    ]
    if app_type:
        parts.append(f"**Application type:** {app_type}")
    if ocr_text:
        parts.append(f"**OCR text extracted from this frame:**\n{ocr_text[:2000]}")
    if previous_description:
        parts.append(
            "**Previous frame description (for change-detection):**\n"
            f"{previous_description[:1200]}\n"
            "Focus on what CHANGED in the current frame relative to the previous: "
            "typed values, selected options, button states, modal opens, focus shifts."
        )
    parts.append(
        "Respond with a JSON object containing exactly these keys:\n"
        '  "description": one-paragraph natural language summary of what is on screen\n'
        '  "ui_elements": array of {element_type, text, confidence, properties}\n'
        '  "tables": array of any data tables (empty array if none)\n'
        '  "page_title": the page or dialog title shown\n'
        "element_type values: button, text_field, dropdown, label, table, "
        "menu, checkbox, radio, link, image, tab.\n"
        "Return ONLY the JSON object, no surrounding prose."
    )
    return "\n\n".join(parts)
