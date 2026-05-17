"""OpenAI vision provider (Tier 1 / Tier 2).

Wraps the OpenAI Chat Completions API with image content blocks
(``type: image_url`` with a base64 data URI).  Compatible with gpt-4o,
gpt-4o-mini, and any future multimodal model that follows the same
schema.

Authentication: ``api_key`` from :class:`VisionTierSpec`.  The provider
never logs the key.
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
)
from ..config import VisionTierSpec
from .ollama import _parse_provider_response


_DEFAULT_BASE_URL = "https://api.openai.com"


class OpenAIVisionProvider(VisionProvider):
    """Vision provider backed by OpenAI Chat Completions API."""

    def __init__(
        self,
        spec: VisionTierSpec,
        *,
        max_tokens: int = 1024,
    ):
        super().__init__(name=f"openai:{spec.model}", model=spec.model, retries=spec.retries)
        if not spec.api_key:
            raise VisionProviderError(
                f"openai tier{spec.tier} missing api_key "
                f"(set EYES_VISION_TIER{spec.tier}_API_KEY)",
                provider=self.name,
                retriable=False,
            )
        self._spec = spec
        self._base_url = (spec.base_url or _DEFAULT_BASE_URL).rstrip("/")
        self._timeout = spec.timeout_seconds
        self._max_tokens = max_tokens
        self._client: Optional[httpx.AsyncClient] = None

    async def initialize(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout,
            headers={
                "Authorization": f"Bearer {self._spec.api_key}",
                "Content-Type": "application/json",
            },
        )
        self._initialized = True

    async def shutdown(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self._initialized = False

    async def health(self) -> dict[str, Any]:
        return {
            "healthy": self._initialized,
            "provider": self.name,
            "model": self.model,
            "reason": "no_probe_endpoint" if self._initialized else "not_initialized",
        }

    async def _do_analyze(self, request: VisionAnalysisRequest) -> VisionAnalysisResponse:
        if self._client is None:
            raise VisionProviderError(
                "openai provider not initialized",
                provider=self.name,
                retriable=False,
            )

        prompt_text = _build_prompt(
            ocr_text=request.ocr_text,
            app_type=request.app_type,
            previous_description=request.previous_description,
        )

        try:
            data_uri = _encode_image_data_uri(request.frame_path)
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
            {"type": "image_url", "image_url": {"url": data_uri}},
        ]
        if request.previous_frame_path:
            try:
                prev_data_uri = _encode_image_data_uri(request.previous_frame_path)
                content_blocks = [
                    {"type": "text", "text": "PREVIOUS FRAME (for change-detection):"},
                    {"type": "image_url", "image_url": {"url": prev_data_uri}},
                    {"type": "text", "text": "CURRENT FRAME (analyze this one):"},
                    content_blocks[0],
                ]
            except (FileNotFoundError, OSError):
                pass
        content_blocks.append({"type": "text", "text": prompt_text})

        payload: dict[str, Any] = {
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
            "response_format": {"type": "json_object"},
        }

        try:
            resp = await self._client.post(
                "/v1/chat/completions", json=payload, timeout=self._timeout,
            )
        except httpx.TimeoutException as exc:
            raise VisionProviderError(
                f"openai timeout after {self._timeout}s",
                provider=self.name,
                retriable=True,
            ) from exc
        except httpx.HTTPError as exc:
            raise VisionProviderError(
                f"openai HTTP error: {type(exc).__name__}",
                provider=self.name,
                retriable=True,
            ) from exc

        if resp.status_code in (401, 403):
            raise VisionProviderError(
                f"openai auth failed (status {resp.status_code})",
                provider=self.name,
                retriable=False,
            )
        if resp.status_code == 429:
            raise VisionProviderError("openai rate-limited (429)", provider=self.name, retriable=True)
        if resp.status_code >= 500:
            raise VisionProviderError(
                f"openai server error {resp.status_code}",
                provider=self.name,
                retriable=True,
            )
        if resp.status_code >= 400:
            raise VisionProviderError(
                f"openai client error {resp.status_code}: {resp.text[:200]}",
                provider=self.name,
                retriable=False,
            )

        body = resp.json()
        # Chat Completions returns choices[0].message.content as a string.
        choices = body.get("choices") or []
        content_str = ""
        finish_reason = ""
        if choices and isinstance(choices[0], dict):
            msg = choices[0].get("message") or {}
            content_str = str(msg.get("content", "") or "")
            finish_reason = str(choices[0].get("finish_reason", "") or "")

        response = _parse_provider_response(
            content_str=content_str, raw=body, model=self.model,
        )
        if finish_reason == "length":
            response.truncated = True
        return response

    async def _do_analyze_transition(
        self, request,  # VisionTransitionRequest
    ):
        """Two-image transition via OpenAI vision-enabled chat. Two
        image_url content blocks in a single user message — gpt-4o
        reasons over both as a chronological pair."""
        from ..base import VisionProviderError
        from ._transition_shared import (
            build_transition_prompt, parse_transition_response,
        )

        if self._client is None:
            raise VisionProviderError(
                "openai provider not initialized",
                provider=self.name, retriable=False,
            )

        try:
            uri_before = _encode_image_data_uri(request.before_frame_path)
            uri_after = _encode_image_data_uri(request.after_frame_path)
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

        prompt_text = build_transition_prompt(
            ocr_before=request.ocr_before,
            ocr_after=request.ocr_after,
            url_changed=request.url_changed,
        )
        content_blocks: list[dict[str, Any]] = [
            {"type": "text", "text": "BEFORE (Image 1):"},
            {"type": "image_url", "image_url": {"url": uri_before}},
            {"type": "text", "text": "AFTER (Image 2):"},
            {"type": "image_url", "image_url": {"url": uri_after}},
            {"type": "text", "text": prompt_text},
        ]

        payload: dict[str, Any] = {
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
            "response_format": {"type": "json_object"},
        }

        try:
            resp = await self._client.post(
                "/v1/chat/completions", json=payload, timeout=self._timeout,
            )
        except httpx.TimeoutException as exc:
            raise VisionProviderError(
                f"openai transition timeout after {self._timeout}s",
                provider=self.name, retriable=True,
            ) from exc
        except httpx.HTTPError as exc:
            raise VisionProviderError(
                f"openai transition HTTP error: {type(exc).__name__}",
                provider=self.name, retriable=True,
            ) from exc

        if resp.status_code in (401, 403):
            raise VisionProviderError(
                f"openai auth failed (status {resp.status_code})",
                provider=self.name, retriable=False,
            )
        if resp.status_code == 429:
            raise VisionProviderError(
                "openai rate-limited (429)",
                provider=self.name, retriable=True,
            )
        if resp.status_code >= 500:
            raise VisionProviderError(
                f"openai server error {resp.status_code}",
                provider=self.name, retriable=True,
            )
        if resp.status_code >= 400:
            raise VisionProviderError(
                f"openai client error {resp.status_code}: {resp.text[:200]}",
                provider=self.name, retriable=False,
            )

        body = resp.json()
        choices = body.get("choices") or []
        content_str = ""
        if choices and isinstance(choices[0], dict):
            msg = choices[0].get("message") or {}
            content_str = str(msg.get("content", "") or "")

        return parse_transition_response(
            content_str=content_str, raw=body, model=self.model,
        )


def _encode_image_data_uri(frame_path: str) -> str:
    """Return ``data:<media>;base64,<...>`` URI for an image."""
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
        b64 = base64.b64encode(fh.read()).decode("ascii")
    return f"data:{media_type};base64,{b64}"


def _build_prompt(*, ocr_text: str, app_type: str, previous_description: str) -> str:
    parts: list[str] = [
        "You are analyzing a screenshot from a software application for QA testing.",
    ]
    if app_type:
        parts.append(f"Application type: {app_type}")
    if ocr_text:
        parts.append(f"OCR text from this frame:\n{ocr_text[:2000]}")
    if previous_description:
        parts.append(
            "Previous frame description (focus on what CHANGED):\n"
            f"{previous_description[:1200]}"
        )
    parts.append(
        "Respond with a JSON object: "
        '"description" (string), '
        '"ui_elements" (array of {element_type, text, confidence, properties}), '
        '"tables" (array; empty if none), '
        '"page_title" (string). '
        "element_type ∈ {button, text_field, dropdown, label, table, menu, "
        "checkbox, radio, link, image, tab}."
    )
    return "\n\n".join(parts)
